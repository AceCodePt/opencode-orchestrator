from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gate, merge, observe, server
from .store import DONE, NEEDS_REVIEW as ST_NEEDS_REVIEW, RUNNING, Store, Task

BUDGET_BUSY_S = 20 * 60
BUDGET_IDLE_S = 2 * 60
BUDGET_BLOCKED_S = 60

FINISHED_COMPLETE = "finished: gate loop settled on a pass"
FINISHED_GAVE_UP = "finished: retry cap spent"
STILL_RETRYING = "not finished: the gate loop is still driving it"
GATE_IN_FLIGHT = "not finished: a gate is running right now"
NEVER_GATED = "not finished: stopped and no gate ever ran"

GRACE_UNGATED_S = 90
BUDGET_GATE_S = 20 * 60

RENEW = "renew"
RECLAIM = "reclaim"
NEEDS_REVIEW = "needs-review"
ALERT = "alert"
RESTART_SERVER = "restart-server"
NOTE = "note"
VERIFY = "verify"
MERGE = "merge"


@dataclass
class Action:
    kind: str
    task: str | None = None
    reason: str = ""
    detail: str = ""
    project: str | None = None


@dataclass
class Snapshot:

    now: int
    server_running: bool
    server_unresponsive: bool
    tasks: list[Task] = field(default_factory=list)
    facts: dict[str, observe.TaskFact] = field(default_factory=dict)
    dispatch_alive: dict[int, bool] = field(default_factory=dict)
    quiet_for: dict[str, int] = field(default_factory=dict)
    router_outcome: dict[str, str | None] = field(default_factory=dict)
    done_tasks: list[Task] = field(default_factory=list)


def source_fingerprint() -> str:
    h = hashlib.sha256()
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(f.name.encode())
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:12]


def project_label(project: str) -> str:
    p = Path(project)
    return p.parent.name if p.name == ".git" else p.name


def stop_reason(outcome: str | None) -> str:
    if outcome == "running":
        return GATE_IN_FLIGHT
    if outcome == "passed":
        return FINISHED_COMPLETE
    if outcome == "stuck":
        return FINISHED_GAVE_UP
    if outcome in ("failed", "gate-modified"):
        return STILL_RETRYING
    return NEVER_GATED


def decide(snap: Snapshot) -> list[Action]:
    actions: list[Action] = []

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
            actions.append(Action(
                NEEDS_REVIEW, t.name,
                "the shared server died while this task was running",
                "its worktree may hold partial work; re-dispatch is a decision, "
                "not a default",
                project=t.project,
            ))
        return actions

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
            actions.append(Action(RENEW, t.name, "dispatch process is alive",
                                  project=t.project))
            continue

        if fact is not None and fact.state == observe.IDLE:
            why = stop_reason(snap.router_outcome.get(t.worktree or ""))

            if why == STILL_RETRYING:
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

            if why == GATE_IN_FLIGHT:
                actions.append(Action(RENEW, t.name, why, project=t.project))
                if quiet > BUDGET_GATE_S:
                    actions.append(Action(
                        NEEDS_REVIEW, t.name,
                        f"a gate has been running for {quiet}s",
                        "past the gate's own timeout, so the run itself is wedged or "
                        "the server hosting it died mid-check",
                        project=t.project,
                    ))
                continue

            if why == NEVER_GATED:
                if quiet <= GRACE_UNGATED_S:
                    actions.append(Action(
                        RENEW, t.name,
                        f"no in-loop verdict yet, {quiet}s into the grace period",
                        project=t.project,
                    ))
                    continue
                actions.append(Action(
                    NEEDS_REVIEW, t.name, why,
                    f"no in-loop verdict after {quiet}s, so the router did not fire "
                    f"here: check that it is installed and the gate is committed",
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

    for t in snap.done_tasks:
        actions.append(Action(
            MERGE, t.name,
            "done: merge into base and clean up",
            project=t.project,
        ))

    return actions


class Supervisor:
    def __init__(self, project: str | None = None, store: Store | None = None,
                 dry_run: bool = False) -> None:
        self.project = project
        self.store = store or Store()
        self.dry_run = dry_run
        self._last_state: dict[str, tuple[str, int]] = {}

    def snapshot(self) -> Snapshot:
        st = server.status()
        tasks_all = self.store.tasks(self.project, status=RUNNING)
        done_tasks = self.store.tasks(self.project, status=DONE)

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
            done_tasks=done_tasks,
        )

    def apply(self, actions: list[Action]) -> list[str]:
        log: list[str] = []
        for a in actions:
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
                rid = a.detail.rsplit("[", 1)[-1].rstrip("]") if "[" in a.detail else a.detail
                if self.store.note_alert(rid, "", a.task, a.reason, a.detail):
                    log.append(f"  -> new alert {rid} (would notify)")
            elif a.kind == VERIFY and a.task and proj:
                for line in self.verify(a.task, project=proj, why=a.reason):
                    log.append(f"  -> {line}")
            elif a.kind == MERGE and a.task and proj:
                r = merge.merge_and_cleanup(self.store, proj, a.task)
                for s in r.steps:
                    log.append(f"  -> {s}")
                for p in r.problems:
                    log.append(f"  -> ! {p}")
            elif a.kind == RESTART_SERVER:
                try:
                    info = server.restart()
                    log.append(f"  -> restarted shared server pid={info.pid} {info.url}")
                except (RuntimeError, TimeoutError, OSError) as e:
                    log.append(f"  -> could not restart the shared server: {e}")
        return log

    def verify(self, name: str, event: str = "session-idle",
               project: str | None = None, why: str = "") -> list[str]:
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


UNIT_NAME = "orch-daemon.service"

UNIT = """\
[Unit]
Description=orch supervisor - renews leases, verifies stopped agents, restarts the shared server
Documentation=https://github.com/AceCodePt/opencode-orchestrator
After=network-online.target

[Service]
Type=simple
ExecStart={orch} daemon run --interval {interval:g}
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


def _run_config_cmd(cmd: list[str], out: list[str]) -> None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            out.append(f"ok: {' '.join(cmd)}")
        else:
            out.append(f"FAILED: {' '.join(cmd)}: "
                       f"{(p.stderr or p.stdout).strip()[:200]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        out.append(f"FAILED: {' '.join(cmd)}: {e}")


def install(interval: float = 10.0, enable: bool = True) -> list[str]:
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
        _run_config_cmd(cmd, out)
    return out


def uninstall(wipe_state: bool = True) -> list[str]:
    """Remove every config-level trace of orch. Never touches a project's
    tracked `.orchestration/` or the router plugin in a project's `.opencode/`;
    those belong to the project, and only the project removes them."""
    out: list[str] = []
    path = unit_path()

    if path.exists():
        out.append(f"removing {path}")
        for cmd in (
            ["systemctl", "--user", "stop", UNIT_NAME],
            ["systemctl", "--user", "disable", UNIT_NAME],
            ["systemctl", "--user", "daemon-reload"],
        ):
            _run_config_cmd(cmd, out)
        try:
            path.unlink()
            out.append(f"removed {path}")
        except OSError as e:
            out.append(f"FAILED: remove {path}: {e}")
    else:
        out.append(f"no unit at {path}")

    _run_config_cmd(["loginctl", "disable-linger", os.environ.get("USER") or ""], out)

    if server.current() is not None:
        killed = server.stop()
        out.append("stopped the shared server" if killed else "no shared server running")
    else:
        out.append("no shared server running")

    state = server.state_dir()
    if wipe_state and state.exists():
        import shutil

        shutil.rmtree(state)
        out.append(f"removed state {state}")
    elif state.exists():
        out.append(f"kept state {state} (--keep-state)")
    else:
        out.append("no state to remove")

    out.append("left every project's .orchestration/ and router plugin alone")
    return out


def unit_status() -> dict:
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
