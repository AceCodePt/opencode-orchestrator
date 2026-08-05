"""The supervisor: policy and authority.

Split deliberately into a **pure decision function** and a thin executor. `decide()`
takes a snapshot and returns actions; it touches no process, no socket and no
database. That is what makes the policy testable at all - budgets, reclaim rules and
the difference between "quiet" and "dead" are exactly the parts that are impossible
to exercise against live agents on demand.

The one rule encoded here that cost the most to learn: **a session outlives its
dispatch process.** Measured - a run exited 0 and its session then performed 24 more
tool calls over the following 36 seconds, because the router injected a follow-up
prompt. So "the dispatch pid is gone" says nothing about whether work is finished,
and a supervisor that reclaims on that basis destroys live work.

How a task reaches `done`: **this process runs the gate itself.** It does not accept a
verdict from anywhere.

That is a deliberate refusal of the obvious design. The router already runs gates and
could simply report the result, which is what the original task described. But the
router is a plugin inside the agent's own server, and the agent holds a bash tool, so
a `passed` coming from there is a self-report - the agent would author the answer
rather than the criterion, which is the same hole in a new place. Reading the gate
from the base branch removed the agent's authority over *what* is checked; only
running it out-of-loop removes its authority over *what the result was*.

So the two runs serve different purposes and neither is redundant. The router's run is
advisory and in-loop: its job is to hand a failure back to the agent while the agent
is still there to fix it. This one is authoritative and terminal, and it happens after
the session has stopped, in a process the session never spawned.

The cost is running the suite twice on a passing task. That is the right thing to pay:
the alternative is a `done` that means "something inside the sandbox said so".
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gate, observe, server
from .store import DONE, NEEDS_REVIEW as ST_NEEDS_REVIEW, RUNNING, Store, Task

# Per-state silence budgets. A blocked agent is not "quiet", it is waiting on a
# human, so it gets a shorter fuse than one that is legitimately grinding.
BUDGET_BUSY_S = 20 * 60
BUDGET_IDLE_S = 2 * 60
BUDGET_BLOCKED_S = 60

# Why a task stopped, which is not the same as whether it is done.
#
# `finished` is a property of the in-loop gate loop having *terminated*, not of the
# session merely going quiet. Going idle is what fires the gate; a failing gate injects
# a prompt and the agent works again. So an agent only stays stopped once the loop can
# no longer move it: the gate passed, or the retry cap was spent.
FINISHED_COMPLETE = "finished: gate loop settled on a pass"
FINISHED_GAVE_UP = "finished: retry cap spent"
STILL_RETRYING = "not finished: the gate loop is still driving it"
NEVER_GATED = "not finished: stopped and no gate ever ran"

RENEW = "renew"
RECLAIM = "reclaim"
NEEDS_REVIEW = "needs-review"
ALERT = "alert"
RESTART_SERVER = "restart-server"
NOTE = "note"
# Run the committed gate and let its result decide the status. Emitted rather than
# performed, because `decide` must stay pure - and because that keeps "when do we
# check" testable separately from "what did the check say".
VERIFY = "verify"


@dataclass
class Action:
    kind: str
    task: str | None = None
    reason: str = ""
    detail: str = ""
    # Carried per action because one daemon supervises every project on the machine.
    # Task names are only unique within a project, so an action that named a task
    # without its project would eventually renew or reclaim the wrong row.
    project: str | None = None


@dataclass
class Snapshot:
    """Everything `decide` is allowed to know."""

    now: int
    server_running: bool
    server_unresponsive: bool
    tasks: list[Task] = field(default_factory=list)
    # worktree path -> what the shared server says about that worktree's sessions.
    # Absent key means the server knows no session there, which is not the same as
    # idle: never dispatched, or dispatched into a server that has been replaced.
    facts: dict[str, observe.TaskFact] = field(default_factory=dict)
    # pid -> whether the dispatch process is still alive
    dispatch_alive: dict[int, bool] = field(default_factory=dict)
    # worktree -> seconds since that agent last changed state
    quiet_for: dict[str, int] = field(default_factory=dict)
    # worktree -> the router's most recent in-loop outcome, or None if it never
    # reported. This is how "stopped" is told apart from "finished": the router is what
    # drives an idle agent back to work, so its last word says whether anything more
    # was ever going to happen.
    router_outcome: dict[str, str | None] = field(default_factory=dict)


def source_fingerprint() -> str:
    """Hash of the code this process would run if started now.

    A version string is useless for this: it stays `0.1.0` across every commit, so it
    cannot tell a running daemon from an upgraded checkout. Hashing the sources catches
    any change, committed or not, which is what the question actually is - is the
    supervisor in memory the supervisor on disk.
    """
    h = hashlib.sha256()
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(f.name.encode())
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:12]


def project_label(project: str) -> str:
    """A readable name for a project path.

    A project is identified by its git common dir, which for an ordinary clone is
    `<repo>/.git` - so the last path component is the useless string `.git`. Bare repos
    end in `<name>.git`, which is fine as-is.
    """
    p = Path(project)
    return p.parent.name if p.name == ".git" else p.name


def stop_reason(outcome: str | None) -> str:
    """Classify a stopped agent by what the in-loop gate loop last did.

    Deliberately reads the router's outcome rather than asking the agent. The router's
    verdict cannot be trusted as a statement about *quality* - it runs inside the
    agent's own server - but it is solid evidence about *mechanism*: whether the loop
    that would have sent the agent back to work has stopped trying.
    """
    if outcome == "passed":
        return FINISHED_COMPLETE
    if outcome == "stuck":
        return FINISHED_GAVE_UP
    if outcome in ("failed", "gate-modified"):
        return STILL_RETRYING
    # Includes `no-gate-configured` and the absence of any report at all: no router
    # installed, a plugin that threw, or a gate that never fired. Idle is what triggers
    # the gate, so silence here means the mechanism did not run.
    return NEVER_GATED


def decide(snap: Snapshot) -> list[Action]:
    actions: list[Action] = []

    # ---------------------------------------------------------------- the server
    if not snap.server_running or snap.server_unresponsive:
        why = ("is not running" if not snap.server_running
               else "is alive but answers nothing")
        actions.append(Action(
            RESTART_SERVER, None,
            f"the shared server {why}",
            "every dispatched task lives inside this one process, so its death "
            "takes them all with it",
        ))
        for t in snap.tasks:
            if t.status != RUNNING:
                continue
            # Escalate rather than re-dispatch automatically. The worktree still
            # exists and may hold partial work, so re-running from scratch and
            # resuming are different operations, and picking silently would hide
            # which one happened.
            actions.append(Action(
                NEEDS_REVIEW, t.name,
                "the shared server died while this task was running",
                "its worktree may hold partial work; re-dispatch is a decision, "
                "not a default",
                project=t.project,
            ))
        return actions

    # ----------------------------------------------------------------- each task
    for t in snap.tasks:
        if t.status != RUNNING:
            continue

        fact = snap.facts.get(t.worktree or "")
        alive = snap.dispatch_alive.get(t.pid or -1, False)
        quiet = snap.quiet_for.get(t.worktree or "", 0)

        if fact is not None and fact.needs_human:
            for b in fact.blocked:
                actions.append(Action(
                    ALERT, t.name,
                    f"blocked on {b.kind}",
                    f"{b.detail} [{b.request_id}]",
                    project=t.project,
                ))
            # Still renew: it is waiting on a human, which is the opposite of dead,
            # and letting the lease lapse here invites a reclaim of live work.
            actions.append(Action(RENEW, t.name, "blocked but alive",
                                  project=t.project))
            if quiet > BUDGET_BLOCKED_S:
                actions.append(Action(
                    NOTE, t.name,
                    f"waiting on a human for {quiet}s",
                    "nothing will happen until it is answered",
                    project=t.project,
                ))
            continue

        if fact is not None and fact.state == observe.BUSY:
            actions.append(Action(RENEW, t.name, "session is busy",
                                  project=t.project))
            if quiet > BUDGET_BUSY_S:
                actions.append(Action(
                    NOTE, t.name,
                    f"busy but unchanged for {quiet}s",
                    "may be looping; a static todo list plus rising cost is the tell",
                    project=t.project,
                ))
            continue

        if alive:
            # The dispatch is running but the session is not visibly busy. Normal
            # during startup, and not something to act on.
            actions.append(Action(RENEW, t.name, "dispatch process is alive",
                                  project=t.project))
            continue

        # Dispatch gone from here on.
        if fact is not None and fact.state == observe.IDLE:
            # Stopped. Whether it is *finished* depends on the in-loop gate loop: going
            # idle is what fires the gate, and a failing gate sends the agent back to
            # work. So the loop's last word decides what happens next.
            why = stop_reason(snap.router_outcome.get(t.worktree or ""))

            if why == STILL_RETRYING:
                # The loop is mid-flight. Injecting a prompt makes the session busy
                # again, so idle here is a moment between attempts - not an end.
                actions.append(Action(RENEW, t.name, why, project=t.project))
                if quiet > BUDGET_IDLE_S:
                    actions.append(Action(
                        NEEDS_REVIEW, t.name,
                        f"stopped mid-retry and idle for {quiet}s",
                        "the gate failed and nothing drove it back to work - the "
                        "prompt injection or the router itself has stopped working",
                        project=t.project,
                    ))
                continue

            if why == NEVER_GATED:
                # The silent hole. Verifying here would let a gate that never fired
                # be replaced by one lucky out-of-loop run, and "nothing objected"
                # would become a pass for work nothing in-loop ever examined.
                actions.append(Action(
                    NEEDS_REVIEW, t.name, why,
                    "no in-loop verdict was ever recorded, so the router did not "
                    "fire here: check that it is installed and the gate is committed",
                    project=t.project,
                ))
                continue

            actions.append(Action(
                VERIFY, t.name, why,
                "verified out-of-loop on purpose: a verdict from the router would be "
                "the agent's own report",
                project=t.project,
            ))
            continue

        if fact is None:
            expired = (t.lease_until is not None and t.lease_until < snap.now)
            if expired:
                # All three together: lease lapsed, dispatch gone, nothing
                # listening for this worktree. Only then is it definitely dead.
                actions.append(Action(
                    RECLAIM, t.name,
                    "definitely dead: lease expired, dispatch gone, no session",
                    project=t.project,
                ))
            else:
                actions.append(Action(
                    NOTE, t.name,
                    "dispatch exited and no session is visible, but the lease is "
                    "still valid",
                    "waiting for the lease to lapse before reclaiming - an expired "
                    "lease alone is not proof of death",
                    project=t.project,
                ))
            continue

        actions.append(Action(RENEW, t.name, f"state {fact.state}",
                              project=t.project))

    return actions


# --------------------------------------------------------------------- executor


class Supervisor:
    """One supervisor per machine, not per project.

    There is one shared server, one database and one set of leases per machine, so a
    per-project daemon would have to be started once per project and would silently
    supervise nothing for any project nobody remembered to start. `project=None`
    means every project, which is the mode systemd runs.
    """

    def __init__(self, project: str | None = None, store: Store | None = None,
                 dry_run: bool = False) -> None:
        self.project = project
        self.store = store or Store()
        self.dry_run = dry_run
        self._last_state: dict[str, tuple[str, int]] = {}

    def snapshot(self) -> Snapshot:
        st = server.status()
        tasks_all = self.store.tasks(self.project, status=RUNNING)

        # Ask about each task's worktree specifically. Enumerating processes cannot
        # work here: one shared server has a single cwd and hosts every worktree, so
        # a per-process view can never say which *task* is busy. `GET /session` is
        # also global - it returned 33 sessions spanning directories going back
        # days - so the directory filter is the only usable join.
        by_worktree: dict[str, observe.TaskFact] = {}
        port = st.get("port") if st.get("running") and not st.get("unresponsive") else None
        if port:
            for t in tasks_all:
                if not t.worktree:
                    continue
                tf = observe.look_task(int(port), t.worktree)
                if tf is not None:
                    by_worktree[t.worktree] = tf

        now = int(time.time())
        quiet: dict[str, int] = {}
        for wt, f in by_worktree.items():
            prev = self._last_state.get(wt)
            if prev is None or prev[0] != f.state:
                self._last_state[wt] = (f.state, now)
                quiet[wt] = 0
            else:
                quiet[wt] = now - prev[1]

        alive = {
            t.pid: server.pid_alive(t.pid)
            for t in tasks_all if t.pid
        }

        # The router's last in-loop word per worktree. Read from the store rather than
        # asked of the agent: it is untrustworthy about quality and reliable about
        # mechanism, and mechanism is all this needs.
        router: dict[str, str | None] = {}
        for t in tasks_all:
            if not t.worktree:
                continue
            row = self.store.last_verdict(t.project, t.name, source="router")
            router[t.worktree] = row["outcome"] if row else None

        return Snapshot(
            now=now,
            server_running=bool(st.get("running")),
            server_unresponsive=bool(st.get("unresponsive")),
            tasks=tasks_all,
            facts=by_worktree,
            dispatch_alive=alive,
            quiet_for=quiet,
            router_outcome=router,
        )

    def apply(self, actions: list[Action]) -> list[str]:
        log: list[str] = []
        for a in actions:
            # Label with the project, since one daemon now covers all of them and two
            # projects may hold a task of the same name.
            where = f"[{project_label(a.project)}] " if a.project else ""
            log.append(f"{a.kind}: {where}{a.task or '-'} - {a.reason}"
                       + (f" ({a.detail})" if a.detail else ""))
            if self.dry_run:
                continue
            proj = a.project or self.project
            if a.kind == RENEW and a.task and proj:
                self.store.renew(proj, a.task)
            elif a.kind == RECLAIM and a.task and proj:
                self.store.reclaim(proj, a.task, a.reason)
            elif a.kind == NEEDS_REVIEW and a.task and proj:
                self.store.finish(proj, a.task, "needs-review", a.reason)
            elif a.kind == ALERT and a.task:
                # Dedup lives in the store, keyed on the request id: one turn can
                # raise several requests, and alerting on "is /permission non-empty"
                # fires repeatedly for what a human experiences as one decision.
                rid = a.detail.rsplit("[", 1)[-1].rstrip("]") if "[" in a.detail else a.detail
                if self.store.note_alert(rid, "", a.task, a.reason, a.detail):
                    log.append(f"  -> new alert {rid} (would notify)")
            elif a.kind == VERIFY and a.task and proj:
                for line in self.verify(a.task, project=proj, why=a.reason):
                    log.append(f"  -> {line}")
            elif a.kind == RESTART_SERVER:
                try:
                    info = server.restart()
                    log.append(f"  -> restarted shared server pid={info.pid} {info.url}")
                except (RuntimeError, TimeoutError, OSError) as e:
                    log.append(f"  -> could not restart the shared server: {e}")
        return log

    def verify(self, name: str, event: str = "session-idle",
               project: str | None = None, why: str = "") -> list[str]:
        """Run the committed gate for a finished task and record what it said.

        Only `passed` promotes to `done`. Everything else - a failure, a timeout, or no
        committed gate at all - lands on `needs-review`, because they share the one
        property that matters: nobody has established that this branch is good.

        An agent that spent its retry cap never reaches `done` even on a passing gate.
        A suite that failed three times and then passes untouched has not earned
        confidence; it has raised a question about flakiness, and that is a human's to
        answer.
        """
        proj = project or self.project
        if not proj:
            return [f"cannot verify {name}: no project given"]
        t = self.store.get(proj, name)
        if t is None or not t.worktree:
            return [f"cannot verify {name}: no recorded worktree"]
        wt = Path(t.worktree)
        if not wt.exists():
            self.store.finish(proj, name, ST_NEEDS_REVIEW,
                              "worktree is gone")
            return [f"worktree {wt} no longer exists -> needs-review"]

        v = gate.run(wt, event)
        self.store.record_verdict(
            proj, name, v.event, v.outcome, v.detail,
            diverged=v.diverged, seconds=v.seconds, source="daemon",
        )
        out = [f"gate {v.summary}"]
        if v.diverged:
            out.append("the worktree's gate differs from the committed one; the "
                       "committed gate ran, but review the diff")
        if v.justifies_done and why == FINISHED_GAVE_UP:
            self.store.finish(proj, name, ST_NEEDS_REVIEW,
                              f"{v.event}: {v.outcome} after the cap was spent")
            out.append(f"{name} -> needs-review (the gate passes now, but the agent "
                       f"had already spent its retry cap - suspect flakiness)")
        elif v.justifies_done:
            self.store.finish(proj, name, DONE, f"{v.event}: {v.outcome}")
            out.append(f"{name} -> done")
        else:
            self.store.finish(proj, name, ST_NEEDS_REVIEW,
                              f"{v.event}: {v.outcome}")
            reason = ("no committed gate, so nothing verified this work"
                      if v.outcome == gate.NO_GATE else "the gate said no")
            out.append(f"{name} -> needs-review ({reason})")
            if v.detail:
                out.append(v.detail.strip().splitlines()[-1][:200])
        return out

    def step(self) -> list[str]:
        return self.apply(decide(self.snapshot()))

    def close(self) -> None:
        self.store.close()


def run(project: str | None = None, interval: float = 10.0, once: bool = False,
        dry_run: bool = False) -> int:
    sup = Supervisor(project, dry_run=dry_run)
    scope = project_label(project) if project else "all projects"
    print(f"orch daemon: supervising {scope}, every {interval:g}s", flush=True)

    # Record which version is actually running. A long-lived daemon holds the code it
    # imported at startup, so upgrading `orch` on disk changes nothing until the unit is
    # restarted - and the daemon keeps enforcing the old policy while looking healthy.
    # Observed here: a supervisor that had been running for fourteen minutes verified a
    # task using the previous release's rules.
    if not dry_run:
        try:
            sup.store.set_meta("daemon_running_source", source_fingerprint())
            sup.store.set_meta("daemon_started_at", str(int(time.time())))
        except Exception:  # noqa: BLE001
            pass
    try:
        while True:
            for line in sup.step():
                print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sup.close()


# ---------------------------------------------------------------------- systemd

UNIT_NAME = "orch-daemon.service"

UNIT = """\
[Unit]
Description=orch supervisor - renews leases, verifies stopped agents, restarts the shared server
Documentation=https://github.com/AceCodePt/opencode-orchestrator
After=network-online.target

[Service]
Type=simple
ExecStart={orch} daemon --interval {interval:g}
# Restart always, not on-failure: a supervisor that has exited cleanly is still a
# supervisor that is not supervising, and nothing else notices its absence.
Restart=always
RestartSec=5
# Journald already timestamps and rotates, so the daemon logs plainly to stdout.
StandardOutput=journal
StandardError=journal
# Never kill the shared server or a running agent when the unit stops: they are
# started with start_new_session and deliberately outlive this process.
KillMode=process

[Install]
WantedBy=default.target
"""


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT_NAME


def unit_text(interval: float = 10.0) -> str:
    return UNIT.format(orch=server.orch_bin(), interval=interval)


def install(interval: float = 10.0, enable: bool = True) -> list[str]:
    """Install the user unit, and make it survive both logout and reboot.

    A *user* unit rather than a system one: the daemon runs the same gates a human
    runs, against worktrees in a home directory, and needs no privilege beyond the
    account that owns them.

    `loginctl enable-linger` is the part that is easy to miss and silently defeats the
    whole point - without it a user unit starts at login and dies at logout, so a
    machine that reboots unattended comes back with nothing supervising anything.
    """
    out: list[str] = []
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(interval))
    out.append(f"wrote {path}")

    if not enable:
        out.append("not enabled. To finish:")
        out.append("  systemctl --user daemon-reload")
        out.append(f"  systemctl --user enable --now {UNIT_NAME}")
        out.append(f"  loginctl enable-linger {os.environ.get('USER', '$USER')}")
        return out

    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", UNIT_NAME],
        ["loginctl", "enable-linger", os.environ.get("USER") or ""],
    ):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                out.append(f"ok: {' '.join(cmd)}")
            else:
                out.append(f"FAILED: {' '.join(cmd)}: "
                           f"{(p.stderr or p.stdout).strip()[:200]}")
        except (OSError, subprocess.TimeoutExpired) as e:
            out.append(f"FAILED: {' '.join(cmd)}: {e}")
    return out


def unit_status() -> dict:
    """What systemd thinks, rather than what we hope."""
    if not unit_path().exists():
        return {"installed": False}
    info: dict = {"installed": True, "unit": str(unit_path())}
    for prop in ("ActiveState", "SubState", "UnitFileState", "ExecMainPID"):
        try:
            p = subprocess.run(
                ["systemctl", "--user", "show", "-p", prop, "--value", UNIT_NAME],
                capture_output=True, text=True, timeout=10,
            )
            info[prop] = p.stdout.strip() if p.returncode == 0 else "?"
        except (OSError, subprocess.TimeoutExpired):
            info[prop] = "?"
    try:
        p = subprocess.run(["loginctl", "show-user", os.environ.get("USER") or "",
                            "-p", "Linger", "--value"],
                           capture_output=True, text=True, timeout=10)
        info["linger"] = p.stdout.strip() == "yes"
    except (OSError, subprocess.TimeoutExpired):
        info["linger"] = None
    return info
