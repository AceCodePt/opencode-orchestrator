from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, hooks, harness, merge, observe, server, sse, state, verify
from .harness import prompt_async

SETTLE_BUDGET_S = 30
STALL_BUDGET_S = 10 * 60
RETRIGGER_GRACE_S = 30
REVIEW_BACKOFF_S = 15 * 60
MERGE_BACKOFF_S = 30 * 60
VERIFY_TIMEOUT_S = 20 * 60
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
CLEAR_ORPHANED = "clear-orphaned"


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
    wait_human_merge: bool = False
    orphaned: list[dict] = field(default_factory=list)


@dataclass
class LoopState:
    retries: dict[tuple[str, str], int] = field(default_factory=dict)
    fingerprint: dict[tuple[str, str], str] = field(default_factory=dict)
    last_fail: dict[tuple[str, str], int] = field(default_factory=dict)
    injected_at: dict[tuple[str, str], int] = field(default_factory=dict)
    settled_at: dict[tuple[str, str], int] = field(default_factory=dict)
    wait_until: dict[tuple[str, str], int] = field(default_factory=dict)
    verifying_until: dict[tuple[str, str], int] = field(default_factory=dict)
    alerted: set[str] = field(default_factory=set)
    cleared: set[str] = field(default_factory=set)
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
    global_max: int | None = None
    settle_budget: int = SETTLE_BUDGET_S


def project_label(project: str) -> str:
    p = Path(project)
    name = p.parent.name if p.name == ".git" else p.name
    return name[:-4] if name.endswith(".git") else name


def _commit_dirty(worktree: Path, message: str) -> None:
    """Commit whatever the agent left uncommitted, with the orch identity.
    Used only when there is no verification hook to do the commit."""
    if not state._dirty(worktree):
        return
    subprocess.run(["git", "-C", str(worktree), "add", "-A"],
                   capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(worktree), "-c", "user.name=orch",
         "-c", "user.email=orch@localhost", "commit", "-q", "-m", message],
        capture_output=True, text=True,
    )


def _hook_env(a: Action, extra: dict[str, str] | None = None) -> dict[str, str]:
    env: dict[str, str] = {"ORCH_ACTION": a.kind}
    if a.project:
        env["PROJECT_DIR"] = a.project
        env["PROJECT_NAME"] = project_label(a.project)
    if a.task:
        env["TASK_NAME"] = a.task
    if a.branch:
        env["BRANCH_NAME"] = a.branch
    if a.worktree:
        env["WORKTREE_PATH"] = a.worktree
        env["WORKTREE_NAME"] = Path(a.worktree).name
    if a.base:
        env["BASE_BRANCH"] = a.base
    if a.reason:
        env["ORCH_REASON"] = a.reason
    if a.detail:
        env["ORCH_DETAIL"] = a.detail
    if extra:
        env.update(extra)
    return env


def _hook_cwd(a: Action) -> Path | None:
    if a.worktree and os.path.isdir(a.worktree):
        return Path(a.worktree)
    if a.project and a.base:
        bwt = state._base_worktree(Path(a.project), a.base)
        if bwt is not None:
            return bwt
    return None


def _reset_loop(mem: LoopState, key: tuple[str, str],
                keep: tuple[str, ...] = ()) -> None:
    for name in ("settled_at", "fingerprint", "last_fail", "injected_at",
                 "verifying_until"):
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
            f"worktree of this project to let the daemon verify, merge and "
            f"dispatch here", project=proj,
        ))

    for t in snap.tasks:
        if not t.claimed:
            continue
        if t.project not in snap.trusted:
            continue
        key = (t.project, t.name)

        if t.orphaned:
            fresh = [a for a in t.orphaned
                     if str(a.get("id") or a.get("requestID") or "") not in mem.cleared]
            if fresh:
                actions.append(Action(
                    CLEAR_ORPHANED, t.name,
                    "orphaned ask(s) from an aborted turn",
                    project=t.project, worktree=t.worktree, branch=t.branch,
                    base=t.base, port=snap.port, orphaned=fresh,
                ))

        if t.interrupted:
            if t.dirty or t.diff:
                actions.append(Action(
                    NEEDS_REVIEW, t.name, "interrupted",
                    "the task's turn was aborted (a user stop, or the turn "
                    "died); its work is held for you to verify",
                    project=t.project, worktree=t.worktree, branch=t.branch,
                    base=t.base,
                ))
                mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S
                _reset_loop(mem, key, keep=("wait_until",))
                continue
            # no work produced: fall through to the normal no-op close

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
            if t.wait_human_start:
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
            # A backoff (e.g. from a failed merge) must not freeze a clean,
            # already-merged task: its branch is an ancestor of base, so there is
            # nothing left to re-attempt and the no-op sweep is idempotent.
            if not t.dirty and not t.diff:
                mem.wait_until.pop(key, None)
            else:
                continue

        if key not in mem.settled_at:
            mem.settled_at[key] = snap.now
            continue
        if snap.now - mem.settled_at[key] < snap.settle_budget:
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

        if t.wait_human_merge and not t.dirty and t.diff:
            # clean, committed, waiting for the human's `orch task merge`
            continue

        fp = state.fingerprint(t.worktree)
        if mem.fingerprint.get(key) == fp:
            last = mem.last_fail.get(key, 0)
            if snap.now - last > STALL_BUDGET_S:
                actions.append(Action(
                    NEEDS_REVIEW, t.name,
                    f"stalled: unchanged for {snap.now - last}s after a verification failure",
                    "the agent was told what to fix and nothing changed",
                    project=t.project, worktree=t.worktree,
                    branch=t.branch, base=t.base,
                ))
                mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S
                _reset_loop(mem, key, keep=("wait_until",))
            continue

        if snap.now < mem.verifying_until.get(key, 0):
            continue

        actions.append(Action(
            VERIFY, t.name, "settled: run the committed verification hook",
            project=t.project, worktree=t.worktree, branch=t.branch,
            base=t.base, retry_cap=t.retry_cap, port=snap.port,
            session_ids=t.session_ids,
            wait_human_merge=t.wait_human_merge,
        ))
        mem.verifying_until[key] = snap.now + VERIFY_TIMEOUT_S
        mem.settled_at.pop(key, None)

    _schedule(snap, actions)
    return actions


def _schedule(snap: Snapshot, actions: list[Action]) -> None:
    mem = snap.mem
    by_project: dict[str, list[state.TaskState]] = {}
    for t in snap.tasks:
        by_project.setdefault(t.project, []).append(t)

    global_in_flight = sum(
        1 for t in snap.tasks if t.claimed and not t.archived
    )
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
            if snap.global_max is not None and \
                    global_in_flight >= snap.global_max:
                return
            if t.claimed or not t.declared or t.archived or t.wait_human_start:
                continue
            key = (project, t.name)
            if snap.now < mem.dispatch_backoff.get(key, 0):
                continue
            if not _deps_satisfied(tasks, t):
                continue
            actions.append(Action(
                DISPATCH, t.name, "todo: declared on base, no claim",
                detail="auto-dispatched by the scheduler",
                project=project, base=t.base,
                start=str(pol.start),
            ))
            in_flight += 1
            global_in_flight += 1


def _deps_satisfied(tasks: list[state.TaskState], t: state.TaskState) -> bool:
    """A todo task dispatches only when every dependency is archived (or was
    never declared — a missing slug cannot block). An open task with the same
    name, or one reset back to `todo`, leaves the dependency unsatisfied."""
    if not t.dependencies:
        return True
    archived = {x.name for x in tasks if x.archived}
    declared_open = {x.name for x in tasks if x.declared and not x.archived}
    for dep in t.dependencies:
        if dep in archived:
            continue
        if dep not in declared_open:
            continue
        return False
    return True


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
        self._session_map: dict[str, tuple[str, str, str]] = {}
        self._watcher: sse.EventWatcher | None = None

    def start_event_watcher(self) -> None:
        self._watcher = sse.EventWatcher(resolver=self._session_owner)
        self._watcher.start()

    def stop_event_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()

    def _session_owner(self, session_id: str) -> tuple[str, str, str] | None:
        return self._session_map.get(session_id)

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
        session_map: dict[str, tuple[str, str, str]] = {}
        for common, orch in self._discover().items():
            proj = state.Project(Path(common), orch,
                                 state.base_from_config(orch))
            proj_tasks = proj.scan(port)
            tasks.extend(proj_tasks)
            for t in proj_tasks:
                if t.worktree:
                    for sid in t.session_ids:
                        session_map.setdefault(sid, (common, proj.base, t.worktree))
            pol = state.scheduler_from_config(
                state.committed_config(common, proj.base))
            if pol.enabled:
                pol.start = proj.base_worktree
            policies[str(common)] = pol
        self._session_map = session_map

        return Snapshot(
            now=int(time.time()),
            server_running=bool(st.get("running")),
            server_unresponsive=bool(st.get("unresponsive")),
            port=port,
            tasks=tasks,
            mem=self.mem,
            policies=policies,
            trusted=state.trusted_projects(),
            global_max=state.machine_budget(),
            settle_budget=state.machine_settle_budget() or SETTLE_BUDGET_S,
        )

    def _verify(self, a: Action) -> None:
        key = (a.project or "", a.task or "")
        self.mem.verifying_until.pop(key, None)
        log: list[str] = []
        wt = Path(a.worktree or "")
        sid = a.session_ids[0] if a.session_ids else ""

        v = verify.run(wt, "session-idle", base=a.base or "main",
                       session_id=sid)
        log.append(f"verify {v.summary}")

        if v.outcome == verify.NO_VERIFY:
            log.append(f"{a.task} -> no verification hook committed; the work is "
                       f"accepted without verification")
            _commit_dirty(wt, "orch: accepted without verification")
        elif not v.justifies_done:
            retries = self.mem.retries.get(key, 0) + 1
            self.mem.retries[key] = retries
            self.mem.fingerprint[key] = state.fingerprint(wt)
            self.mem.last_fail[key] = int(time.time())
            self.mem.injected_at[key] = int(time.time())
            cap = a.retry_cap or 3

            if retries >= cap:
                log.append(f"{a.task} -> needs-review (retries-exhausted "
                           f"{retries}/{cap})")
                log.extend(self._fire_hook("needs-review", _hook_env(a), _hook_cwd(a)))
                self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
                self.mem.fingerprint.pop(key, None)
                self._verify_log = log
                return

            if not a.session_ids:
                log.append(f"{a.task} -> needs-review (verification failed, no session left "
                           f"to retrigger)")
                log.extend(self._fire_hook("needs-review", _hook_env(a), _hook_cwd(a)))
                self.mem.wait_until[key] = int(time.time()) + REVIEW_BACKOFF_S
                self._verify_log = log
                return

            text = (
                f"The `session-idle` verification hook failed (attempt {retries}/{cap}). "
                f"Fix these, then continue:\n\n```\n{v.detail}\n```"
            )
            if not a.port:
                log.append(f"{a.task} -> needs-review (no server to inject into)")
                log.extend(self._fire_hook("needs-review", _hook_env(a), _hook_cwd(a)))
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
            return

        self.mem.retries.pop(key, None)
        self.mem.fingerprint.pop(key, None)
        self.mem.last_fail.pop(key, None)
        self.mem.settled_at.pop(key, None)
        if state._dirty(wt):
            log.append("the work is still uncommitted after acceptance; "
                       "reviewing rather than merging")
            log.extend(self._fire_hook("needs-review", _hook_env(a), _hook_cwd(a)))
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
                log.extend(self._fire_hook("removed", _hook_env(a), _hook_cwd(a)))
            self._verify_log = log
            return
        if a.wait_human_merge:
            log.append(f"{a.task} -> awaiting your merge (wait_human_merge)")
            self._verify_log = log
            return
        r = merge.merge_and_cleanup(Path(a.project or ""), a.task or "",
                                    a.branch or "", wt, a.base or "main")
        for s in r.steps:
            log.append(s)
        for p in r.problems:
            log.append(f"! {p}")
        if not r.ok:
            self.mem.wait_until[key] = int(time.time()) + MERGE_BACKOFF_S
        else:
            log.extend(self._fire_hook("merged", _hook_env(a), _hook_cwd(a)))
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
            log.extend(self._fire_hook("dispatched", _hook_env(a), _hook_cwd(a)))
        else:
            for p in r.problems:
                log.append(f"! {p}")
            self.mem.dispatch_backoff[key] = int(time.time()) + DISPATCH_BACKOFF_S
            self.mem.dispatch_error[key] = (r.problems[-1] if r.problems
                                            else "dispatch failed")
            log.extend(self._fire_hook("dispatch-failed", _hook_env(a),
                                       _hook_cwd(a)))
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
            log.extend(self._fire_hook("resolve-failed", _hook_env(a),
                                       _hook_cwd(a)))
        return log

    def _fire_hook(self, event: str, env: dict[str, str],
                   cwd: Path | None = None) -> list[str]:
        env = {**env, "ORCH_EVENT": event}
        try:
            pid = hooks.run(event, env, cwd)
        except OSError as e:
            return [f"global hook {event} could not spawn: {e}"]
        if pid is not None:
            return [f"fired global hook {event} pid={pid}"]
        return []

    def _hook_log(self, event: str, env: dict[str, str],
                  cwd: Path | None = None) -> list[str]:
        return [f"  -> {l}" for l in self._fire_hook(event, env, cwd)]

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
                    log.extend(self._hook_log("restart-server", _hook_env(a)))
                except (RuntimeError, TimeoutError, OSError) as e:
                    log.append(f"  -> could not restart the shared server: {e}")
            elif a.kind == ALERT:
                if not a.blocked:
                    log.append(f"  -> alert: {a.reason} ({a.detail})")
                    log.extend(self._hook_log("alert", _hook_env(a), _hook_cwd(a)))
                for b in a.blocked:
                    if b.request_id in self.mem.alerted:
                        continue
                    self.mem.alerted.add(b.request_id)
                    log.append(f"  -> alert {b.request_id}: {b.kind} {b.detail}")
                    env = _hook_env(a, {
                        "ORCH_REQUEST_ID": b.request_id,
                        "ORCH_SESSION_ID": b.session_id,
                        "ORCH_KIND": b.kind,
                    })
                    log.extend(self._hook_log("alert", env, _hook_cwd(a)))
            elif a.kind == NEEDS_REVIEW:
                log.append(f"  -> {a.task} needs-review ({a.reason})")
                log.extend(self._hook_log("needs-review", _hook_env(a), _hook_cwd(a)))
            elif a.kind == CLEAR_ORPHANED and a.port and a.worktree:
                for ask in a.orphaned:
                    rid = str(ask.get("id") or ask.get("requestID") or "")
                    if not rid or rid in self.mem.cleared:
                        continue
                    try:
                        harness.reply_permission(a.port, rid, "reject",
                                                 directory=a.worktree)
                        self.mem.cleared.add(rid)
                        log.append(f"  -> cleared orphaned ask {rid}")
                    except Exception as e:  # noqa: BLE001
                        log.append(f"  -> could not clear orphaned ask {rid}: {e}")
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
                if r.ok:
                    log.extend(self._hook_log("merged", _hook_env(a), _hook_cwd(a)))
                else:
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
                if r.ok:
                    log.extend(self._hook_log("removed", _hook_env(a), _hook_cwd(a)))
        return log

    def step(self) -> list[str]:
        return self.apply(decide(self.snapshot()))


def run(project: str | None = None, roots: list[str] | None = None,
        interval: float = 1.0, once: bool = False,
        dry_run: bool = False, sse: bool = True) -> int:
    sup = Supervisor(project=project, roots=roots, dry_run=dry_run)
    scope = project_label(project) if project else "every discovered project"
    print(f"orch daemon: supervising {scope}, every {interval:g}s", flush=True)
    if sse and not once and not dry_run:
        sup.start_event_watcher()
        print("orch daemon: listening on the shared server's /event stream for "
              "per-event hooks", flush=True)
    try:
        while True:
            for line in sup.step():
                print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            if sup._watcher is not None:
                for line in list(sup._watcher.lines):
                    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
                sup._watcher.lines.clear()
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sup.stop_event_watcher()


UNIT_NAME = "orch-daemon.service"
UNIT = """\
[Unit]
Description=orch supervisor - settles tasks, runs verification hooks, retriggers agents, merges and archives
Documentation=https://github.com/AceCodePt/opencode-orchestrator
After=network-online.target

[Service]
Type=simple
# The daemon must be able to (re)start the shared server, which spawns
# `opencode`. systemd's default PATH does not include the user's shell PATH
# (e.g. ~/.opencode/bin), so the install-time environment is baked in. This
# also carries ORCH_ROOTS when the owner set it, so supervised roots survive
# an install.
{env}
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


def _dedup_path(path: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for entry in path.split(":"):
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return ":".join(out)


def unit_text(interval: float = 1.0) -> str:
    env = {"HOME": os.environ.get("HOME") or str(Path.home()),
           "PATH": _dedup_path(os.environ.get("PATH")
                               or "/usr/local/bin:/usr/bin:/bin")}
    roots = os.environ.get("ORCH_ROOTS")
    if roots:
        env["ORCH_ROOTS"] = roots
    lines = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    return UNIT.format(orch=server.orch_bin(), interval=interval, env=lines)


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


def install(interval: float = 1.0, enable: bool = True) -> list[str]:
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
