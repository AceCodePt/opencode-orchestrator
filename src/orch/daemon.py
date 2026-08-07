from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, gate, merge, observe, server, state
from .harness import prompt_async

SETTLE_BUDGET_S = 30
STALL_BUDGET_S = 10 * 60
RETRIGGER_GRACE_S = 30
REVIEW_BACKOFF_S = 15 * 60
MERGE_BACKOFF_S = 30 * 60
GATE_TIMEOUT_S = 20 * 60
DISPATCH_BACKOFF_S = 60
MAX_ORCHESTRATED_S = 6 * 3600

RESTART_SERVER = "restart-server"
NEEDS_REVIEW = "needs-review"
MERGE = "merge"
REMOVE = "remove"
VERIFY = "verify"
DISPATCH = "dispatch"
RESOLVE = "resolve"
ALERT = "alert"
NOTE = "note"


@dataclass
class Action:
    kind: str
    task: str | None = None
    reason: str = ""
    detail: str = ""
    project: str | None = None
    worktree: str | None = None
    branch: str | None = None
    base: str | None = None
    retry_cap: int = 3
    port: int | None = None
    session_ids: list[str] = field(default_factory=list)
    blocked: list[observe.Blocked] = field(default_factory=list)
    start: str | None = None


@dataclass
class LoopState:
    retries: dict[tuple[str, str], int] = field(default_factory=dict)
    fingerprint: dict[tuple[str, str], str] = field(default_factory=dict)
    last_fail: dict[tuple[str, str], int] = field(default_factory=dict)
    injected_at: dict[tuple[str, str], int] = field(default_factory=dict)
    settled_at: dict[tuple[str, str], int] = field(default_factory=dict)
    wait_until: dict[tuple[str, str], int] = field(default_factory=dict)
    gating_until: dict[tuple[str, str], int] = field(default_factory=dict)
    alerted: set[str] = field(default_factory=set)
    trusted_alerted: set[str] = field(default_factory=set)
    dispatch_backoff: dict[tuple[str, str], int] = field(default_factory=dict)
    dispatch_error: dict[tuple[str, str], str] = field(default_factory=dict)
    dispatch_seen: dict[tuple[str, str], int] = field(default_factory=dict)
    dispatch_escaped: set[tuple[str, str]] = field(default_factory=set)
    resolve_retries: dict[tuple[str, str], int] = field(default_factory=dict)
    resolve_at: dict[tuple[str, str], int] = field(default_factory=dict)


@dataclass
class Snapshot:

    now: int
    server_running: bool
    server_unresponsive: bool
    port: int | None
    tasks: list[state.TaskState]
    mem: LoopState
    policies: dict[str, state.SchedulerPolicy] = field(default_factory=dict)
    trusted: set[str] = field(default_factory=set)


def project_label(project: str) -> str:
    p = Path(project)
    return p.parent.name if p.name == ".git" else p.name


def _reset_loop(mem: LoopState, key: tuple[str, str],
                keep: tuple[str, ...] = ()) -> None:
    for name in ("settled_at", "fingerprint", "last_fail", "injected_at",
                 "gating_until"):
        if name not in keep:
            getattr(mem, name).pop(key, None)


def decide(snap: Snapshot) -> list[Action]:
    actions: list[Action] = []
    mem = snap.mem

    if not snap.server_running or snap.server_unresponsive:
        why = ("is not running" if not snap.server_running
               else "is alive but answers nothing")
        actions.append(Action(
            RESTART_SERVER, None,
            f"the shared server {why}",
            "every dispatched task lives inside this one process, so its death "
            "takes them all with it",
        ))
        return actions

    untrusted = sorted({t.project for t in snap.tasks
                        if t.project not in snap.trusted})
    for proj in untrusted:
        if proj in mem.trusted_alerted:
            continue
        mem.trusted_alerted.add(proj)
        actions.append(Action(
            ALERT, None, "project-not-trusted",
            f"supervised but not trusted; run `orch project trust` from any "
            f"worktree of this project to let the daemon gate, merge and "
            f"dispatch here", project=proj,
        ))

    for t in snap.tasks:
        if not t.claimed:
            continue
        if t.project not in snap.trusted:
            continue
        key = (t.project, t.name)

        if t.session == state.SESSION_BLOCKED:
            for b in t.blocked:
                if b.request_id in mem.alerted:
                    continue
                actions.append(Action(
                    ALERT, t.name, f"blocked on {b.kind}",
                    f"{b.detail} [{b.request_id}]", project=t.project,
                    blocked=[b],
                ))
            actions.append(Action(NOTE, t.name, "blocked but alive",
                                  project=t.project))
            _reset_loop(mem, key)
            continue

        if t.session == state.SESSION_BUSY:
            _reset_loop(mem, key)
            continue

        if t.dispatch_orchestrated:
            mem.dispatch_seen.setdefault(key, snap.now)
            if (key not in mem.dispatch_escaped
                    and snap.now - mem.dispatch_seen[key] < MAX_ORCHESTRATED_S):
                _reset_loop(mem, key)
                continue
            mem.dispatch_escaped.add(key)
            mem.dispatch_seen.pop(key, None)
        else:
            mem.dispatch_seen.pop(key, None)
            mem.dispatch_escaped.discard(key)

        if not t.worktree or not os.path.isdir(t.worktree):
            actions.append(Action(
                NEEDS_REVIEW, t.name, "worktree-gone",
                "the worktree recorded for this branch does not exist",
                project=t.project, worktree=t.worktree,
                branch=t.branch, base=t.base,
            ))
            mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S
            _reset_loop(mem, key, keep=("wait_until",))
            continue

        if t.conflicted:
            if t.hitl:
                actions.append(Action(
                    NEEDS_REVIEW, t.name, "merge-conflict",
                    "the merge into base conflicts and is left in progress; resolve it "
                    "in the base worktree (fix the files, git add, git commit). The "
                    "branch is kept so the work is not lost.",
                    project=t.project, worktree=t.worktree,
                    branch=t.branch, base=t.base,
                ))
                mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S
                _reset_loop(mem, key, keep=("wait_until",))
                continue

            base_wt = state._base_worktree(Path(t.project), t.base)
            res = state.resolution_pid(str(base_wt), t.name) if base_wt else None
            if res is not None:
                _reset_loop(mem, key)
                continue
            if snap.now - mem.resolve_at.get(key, 0) < DISPATCH_BACKOFF_S:
                _reset_loop(mem, key)
                continue
            if state._base_merging(Path(t.project), t.base):
                retries = mem.resolve_retries.get(key, 0)
                if retries >= t.retry_cap:
                    actions.append(Action(
                        NEEDS_REVIEW, t.name, "merge-conflict",
                        f"auto-resolution failed after {retries} attempt(s); resolve it "
                        "in the base worktree (fix the files, git add, git commit)",
                        project=t.project, worktree=t.worktree,
                        branch=t.branch, base=t.base,
                    ))
                    mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S
                    _reset_loop(mem, key, keep=("wait_until",))
                    continue
                actions.append(Action(
                    RESOLVE, t.name,
                    "merge-conflict: dispatching the AI to resolve it in base",
                    project=t.project, base=t.base,
                    start=str(base_wt) if base_wt else None,
                    retry_cap=t.retry_cap,
                ))
                mem.resolve_at[key] = snap.now
                mem.resolve_retries[key] = retries + 1
                _reset_loop(mem, key)
                continue

            mem.resolve_retries.pop(key, None)
            mem.resolve_at.pop(key, None)
            _reset_loop(mem, key)
            continue

        if snap.now < mem.wait_until.get(key, 0):
            continue

        if key not in mem.settled_at:
            mem.settled_at[key] = snap.now
            continue
        if snap.now - mem.settled_at[key] < SETTLE_BUDGET_S:
            continue

        if not t.dirty and not t.diff:
            actions.append(Action(
                REMOVE, t.name, "no-op: clean branch identical to base",
                "nothing was produced, so there is nothing to merge or verify",
                project=t.project, worktree=t.worktree,
                branch=t.branch, base=t.base,
            ))
            _reset_loop(mem, key)
            continue

        fp = state.fingerprint(t.worktree)
        if mem.fingerprint.get(key) == fp:
            last = mem.last_fail.get(key, 0)
            if snap.now - last > STALL_BUDGET_S:
                actions.append(Action(
                    NEEDS_REVIEW, t.name,
                    f"stalled: unchanged for {snap.now - last}s after a gate failure",
                    "the agent was told what to fix and nothing changed",
                    project=t.project, worktree=t.worktree,
                    branch=t.branch, base=t.base,
                ))
                mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S
                _reset_loop(mem, key, keep=("wait_until",))
            continue

        if snap.now < mem.gating_until.get(key, 0):
            continue

        actions.append(Action(
            VERIFY, t.name, "settled: run the committed gate",
            project=t.project, worktree=t.worktree, branch=t.branch,
            base=t.base, retry_cap=t.retry_cap, port=snap.port,
            session_ids=t.session_ids,
        ))
        mem.gating_until[key] = snap.now + GATE_TIMEOUT_S
        mem.settled_at.pop(key, None)

    _schedule(snap, actions)
    return actions


def _schedule(snap: Snapshot, actions: list[Action]) -> None:
    mem = snap.mem
    by_project: dict[str, list[state.TaskState]] = {}
    for t in snap.tasks:
        by_project.setdefault(t.project, []).append(t)

    for project, tasks in by_project.items():
        if project not in snap.trusted:
            continue
        pol = snap.policies.get(project)
        if not pol or not pol.enabled or not pol.start:
            continue
        if pol.max_concurrent <= 0:
            continue
        in_flight = sum(1 for t in tasks if t.claimed and not t.archived)
        for t in tasks:
            if in_flight >= pol.max_concurrent:
                break
            if t.claimed or not t.declared or t.archived or t.hitl:
                continue
            key = (project, t.name)
            if snap.now < mem.dispatch_backoff.get(key, 0):
                continue
            actions.append(Action(
                DISPATCH, t.name, "todo: declared on base, no claim",
                detail="auto-dispatched by the scheduler",
                project=project, base=t.base,
                start=str(pol.start),
            ))
            in_flight += 1


class Supervisor:
    def __init__(self, project: str | None = None, roots: list[str] | None = None,
                 dry_run: bool = False) -> None:
        self.project = project
        self.roots = roots
        self.dry_run = dry_run
        self.mem = LoopState()
        self._projects: dict[str, Path] = {}
        self._projects_at = 0.0
        self._verify_log: list[str] = []

    def _discover(self) -> dict[str, Path]:
        now = time.monotonic()
        if not self._projects or now - self._projects_at > 60:
            found = state.discover_projects(self.roots)
            if self.project:
                filtered = {k: v for k, v in found.items() if k == self.project}
                if not filtered:
                    proj = state.Project.from_common(self.project)
                    if proj is not None and proj.orch_dir is not None:
                        filtered = {str(proj.common): proj.orch_dir}
                found = filtered
            self._projects = found
            self._projects_at = now
        return self._projects

    def snapshot(self) -> Snapshot:
        st = server.status()
        port = (st.get("port") if st.get("running")
                and not st.get("unresponsive") else None)

        tasks: list[state.TaskState] = []
        policies: dict[str, state.SchedulerPolicy] = {}
        for common, orch in self._discover().items():
            proj = state.Project(Path(common), orch,
                                 state.base_from_config(orch))
            tasks.extend(proj.scan(port))
            pol = state.scheduler_from_config(orch)
            if pol.enabled:
                pol.start = proj.base_worktree
            policies[str(common)] = pol

        return Snapshot(
            now=int(time.time()),
            server_running=bool(st.get("running")),
            server_unresponsive=bool(st.get("unresponsive")),
            port=port,
            tasks=tasks,
            mem=self.mem,
            policies=policies,
            trusted=state.trusted_projects(),
        )

    def _verify(self, a: Action) -> None:
        key = (a.project or "", a.task or "")
        self.mem.gating_until.pop(key, None)
        log: list[str] = []
        wt = Path(a.worktree or "")
        sid = a.session_ids[0] if a.session_ids else ""

        v = gate.run(wt, "session-idle", base=a.base or "main",
                     session_id=sid)
        log.append(f"gate {v.summary}")

        if v.outcome == gate.NO_GATE:
            log.append(f"{a.task} -> needs-review (no committed gate, so nothing "
                       f"verified this work)")
            self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
            _reset_loop(self.mem, key, keep=("wait_until",))
            self._verify_log = log
            return

        if v.justifies_done:
            self.mem.retries.pop(key, None)
            self.mem.fingerprint.pop(key, None)
            self.mem.last_fail.pop(key, None)
            self.mem.settled_at.pop(key, None)
            if state._dirty(wt):
                log.append("gate passed but the worktree is still dirty; "
                           "reviewing rather than merging")
                self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
                self._verify_log = log
                return
            if not state._diff(Path(a.project or ""), a.base or "main",
                               a.branch or ""):
                r = merge.remove_task(Path(a.project or ""), a.task or "",
                                      a.branch or "", wt, a.base or "main")
                for s in r.steps:
                    log.append(s)
                for p in r.problems:
                    log.append(f"! {p}")
                if not r.ok:
                    self.mem.wait_until[key] = int(time.time()) + MERGE_BACKOFF_S
            else:
                r = merge.merge_and_cleanup(Path(a.project or ""), a.task or "",
                                            a.branch or "", wt, a.base or "main")
                for s in r.steps:
                    log.append(s)
                for p in r.problems:
                    log.append(f"! {p}")
                if not r.ok:
                    self.mem.wait_until[key] = int(time.time()) + MERGE_BACKOFF_S
            self._verify_log = log
            return

        retries = self.mem.retries.get(key, 0) + 1
        self.mem.retries[key] = retries
        self.mem.fingerprint[key] = state.fingerprint(wt)
        self.mem.last_fail[key] = int(time.time())
        self.mem.injected_at[key] = int(time.time())
        cap = a.retry_cap or 3

        if retries >= cap:
            log.append(f"{a.task} -> needs-review (retries-exhausted "
                       f"{retries}/{cap})")
            self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
            self.mem.fingerprint.pop(key, None)
            self._verify_log = log
            return

        if not a.session_ids:
            log.append(f"{a.task} -> needs-review (gate failed, no session left "
                       f"to retrigger)")
            self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
            self._verify_log = log
            return

        text = (
            f"The `session-idle` gate failed (attempt {retries}/{cap}). "
            f"Fix these, then continue:\n\n```\n{v.detail}\n```"
        )
        if not a.port:
            log.append(f"{a.task} -> needs-review (no server to inject into)")
            self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
            self._verify_log = log
            return
        try:
            prompt_async(a.port, a.session_ids[0], text)
            log.append(f"injected failure (attempt {retries}/{cap}) into "
                       f"session {a.session_ids[0]}")
        except Exception as e:  # noqa: BLE001
            log.append(f"could not inject: {e}")
            self.mem.wait_until[key] = int(time.time()) + RETRIGGER_GRACE_S
        self._verify_log = log

    def _dispatch(self, a: Action) -> list[str]:
        key = (a.project or "", a.task or "")
        log: list[str] = []
        if not a.start:
            log.append(f"-> {a.task}: no base worktree to dispatch from")
            self.mem.dispatch_backoff[key] = int(time.time()) + DISPATCH_BACKOFF_S
            return log
        r = dispatch.start(Path(a.start), a.task or "")
        for s in r.steps:
            log.append(s)
        if r.ok:
            self.mem.dispatch_backoff.pop(key, None)
            self.mem.dispatch_error.pop(key, None)
        else:
            for p in r.problems:
                log.append(f"! {p}")
            self.mem.dispatch_backoff[key] = int(time.time()) + DISPATCH_BACKOFF_S
            self.mem.dispatch_error[key] = (r.problems[-1] if r.problems
                                            else "dispatch failed")
        return log

    def _resolve(self, a: Action) -> list[str]:
        key = (a.project or "", a.task or "")
        log: list[str] = []
        if not a.start:
            log.append(f"-> {a.task}: no base worktree to resolve in; retrying later")
            return log
        r = dispatch.start_resolve(Path(a.start), a.task or "")
        for s in r.steps:
            log.append(s)
        if not r.ok:
            for p in r.problems:
                log.append(f"! {p}")
            self.mem.resolve_at[key] = int(time.time())
        return log

    def apply(self, actions: list[Action]) -> list[str]:
        log: list[str] = []
        for a in actions:
            where = f"[{project_label(a.project)}] " if a.project else ""
            log.append(f"{a.kind}: {where}{a.task or '-'} - {a.reason}"
                       + (f" ({a.detail})" if a.detail else ""))
            if self.dry_run:
                continue
            if a.kind == RESTART_SERVER:
                try:
                    info = server.restart()
                    log.append(f"  -> restarted shared server pid={info.pid} {info.url}")
                except (RuntimeError, TimeoutError, OSError) as e:
                    log.append(f"  -> could not restart the shared server: {e}")
            elif a.kind == ALERT:
                for b in a.blocked:
                    if b.request_id in self.mem.alerted:
                        continue
                    self.mem.alerted.add(b.request_id)
                    log.append(f"  -> alert {b.request_id}: {b.kind} {b.detail} "
                               f"(would notify)")
            elif a.kind == NEEDS_REVIEW:
                log.append(f"  -> {a.task} needs-review ({a.reason})")
            elif a.kind == DISPATCH and a.project:
                log.extend(f"  -> {l}" for l in self._dispatch(a))
            elif a.kind == RESOLVE and a.project:
                log.extend(f"  -> {l}" for l in self._resolve(a))
            elif a.kind == VERIFY:
                self._verify(a)
                log.extend(f"  -> {l}" for l in self._verify_log)
            elif a.kind == MERGE and a.project:
                r = merge.merge_and_cleanup(Path(a.project), a.task or "",
                                            a.branch or "", Path(a.worktree or ""),
                                            a.base or "main")
                for s in r.steps:
                    log.append(f"  -> {s}")
                for p in r.problems:
                    log.append(f"  -> ! {p}")
                if not r.ok:
                    self.mem.wait_until[(a.project, a.task or "")] = \
                        int(time.time()) + MERGE_BACKOFF_S
            elif a.kind == REMOVE and a.project:
                r = merge.remove_task(Path(a.project), a.task or "",
                                      a.branch or "", Path(a.worktree or ""),
                                      a.base or "main")
                for s in r.steps:
                    log.append(f"  -> {s}")
                for p in r.problems:
                    log.append(f"  -> ! {p}")
        return log

    def step(self) -> list[str]:
        return self.apply(decide(self.snapshot()))


def run(project: str | None = None, roots: list[str] | None = None,
        interval: float = 10.0, once: bool = False,
        dry_run: bool = False) -> int:
    sup = Supervisor(project=project, roots=roots, dry_run=dry_run)
    scope = project_label(project) if project else "every discovered project"
    print(f"orch daemon: supervising {scope}, every {interval:g}s", flush=True)
    try:
        while True:
            for line in sup.step():
                print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


UNIT_NAME = "orch-daemon.service"
UNIT = """\
[Unit]
Description=orch supervisor - settles tasks, runs gates, retriggers agents, merges and archives
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


OPENCODE_DENY_RULES = {
    "**/.orchestration/**": "deny",
    "$HOME/.config/opencode/opencode.json": "deny",
}


def opencode_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opencode" / "opencode.json"


def install_opencode_deny() -> tuple[bool, str]:
    """Merge deny rules for `.orchestration/**` into the machine-level opencode
    config, so no project agent can edit its own orchestration through the
    edit/write/patch tools. Appended last: permission rules are evaluated in
    order with the last matching rule winning."""
    p = opencode_config_path()
    cfg: dict = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError):
            return False, f"could not read {p}; leaving it untouched"
        if not isinstance(raw, dict):
            return False, f"{p} is not a JSON object; leaving it untouched"
        cfg = raw
    perm = cfg.get("permission")
    if not isinstance(perm, dict):
        perm = {}
    edit = perm.get("edit")
    if not isinstance(edit, dict):
        edit = {}
    for key in OPENCODE_DENY_RULES:
        edit.pop(key, None)
    for key, val in OPENCODE_DENY_RULES.items():
        edit[key] = val
    perm["edit"] = edit
    cfg["permission"] = perm
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    return True, f"deny rules for .orchestration/** written to {p}"


def install(interval: float = 10.0, enable: bool = True) -> list[str]:
    out: list[str] = []
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(interval))
    out.append(f"wrote {path}")

    ok, msg = install_opencode_deny()
    out.append(msg)

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
    tracked `.orchestration/`; that belongs to the project, and only the
    project removes it."""
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

    state_dir = server.state_dir()
    if wipe_state and state_dir.exists():
        import shutil

        shutil.rmtree(state_dir)
        out.append(f"removed state {state_dir}")
    elif state_dir.exists():
        out.append(f"kept state {state_dir} (--keep-state)")
    else:
        out.append("no state to remove")

    out.append("left every project's .orchestration/ alone")
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
