from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import daemon, observe, server, state, verify  # noqa: E402

NOW = 1_700_000_000
_WT_DIR = tempfile.mkdtemp(prefix="orch-lab-")
WT = _WT_DIR


def _task(**kw) -> state.TaskState:
    base = dict(project="/tmp/lab/lab.git", name="task-a", base="main",
                retry_cap=3, declared=True, claimed=True, branch="task-a",
                worktree=WT, dirty=False, diff=True, session=state.SESSION_NONE)
    base.update(kw)
    return state.TaskState(**base)  # type: ignore[arg-type]


def _snap(**kw) -> daemon.Snapshot:
    base = dict(now=NOW, server_running=True, server_unresponsive=False,
                port=4096, tasks=[_task()], mem=daemon.LoopState())
    base.update(kw)
    base.setdefault("trusted", {t.project for t in base["tasks"]})  # type: ignore[index]
    return daemon.Snapshot(**base)  # type: ignore[arg-type]


def _kinds(actions: list[daemon.Action]) -> list[str]:
    return [a.kind for a in actions]


def test_dead_server_is_restarted_and_nothing_else_is_decided() -> None:
    acts = daemon.decide(_snap(server_running=False))
    assert _kinds(acts) == [daemon.RESTART_SERVER]
    print("ok: a dead shared server is restarted, tasks are left alone")


def test_busy_session_needs_no_action() -> None:
    acts = daemon.decide(_snap(tasks=[_task(session=state.SESSION_BUSY)]))
    assert acts == []
    print("ok: a busy session is left alone")


def _hook_env_saved() -> dict[str, str | None]:
    return {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}


def _hook_env_restore(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _install_hook(hooks_dir: Path, name: str, marker: Path, extra: str = "") -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    p = hooks_dir / name
    p.write_text("#!/usr/bin/env bash\n"
                 f"{extra}"
                 f"echo \"$ORCH_EVENT:$ORCH_ACTION:$PROJECT_NAME:$TASK_NAME\" "
                 f"> {marker}\n")
    p.chmod(0o755)


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_blocked_session_alerts_once() -> None:
    b = observe.Blocked(kind="external_directory", request_id="per_1",
                        session_id="ses_1", detail="/etc/*")
    sup = daemon.Supervisor(dry_run=False)
    mem = sup.mem
    acts = daemon.decide(_snap(
        tasks=[_task(session=state.SESSION_BLOCKED, blocked=[b])], mem=mem))
    assert daemon.ALERT in _kinds(acts)
    sup.apply(acts)  # apply marks the request id as alerted
    acts2 = daemon.decide(_snap(
        tasks=[_task(session=state.SESSION_BLOCKED, blocked=[b])], mem=mem))
    assert daemon.ALERT not in _kinds(acts2), "the alert must dedupe by request id"
    print("ok: a blocked agent is alerted once, deduped by request id")


def test_alert_fires_the_global_hook_once() -> None:
    saved = _hook_env_saved()
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        marker = Path(tmp.name) / "marker"
        _install_hook(Path(tmp.name) / "orch" / "hooks", "alert", marker)
        b = observe.Blocked(kind="external_directory", request_id="per_1",
                            session_id="ses_1", detail="/etc/*")
        sup = daemon.Supervisor(dry_run=False)
        mem = sup.mem
        acts = daemon.decide(_snap(
            tasks=[_task(session=state.SESSION_BLOCKED, blocked=[b])], mem=mem))
        sup.apply(acts)
        sup.apply(acts)  # deduped: must not fire twice
        assert _wait_for(marker)
        assert marker.read_text().strip() == "alert:alert:lab:task-a"
        assert marker.read_text().strip().count("\n") == 0, "one fire only"
    finally:
        tmp.cleanup()
        _hook_env_restore(saved)
    print("ok: a blocked session fires the global alert hook exactly once")


def test_needs_review_fires_the_global_hook() -> None:
    saved = _hook_env_saved()
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        marker = Path(tmp.name) / "marker"
        _install_hook(Path(tmp.name) / "orch" / "hooks", "needs-review", marker)
        sup = daemon.Supervisor(dry_run=False)
        mem = sup.mem
        t = _task(session=state.SESSION_NONE, worktree=None, dirty=False, diff=False)
        acts = daemon.decide(_snap(tasks=[t], mem=mem, now=NOW + 1000))
        assert daemon.NEEDS_REVIEW in _kinds(acts)
        sup.apply(acts)
        assert _wait_for(marker)
        assert marker.read_text().strip() == "needs-review:needs-review:lab:task-a"
    finally:
        tmp.cleanup()
        _hook_env_restore(saved)
    print("ok: a needs-review escalation fires the global needs-review hook")


def test_dry_run_never_fires_hooks() -> None:
    saved = _hook_env_saved()
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        marker = Path(tmp.name) / "marker"
        _install_hook(Path(tmp.name) / "orch" / "hooks", "alert", marker)
        b = observe.Blocked(kind="external_directory", request_id="per_1",
                            session_id="ses_1", detail="/etc/*")
        sup = daemon.Supervisor(dry_run=True)
        mem = sup.mem
        acts = daemon.decide(_snap(
            tasks=[_task(session=state.SESSION_BLOCKED, blocked=[b])], mem=mem))
        sup.apply(acts)
        time.sleep(0.3)
        assert not marker.exists(), "dry-run must not fire hooks"
    finally:
        tmp.cleanup()
        _hook_env_restore(saved)
    print("ok: --dry-run skips global hooks")


def test_bad_hook_script_is_fail_open() -> None:
    saved = _hook_env_saved()
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        hooks_dir = Path(tmp.name) / "orch" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        p = hooks_dir / "alert"
        p.write_text("#!/no/such/interpreter\necho x\n")
        p.chmod(0o755)
        b = observe.Blocked(kind="external_directory", request_id="per_1",
                            session_id="ses_1", detail="/etc/*")
        sup = daemon.Supervisor(dry_run=False)
        mem = sup.mem
        acts = daemon.decide(_snap(
            tasks=[_task(session=state.SESSION_BLOCKED, blocked=[b])], mem=mem))
        log = sup.apply(acts)  # must not raise
        assert any("could not spawn" in l for l in log)
    finally:
        tmp.cleanup()
        _hook_env_restore(saved)
    print("ok: a hook that cannot spawn is logged fail-open, the loop continues")


def test_newly_settled_task_waits_out_the_budget() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE)
    acts = daemon.decide(_snap(tasks=[t], mem=mem))
    assert acts == [], "the first settle observation must only start the timer"
    assert daemon.VERIFY not in _kinds(acts)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S - 1, tasks=[t], mem=mem))
    assert daemon.VERIFY not in _kinds(acts), "before the budget no action"
    print("ok: a freshly settled task waits before being acted on")


def test_machine_settle_budget_is_honored_per_pass() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=True)
    snap = _snap(tasks=[t], mem=mem, now=NOW)
    daemon.decide(snap)
    acts = daemon.decide(_snap(tasks=[t], mem=mem, now=NOW + 5,
                               settle_budget=5))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    acts = daemon.decide(_snap(tasks=[t], mem=mem, now=NOW + 5,
                               settle_budget=30))
    assert acts == [], "a longer configured budget must still hold the task"
    print("ok: the machine settleBudget is read every pass, no restart needed")


def test_settled_dirty_task_is_verified() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    assert acts[0].retry_cap == 3
    print("ok: a settled dirty task is verified (the verification hook decides commit vs retry)")


def test_settled_clean_diff_task_is_verified() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY]
    print("ok: a settled clean+diff task is verified")


def test_settled_clean_noop_is_held_for_review() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=False)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert daemon.REMOVE not in _kinds(acts)
    print("ok: a clean no-diff task is held for review, not removed")


def test_missing_worktree_needs_review() -> None:
    mem = daemon.LoopState()
    acts = daemon.decide(_snap(tasks=[_task(worktree=None)], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW]
    print("ok: a claimed task with no worktree needs review")


def test_missing_worktree_is_not_re_reported_every_tick() -> None:
    # A vanished worktree cannot repair itself; without the review backoff the
    # daemon would append the same needs-review line on every tick, flooding the
    # log (hundreds of thousands of lines a day).
    mem = daemon.LoopState()
    t = _task(worktree=None)
    first = daemon.decide(_snap(tasks=[t], mem=mem))
    assert _kinds(first) == [daemon.NEEDS_REVIEW], _kinds(first)

    for tick in range(1, 60):
        acts = daemon.decide(_snap(now=NOW + tick, tasks=[t], mem=mem))
        assert acts == [], acts

    # once the backoff expires it surfaces again, so a human is not forgotten
    later = daemon.decide(_snap(now=NOW + daemon.REVIEW_BACKOFF_S + 1,
                                tasks=[t], mem=mem))
    assert _kinds(later) == [daemon.NEEDS_REVIEW], _kinds(later)
    print("ok: a vanished worktree is reported once, not every tick")


def test_interrupted_with_work_is_not_re_reported_every_tick() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, interrupted=True, diff=True)
    first = daemon.decide(_snap(tasks=[t], mem=mem))
    assert _kinds(first) == [daemon.NEEDS_REVIEW], _kinds(first)

    for tick in range(1, 60):
        acts = daemon.decide(_snap(now=NOW + tick, tasks=[t], mem=mem))
        assert acts == [], acts
    print("ok: an interrupted task is held for review once, not every tick")


def test_interrupted_with_work_is_held_for_review() -> None:
    acts = daemon.decide(_snap(tasks=[_task(session=state.SESSION_IDLE,
                                            interrupted=True, diff=True)]))
    kinds = _kinds(acts)
    assert kinds == [daemon.NEEDS_REVIEW], kinds
    assert acts[0].reason == "interrupted"
    print("ok: an interrupted task with work is held for review, never merged")


def test_interrupted_noop_is_held_for_review() -> None:
    mem = daemon.LoopState()
    key = ("/tmp/lab/lab.git", "task-a")
    mem.settled_at[key] = NOW - daemon.SETTLE_BUDGET_S - 1
    t = _task(session=state.SESSION_IDLE, interrupted=True,
              dirty=False, diff=False)
    acts = daemon.decide(_snap(tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert daemon.REMOVE not in _kinds(acts)
    print("ok: an interrupt with no produced work is held for review, not removed")


def test_claimed_no_commit_task_is_held_and_never_removed() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=False)
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert daemon.REMOVE not in _kinds(acts), _kinds(acts)
    print("ok: a claimed task whose branch committed nothing is held, not removed")


def test_held_no_commit_task_honours_the_review_backoff() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=False)
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    base = NOW + daemon.SETTLE_BUDGET_S + 1
    first = daemon.decide(_snap(now=base, tasks=[t], mem=mem))
    assert _kinds(first) == [daemon.NEEDS_REVIEW], _kinds(first)
    for tick in range(1, daemon.REVIEW_BACKOFF_S):
        acts = daemon.decide(_snap(now=base + tick, tasks=[t], mem=mem))
        assert acts == [], acts
    later = daemon.decide(_snap(now=base + daemon.REVIEW_BACKOFF_S + 1,
                                tasks=[t], mem=mem))
    assert _kinds(later) == [daemon.NEEDS_REVIEW], _kinds(later)
    print("ok: a held no-commit task is reported once per review backoff")


def test_claimed_clean_diff_task_still_verifies() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=True)
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    assert daemon.NEEDS_REVIEW not in _kinds(acts), _kinds(acts)
    print("ok: a committed, diffing task still goes to verification")


def test_claimed_dirty_task_still_verifies_not_held() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=True, diff=False)
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    assert daemon.NEEDS_REVIEW not in _kinds(acts), _kinds(acts)
    print("ok: a dirty task's existing path is untouched (still verified)")


def test_orphaned_asks_emit_a_clear_action() -> None:
    t = _task(session=state.SESSION_IDLE,
              orphaned=[{"id": "per_1", "sessionID": "ses_1"}])
    acts = daemon.decide(_snap(tasks=[t]))
    kinds = _kinds(acts)
    assert daemon.CLEAR_ORPHANED in kinds, kinds
    clear = [a for a in acts if a.kind == daemon.CLEAR_ORPHANED][0]
    assert clear.orphaned[0]["id"] == "per_1"
    assert clear.port == 4096
    print("ok: orphaned asks emit a clear action")


def test_daemon_log_is_capped() -> None:
    # A per-tick logging bug must not fill the disk: the persistent log is
    # trimmed once it passes the cap, keeping the most recent entries.
    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name) / "daemon.log"
    p.write_bytes(b"x" * (daemon.MAX_LOG_BYTES + 1))
    daemon._trim_log(p)
    assert p.stat().st_size <= daemon.KEEP_LOG_BYTES + 200, p.stat().st_size
    assert p.read_bytes().startswith(b"--- log trimmed")
    print("ok: an oversized daemon log is trimmed, not left to grow unbounded")


def test_apply_clears_orphaned_asks() -> None:
    calls: list[tuple] = []
    orig = daemon.harness.reply_permission
    daemon.harness.reply_permission = lambda port, sid, rid, reply, message=None: \
        (calls.append((port, sid, rid, reply)) or {})
    try:
        sup = daemon.Supervisor(dry_run=False)
        a = daemon.Action(daemon.CLEAR_ORPHANED, "task-a",
                          "orphaned ask from an aborted turn",
                          project="/tmp/lab/lab.git", worktree=WT,
                          branch="task-a", base="main", port=4096,
                          orphaned=[{"id": "per_1", "sessionID": "ses_1"}])
        log = sup.apply([a])
        assert any("cleared orphaned ask per_1" in l for l in log), log
        assert calls == [(4096, "ses_1", "per_1", "reject")], calls
        assert "per_1" in sup.mem.cleared
    finally:
        daemon.harness.reply_permission = orig
    print("ok: the daemon replies reject to clear an orphaned ask")


def test_wait_backoff_blocks_repeated_actions() -> None:
    mem = daemon.LoopState()
    mem.wait_until[("/tmp/lab/lab.git", "task-a")] = NOW + 9999
    snap = _snap(tasks=[_task(session=state.SESSION_IDLE, dirty=True)], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(snap)
    assert acts == [], "a task in backoff must not be acted on"
    print("ok: a task in backoff is left alone")


def test_backoff_holds_a_clean_noop_for_review() -> None:
    # A clean branch identical to base is no longer swept automatically, so a
    # stale backoff must hold it (not be spent every tick) and the task must
    # still reach review once the backoff expires.
    mem = daemon.LoopState()
    key = ("/tmp/lab/lab.git", "task-a")
    mem.wait_until[key] = NOW + 9999
    mem.settled_at[key] = NOW - daemon.SETTLE_BUDGET_S - 1
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=False)
    acts = daemon.decide(_snap(tasks=[t], mem=mem))
    assert acts == [], acts
    later = daemon.decide(_snap(now=NOW + 10000, tasks=[t], mem=mem))
    assert _kinds(later) == [daemon.NEEDS_REVIEW], _kinds(later)
    print("ok: a backoff holds a clean no-op until it expires, then it is reviewed")


def test_unchanged_worktree_after_failure_stalls_to_review() -> None:
    mem = daemon.LoopState()
    key = ("/tmp/lab/lab.git", "task-a")
    mem.fingerprint[key] = state.fingerprint(WT)
    mem.last_fail[key] = NOW - daemon.STALL_BUDGET_S - 1
    t = _task(session=state.SESSION_IDLE, dirty=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW]
    assert "stalled" in acts[0].reason
    print("ok: an unchanged worktree after a verification failure eventually stalls to review")


def test_unchanged_but_recently_failed_keeps_waiting() -> None:
    mem = daemon.LoopState()
    key = ("/tmp/lab/lab.git", "task-a")
    mem.fingerprint[key] = state.fingerprint(WT)
    mem.last_fail[key] = NOW - 10
    t = _task(session=state.SESSION_IDLE, dirty=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert acts == [], "recently injected and unchanged: keep waiting"
    print("ok: a recently injected task that is unchanged keeps waiting")


def test_a_non_orchestrated_attach_is_ignored_for_settling() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=True, dispatch_alive=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    print("ok: a human-attached client (in a terminal) does not block settling - "
          "the session state is the truth")


def test_orchestrated_dispatch_holds_settling_until_the_process_exits() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=True, dispatch_alive=True,
              dispatch_orchestrated=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [], \
        "an orchestrated `opencode run` is the agent working; hold settling"
    t.dispatch_orchestrated = False
    acts = daemon.decide(_snap(now=NOW + 2 * daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [], "releasing the process restarts the settle timer"
    acts = daemon.decide(_snap(now=NOW + 3 * daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    print("ok: an orchestrated run process holds settling, and releasing it "
          "settles and verifies")


def test_lingering_orchestrated_process_escapes_after_the_cap() -> None:
    mem = daemon.LoopState()
    key = ("/tmp/lab/lab.git", "task-a")
    mem.dispatch_seen[key] = NOW - daemon.MAX_ORCHESTRATED_S - 1
    t = _task(session=state.SESSION_IDLE, dirty=True, dispatch_alive=True,
              dispatch_orchestrated=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], \
        "a run client stuck alive past the cap must not stall the task forever"
    print("ok: an orchestrated client alive past MAX_ORCHESTRATED_S is escaped")


def test_only_claimed_tasks_are_supervised() -> None:
    acts = daemon.decide(_snap(tasks=[_task(claimed=False)]))
    assert acts == []
    print("ok: todo and archived tasks are not supervised")


def test_untrusted_projects_only_alert() -> None:
    t = _task(session=state.SESSION_IDLE, dirty=True, diff=True)
    acts = daemon.decide(_snap(tasks=[t], trusted=set()))
    assert _kinds(acts) == [daemon.ALERT], _kinds(acts)
    assert acts[0].reason == "project-not-trusted", acts[0].reason

    mem = daemon.LoopState()
    daemon.decide(_snap(tasks=[t], trusted={t.project}, mem=mem))
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], trusted={t.project}, mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    print("ok: an untrusted project only alerts; trusting it settles and verifies")


def test_untrusted_projects_are_not_scheduled() -> None:
    t = _task(claimed=False, declared=True, wait_human_start=False)
    snap = _snap(tasks=[t], trusted=set(),
                 policies={t.project: state.SchedulerPolicy(
                     enabled=True, max_concurrent=1, start=Path(WT))})
    acts = daemon.decide(snap)
    assert daemon.DISPATCH not in _kinds(acts), _kinds(acts)
    print("ok: the scheduler never dispatches an untrusted project")


def test_install_opencode_deny_merges_and_is_idempotent() -> None:
    saved = os.environ.get("XDG_CONFIG_HOME")
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    try:
        p = daemon.opencode_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"permission": {"edit": {"*": "ask"}},
                                 "other": True}))
        ok, _ = daemon.install_opencode_deny()
        assert ok
        cfg = json.loads(p.read_text())
        edit = cfg["permission"]["edit"]
        assert edit["**/.orchestration/**"] == "deny"
        assert edit["$HOME/.config/opencode/opencode.json"] == "deny"
        assert edit["*"] == "ask", "existing rules must survive the merge"
        assert cfg["other"] is True
        daemon.install_opencode_deny()
        cfg = json.loads(p.read_text())
        assert cfg["permission"]["edit"]["**/.orchestration/**"] == "deny"
    finally:
        tmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved
    print("ok: install merges the .orchestration deny into the global opencode "
          "config, idempotently")


def test_install_opencode_deny_refuses_unparseable_config() -> None:
    saved = os.environ.get("XDG_CONFIG_HOME")
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    try:
        p = daemon.opencode_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json{")
        ok, msg = daemon.install_opencode_deny()
        assert not ok and "untouched" in msg
        assert p.read_text() == "not json{"
    finally:
        tmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved
    print("ok: an unparseable opencode config is never clobbered")


def _verified_repo(hook_body: str) -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    for cmd in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *cmd, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    h = root / ".orchestration" / "hooks"
    h.mkdir(parents=True)
    (h / "session-idle").write_text(hook_body)
    (h / "session-idle").chmod(0o755)
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "main", "verify": {"retryCap": 3}}\n')
    (root / "seed").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True)
    return root, tmp


def test_apply_verify_merges_a_passing_hook_and_archives() -> None:
    root, tmp = _verified_repo(
        "#!/usr/bin/env bash\n"
        "cd \"${WORKTREE_PATH:-.}\" || exit 1\n"
        "if [ -n \"$(git status --porcelain)\" ]; then\n"
        "  git add -A\n"
        "  git -c user.name=orch -c user.email=orch@localhost commit -q -m g\n"
        "fi\n"
        "exit 0\n")
    # create the task on base, then a claimed branch with committed work
    (root / "tasks" / "task-a").mkdir(parents=True)
    (root / "tasks" / "task-a" / "task.md").write_text("# Task A\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "task"], check=True)

    wt = root.parent / "task-a"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "task-a",
                    str(wt), "main"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    (wt / "answer.txt").write_text("42")
    # dirty work, the hook will pass and commit it

    mem = daemon.LoopState()
    snap = _snap(now=int(time.time()),
                 tasks=[_task(project=str(root), worktree=str(wt),
                              session=state.SESSION_IDLE, dirty=True)],
                 mem=mem)
    sup = daemon.Supervisor(dry_run=False)
    # point the snapshot's task at this repo
    snap.tasks[0].project = str(root)
    snap.tasks[0].worktree = str(wt)
    snap.tasks[0].branch = "task-a"
    acts = [daemon.Action(daemon.VERIFY, "task-a", "settled",
                          project=str(root), worktree=str(wt), branch="task-a",
                          base="main", retry_cap=3, session_ids=[])]
    log = sup.apply(acts)
    joined = "\n".join(log)
    assert "merged" in joined or "archived" in joined, joined
    # the branch should be gone
    p = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "-q",
                        "refs/heads/task-a"], capture_output=True)
    assert p.returncode != 0, "the task branch must be merged and deleted"
    assert not wt.exists(), "the worktree must be removed"
    tmp.cleanup()
    print("ok: a passing verification hook merges, archives, and tears the task down")


def test_apply_verify_failing_hook_retriggers_up_to_the_cap() -> None:
    root, tmp = _verified_repo("#!/usr/bin/env bash\necho 'tests red'\nexit 1\n")
    injected: list[tuple[int, str, str]] = []
    orig = daemon.prompt_async
    daemon.prompt_async = lambda port, sid, text: injected.append((port, sid, text)) or None
    try:
        sup = daemon.Supervisor(dry_run=False)
        a = daemon.Action(daemon.VERIFY, "task-a", "settled",
                          project=str(root), worktree=str(root), branch="task-a",
                          base="main", retry_cap=3, port=4096, session_ids=["ses_1"])
        sup.apply([a])
        assert len(injected) == 1, "a failing verification hook must inject the failure back"
        assert "tests red" in injected[0][2]

        a2 = daemon.Action(daemon.VERIFY, "task-a", "settled",
                           project=str(root), worktree=str(root), branch="task-a",
                           base="main", retry_cap=3, port=4096, session_ids=["ses_1"])
        sup.apply([a2])
        assert len(injected) == 2, "attempt 2 still retriggers"
        a3 = daemon.Action(daemon.VERIFY, "task-a", "settled",
                           project=str(root), worktree=str(root), branch="task-a",
                           base="main", retry_cap=3, port=4096, session_ids=["ses_1"])
        log = "\n".join(sup.apply([a3]))
        assert "retries-exhausted" in log, log
        assert len(injected) == 2, "the cap must stop retriggering"
    finally:
        daemon.prompt_async = orig
    tmp.cleanup()
    print("ok: a failing verification hook retriggers up to the cap, then needs-review")


def test_apply_verify_no_hook_accepts_implicitly() -> None:
    root, tmp = _verified_repo("#!/usr/bin/env bash\nexit 0\n")
    # no committed verification hook: remove the committed hook and commit
    subprocess.run(["git", "-C", str(root), "rm", "-q",
                    ".orchestration/hooks/session-idle"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "no verify"],
                   check=True)
    (root / "tasks" / "task-a").mkdir(parents=True)
    (root / "tasks" / "task-a" / "task.md").write_text("# Task A\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "task"], check=True)

    wt = root.parent / "task-a"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "task-a",
                    str(wt), "main"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    (wt / "answer.txt").write_text("42")

    sup = daemon.Supervisor(dry_run=False)
    a = daemon.Action(daemon.VERIFY, "task-a", "settled",
                      project=str(root), worktree=str(wt), branch="task-a",
                      base="main", retry_cap=3, session_ids=[])
    log = "\n".join(sup.apply([a]))
    assert "without verification" in log, log
    assert "merged" in log or "archived" in log, log
    p = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "-q",
                        "refs/heads/task-a"], capture_output=True)
    assert p.returncode != 0, "the task branch must be merged and deleted"
    assert not wt.exists(), "the worktree must be removed"
    tmp.cleanup()
    print("ok: with no verification hook the daemon accepts the work implicitly and merges")


def test_apply_verify_wait_human_merge_leaves_the_branch() -> None:
    root, tmp = _verified_repo(
        "#!/usr/bin/env bash\n"
        "cd \"${WORKTREE_PATH:-.}\" || exit 1\n"
        "if [ -n \"$(git status --porcelain)\" ]; then\n"
        "  git add -A\n"
        "  git -c user.name=orch -c user.email=orch@localhost commit -q -m g\n"
        "fi\n"
        "exit 0\n")
    (root / "tasks" / "task-a").mkdir(parents=True)
    (root / "tasks" / "task-a" / "task.md").write_text("# Task A\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "task"], check=True)

    wt = root.parent / "task-a"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "task-a",
                    str(wt), "main"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    (wt / "answer.txt").write_text("42")

    sup = daemon.Supervisor(dry_run=False)
    a = daemon.Action(daemon.VERIFY, "task-a", "settled",
                      project=str(root), worktree=str(wt), branch="task-a",
                      base="main", retry_cap=3, session_ids=[],
                      wait_human_merge=True)
    log = "\n".join(sup.apply([a]))
    assert "awaiting your merge" in log, log
    assert "merged" not in log, log
    p = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "-q",
                        "refs/heads/task-a"], capture_output=True)
    assert p.returncode == 0, "the branch must survive awaiting the human's merge"
    assert wt.exists(), "the worktree must survive"
    tmp.cleanup()
    print("ok: wait_human_merge leaves the verified branch awaiting the human")


def test_decide_skips_reverification_when_waiting_for_human_merge() -> None:
    mem = daemon.LoopState()
    t = _task(claimed=True, worktree=WT, branch="task-a", dirty=False, diff=True,
              session=state.SESSION_IDLE, wait_human_merge=True)
    mem.settled_at[(t.project, t.name)] = NOW
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1, tasks=[t],
                               mem=mem))
    assert daemon.VERIFY not in _kinds(acts), _kinds(acts)
    print("ok: a clean+diff task waiting for a human merge is not re-verified")


def test_unit_text_keeps_the_expected_shape() -> None:
    text = daemon.unit_text(interval=10)
    assert "Restart=always" in text
    assert "KillMode=process" in text
    assert "daemon run --interval 10" in text
    print("ok: the systemd unit restarts always and keeps the server alive")


def test_unit_text_bakes_the_install_time_environment() -> None:
    import os
    saved = dict(os.environ)
    try:
        os.environ["HOME"] = "/home/tester"
        os.environ["PATH"] = "/home/tester/.opencode/bin:/usr/bin:/bin"
        os.environ["ORCH_ROOTS"] = "/home/tester,/srv/lab"
        text = daemon.unit_text(interval=10)
        assert "Environment=HOME=/home/tester" in text
        assert "Environment=PATH=/home/tester/.opencode/bin:/usr/bin:/bin" in text
        assert "Environment=ORCH_ROOTS=/home/tester,/srv/lab" in text
        os.environ.pop("ORCH_ROOTS")
        assert "Environment=ORCH_ROOTS=" not in daemon.unit_text(interval=10), \
            "an unset ORCH_ROOTS must fall back to the daemon's own defaults"
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("ok: the unit carries the daemon's opencode PATH and supervised roots")


def test_scheduler_dispatches_a_declared_todo() -> None:
    mem = daemon.LoopState()
    todo = _task(claimed=False, branch=None, worktree=None)
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[todo], mem=mem,
                               policies={todo.project: pol}))
    assert _kinds(acts) == [daemon.DISPATCH], _kinds(acts)
    assert acts[0].start == "/tmp/lab"
    print("ok: a declared todo is auto-dispatched when the scheduler is on")


def test_scheduler_is_opt_in() -> None:
    todo = _task(claimed=False, branch=None, worktree=None)
    off = state.SchedulerPolicy(enabled=False, max_concurrent=1,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[todo],
                               policies={todo.project: off}))
    assert daemon.DISPATCH not in _kinds(acts), "disabled means never dispatch"
    acts = daemon.decide(_snap(tasks=[todo]))
    assert daemon.DISPATCH not in _kinds(acts), "no policy means never dispatch"
    print("ok: the scheduler only acts when a project opts in")


def test_scheduler_respects_max_concurrent() -> None:
    mem = daemon.LoopState()
    claimed = _task(claimed=True, branch="task-a", worktree=WT,
                    session=state.SESSION_NONE, dirty=False, diff=True)
    todo = _task(name="task-b", claimed=False, branch=None, worktree=None)
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[claimed, todo], mem=mem,
                               policies={claimed.project: pol}))
    assert daemon.DISPATCH not in _kinds(acts), \
        "one slot is held by the claimed task"
    print("ok: concurrency is counted from claimed (in-flight) tasks")


def test_scheduler_fills_free_slots_in_order() -> None:
    mem = daemon.LoopState()
    claimed = _task(claimed=True, branch="task-a", worktree=WT,
                    session=state.SESSION_NONE, dirty=False, diff=True)
    t1 = _task(name="task-b", claimed=False, branch=None, worktree=None)
    t2 = _task(name="task-c", claimed=False, branch=None, worktree=None)
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=2,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[claimed, t1, t2], mem=mem,
                               policies={claimed.project: pol}))
    dispatched = [a.task for a in acts if a.kind == daemon.DISPATCH]
    assert dispatched == ["task-b"], \
        "one free slot must go to the first todo, in order"
    print("ok: free slots are filled by the next todo in name order")


def test_scheduler_skips_tasks_in_dispatch_backoff() -> None:
    mem = daemon.LoopState()
    key = ("/tmp/lab/lab.git", "task-a")
    mem.dispatch_backoff[key] = NOW + 9999
    todo = _task(claimed=False, branch=None, worktree=None)
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[todo], mem=mem,
                               policies={todo.project: pol}))
    assert daemon.DISPATCH not in _kinds(acts)
    print("ok: a task that failed to dispatch is not retried before backoff")


def test_apply_dispatch_failure_sets_backoff() -> None:
    sup = daemon.Supervisor(dry_run=False)
    a = daemon.Action(daemon.DISPATCH, "task-x", "todo: declared, no claim",
                      project="/tmp/nowhere/x.git", base="main",
                      start="/tmp/nowhere-not-a-worktree")
    log = "\n".join(sup.apply([a]))
    assert "not inside a git worktree" in log, log
    key = ("/tmp/nowhere/x.git", "task-x")
    assert key in sup.mem.dispatch_backoff
    assert sup.mem.dispatch_error[key] != ""
    print("ok: a failed auto-dispatch backs off and records why")


def test_scheduler_skips_wait_human_start_tasks() -> None:
    mem = daemon.LoopState()
    manual = _task(claimed=False, branch=None, worktree=None, wait_human_start=True)
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[manual], mem=mem,
                               policies={manual.project: pol}))
    assert daemon.DISPATCH not in _kinds(acts), \
        "a task that needs a human start is never auto-picked"
    auto = _task(name="task-auto", claimed=False, branch=None, worktree=None,
                 wait_human_start=False)
    acts = daemon.decide(_snap(tasks=[auto], mem=mem, policies={auto.project: pol}))
    assert _kinds(acts) == [daemon.DISPATCH], _kinds(acts)
    print("ok: the scheduler leaves wait_human_start tasks for a human and picks the rest")


def test_scheduler_holds_a_task_until_its_dependencies_are_archived() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=2,
                                start=Path("/tmp/lab"))
    dep_open = _task(name="feature", claimed=False, branch=None, worktree=None,
                     wait_human_start=False)
    slice_ = _task(name="feature-slice", claimed=False, branch=None, worktree=None,
                   wait_human_start=False, dependencies=["feature"])
    acts = daemon.decide(_snap(tasks=[dep_open, slice_], mem=mem,
                               policies={dep_open.project: pol}))
    dispatched = [a.task for a in acts if a.kind == daemon.DISPATCH]
    assert dispatched == ["feature"], \
        "a slice whose dependency is still open must not dispatch"

    archived = _task(name="feature", claimed=False, branch=None, worktree=None,
                     archived=True)
    acts = daemon.decide(_snap(tasks=[archived, slice_], mem=mem,
                               policies={slice_.project: pol}))
    dispatched = [a.task for a in acts if a.kind == daemon.DISPATCH]
    assert dispatched == ["feature-slice"], \
        "once the dependency is archived, the slice dispatches"
    print("ok: the scheduler conditions dispatch on archived dependencies")


def test_scheduler_treats_an_undeclared_dependency_as_satisfied() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    slice_ = _task(name="slice-a", claimed=False, branch=None, worktree=None,
                   wait_human_start=False, dependencies=["never-written"])
    acts = daemon.decide(_snap(tasks=[slice_], mem=mem, policies={slice_.project: pol}))
    assert _kinds(acts) == [daemon.DISPATCH], \
        "a slug that was never declared cannot block - only an open declared task does"
    print("ok: a dependency slug that was never declared does not hold the task")


def test_scheduler_reused_slug_blocks_until_the_new_generation_archives() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=2,
                                start=Path("/tmp/lab"))
    slice_ = _task(name="feature-slice", claimed=False, branch=None, worktree=None,
                   wait_human_start=False, dependencies=["feature"])
    # A reused slug collapses to one task: declared (the fresh generation), not
    # archived - even though an old generation of "feature" sits in archive/.
    reused_open = _task(name="feature", claimed=False, branch=None, worktree=None,
                        wait_human_start=False, declared=True, archived=False)
    acts = daemon.decide(_snap(tasks=[reused_open, slice_], mem=mem,
                               policies={slice_.project: pol}))
    dispatched = [a.task for a in acts if a.kind == daemon.DISPATCH]
    assert dispatched == ["feature"], \
        "a reused slug's old archive must not unblock a dependent while it is open"

    archived = _task(name="feature", claimed=False, branch=None, worktree=None,
                     archived=True, declared=False)
    acts = daemon.decide(_snap(tasks=[archived, slice_], mem=mem,
                               policies={slice_.project: pol}))
    dispatched = [a.task for a in acts if a.kind == daemon.DISPATCH]
    assert dispatched == ["feature-slice"], \
        "once the reused generation archives, the slice dispatches"
    print("ok: an open reused slug blocks dependents until its new generation archives")


def test_global_budget_caps_dispatches_across_projects() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=5,
                                start=Path("/tmp/lab"))
    p1_todo = _task(project="/tmp/lab/p1.git", name="p1-a", claimed=False,
                    branch=None, worktree=None, wait_human_start=False)
    p2_todo = _task(project="/tmp/lab/p2.git", name="p2-a", claimed=False,
                    branch=None, worktree=None, wait_human_start=False)
    p2_todo2 = _task(project="/tmp/lab/p2.git", name="p2-b", claimed=False,
                     branch=None, worktree=None, wait_human_start=False)
    snap = _snap(tasks=[p1_todo, p2_todo, p2_todo2], mem=mem,
                 policies={p1_todo.project: pol, p2_todo.project: pol},
                 global_max=2)
    dispatched = [a.task for a in daemon.decide(snap)
                  if a.kind == daemon.DISPATCH]
    assert len(dispatched) == 2, dispatched
    print("ok: a machine-wide budget caps auto-dispatch across projects")


def test_global_budget_counts_claimed_tasks() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=5,
                                start=Path("/tmp/lab"))
    claimed = _task(project="/tmp/lab/p1.git", name="running", claimed=True,
                    branch="running", worktree=WT, session=state.SESSION_NONE)
    todo = _task(project="/tmp/lab/p2.git", name="p2-a", claimed=False,
                 branch=None, worktree=None, wait_human_start=False)
    snap = _snap(tasks=[claimed, todo], mem=mem,
                 policies={claimed.project: pol, todo.project: pol},
                 global_max=1)
    acts = daemon.decide(snap)
    assert daemon.DISPATCH not in _kinds(acts), \
        "the in-flight claimed task already uses the whole budget"
    print("ok: the global budget is counted from claimed (in-flight) tasks")


def test_global_budget_zero_disables_the_scheduler() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=5,
                                start=Path("/tmp/lab"))
    todo = _task(claimed=False, branch=None, worktree=None, wait_human_start=False)
    snap = _snap(tasks=[todo], mem=mem, policies={todo.project: pol},
                 global_max=0)
    assert daemon.DISPATCH not in _kinds(daemon.decide(snap))
    print("ok: a machine-wide budget of 0 means nothing is auto-dispatched")


def test_wait_human_start_conflicted_task_is_reviewed_not_reverified() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=True, conflicted=True,
              wait_human_start=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert "merge-conflict" in acts[0].reason, acts[0].reason
    assert daemon.VERIFY not in _kinds(acts), \
        "a conflicted task must not be re-verified or re-merged"
    print("ok: a wait_human_start conflicted task is routed to needs-review, never re-verified")


def _conflicted_repo() -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    from orch import merge as merge_mod
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    for cmd in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *cmd, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "seed").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"], check=True)
    orch = root / ".orchestration"
    orch.mkdir(parents=True)
    (orch / "config.json").write_text('{"baseBranch": "main"}\n')
    d = root / "tasks" / "task-a"
    d.mkdir(parents=True)
    (d / "task.md").write_text("---\nwait_human_start: false\n---\n\n# Task A\n\nDo it.\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "task"], check=True)

    wt = root.parent / "task-a"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "task-a",
                    str(wt), "main"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    (wt / "task.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "work"], check=True)
    (root / "task.py").write_text("x = 2\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "conflict"], check=True)
    r = merge_mod.merge_and_cleanup(root, "task-a", "task-a", wt, "main")
    assert r.conflicted, "the merge must be left mid-merge"
    return root, wt, tmp


def test_auto_conflicted_task_dispatches_a_resolution_agent() -> None:
    root, _wt, tmp = _conflicted_repo()
    proj = state.Project.from_dir(root)
    assert proj is not None
    t = next(x for x in proj.scan() if x.name == "task-a")
    assert t.conflicted and not t.wait_human_start, "the task must be auto and conflicted"
    mem = daemon.LoopState()
    snap = _snap(now=NOW, tasks=[t], mem=mem)
    acts = daemon.decide(snap)
    assert _kinds(acts) == [daemon.RESOLVE], _kinds(acts)
    key = (str(t.project), t.name)
    assert snap.mem.resolve_retries.get(key) == 1
    assert acts[0].start == str(proj.base_worktree)
    assert "merge-conflict" in acts[0].reason
    # while the backoff for a fresh resolution is active, do not re-dispatch
    acts2 = daemon.decide(_snap(now=NOW + 1, tasks=[t], mem=mem))
    assert daemon.RESOLVE not in _kinds(acts2), _kinds(acts2)
    tmp.cleanup()
    print("ok: an auto conflicted task dispatches the AI to resolve, once")


def test_auto_resolution_escalates_to_human_after_retries() -> None:
    root, _wt, tmp = _conflicted_repo()
    proj = state.Project.from_dir(root)
    assert proj is not None
    t = next(x for x in proj.scan() if x.name == "task-a")
    key = (str(t.project), t.name)
    mem = daemon.LoopState()
    mem.resolve_at[key] = NOW - daemon.DISPATCH_BACKOFF_S - 1
    mem.resolve_retries[key] = t.retry_cap
    acts = daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert "merge-conflict" in acts[0].reason
    assert "auto-resolution failed" in acts[0].detail
    tmp.cleanup()
    print("ok: exhausted auto-resolution escalates to a human")


def test_an_in_flight_resolver_session_is_detected_and_not_duplicated() -> None:
    root, _wt, tmp = _conflicted_repo()
    proj = state.Project.from_dir(root)
    assert proj is not None
    t = next(x for x in proj.scan() if x.name == "task-a")
    key = (str(t.project), t.name)
    mem = daemon.LoopState()
    mem.resolve_at[key] = NOW - daemon.DISPATCH_BACKOFF_S - 1
    snap = _snap(now=NOW, tasks=[t], mem=mem)
    # a live resolver session titled <task>-merge-conflict exists in the base
    # worktree, so the daemon must not spawn another and must not escalate
    orig = state.observe.harness.sessions
    state.observe.harness.sessions = lambda port, directory=None: [
        {"id": "ses_resolver", "title": "task-a-merge-conflict",
         "directory": directory, "parentID": None}]
    try:
        acts = daemon.decide(snap)
        assert daemon.RESOLVE not in _kinds(acts), _kinds(acts)
        assert daemon.NEEDS_REVIEW not in _kinds(acts), _kinds(acts)
        assert acts == []
    finally:
        state.observe.harness.sessions = orig
    tmp.cleanup()
    print("ok: an in-flight resolver session is detected; no duplicate resolve "
          "and no human escalation")


def test_a_resolver_session_for_a_different_task_does_not_block_this_one() -> None:
    root, _wt, tmp = _conflicted_repo()
    proj = state.Project.from_dir(root)
    assert proj is not None
    t = next(x for x in proj.scan() if x.name == "task-a")
    key = (str(t.project), t.name)
    mem = daemon.LoopState()
    mem.resolve_at[key] = NOW - daemon.DISPATCH_BACKOFF_S - 1
    snap = _snap(now=NOW, tasks=[t], mem=mem)
    # a resolver for a DIFFERENT task is running; it must not block task-a
    orig = state.observe.harness.sessions
    state.observe.harness.sessions = lambda port, directory=None: [
        {"id": "ses_resolver", "title": "task-other-merge-conflict",
         "directory": directory, "parentID": None}]
    try:
        acts = daemon.decide(snap)
        assert _kinds(acts) == [daemon.RESOLVE], _kinds(acts)
    finally:
        state.observe.harness.sessions = orig
    tmp.cleanup()
    print("ok: a resolver for another task never blocks this task's resolve")


def test_auto_resolved_conflict_is_held_for_review() -> None:
    root, wt, tmp = _conflicted_repo()
    proj = state.Project.from_dir(root)
    assert proj is not None
    # the AI resolves: fix the file and commit, completing the merge in base
    (root / "task.py").write_text("x = 3\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "resolve"], check=True)
    assert state._base_merging(root, "main") is None, "the merge must be complete"
    t = next(x for x in proj.scan() if x.name == "task-a")
    assert not t.conflicted, "the scan must no longer report the conflict"
    assert t.status == state.NEEDS_REVIEW, t.status
    mem = daemon.LoopState()
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    # the branch tip is now base's tip, so the daemon can no longer tell the
    # resolved merge from an empty run: hold it for a human instead of archiving.
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert daemon.REMOVE not in _kinds(acts)
    assert daemon.RESOLVE not in _kinds(acts)
    tmp.cleanup()
    print("ok: a resolved conflict is held for review, not silently archived")


def test_clean_task_is_held_while_the_base_is_mid_merge() -> None:
    root, wt, tmp = _conflicted_repo()
    # a second clean merge-ready task, built from the pre-conflict base
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "task-b",
                    str(root.parent / "task-b"), "main"], check=True)
    wtb = root.parent / "task-b"
    subprocess.run(["git", "-C", str(wtb), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(wtb), "config", "user.name", "t"],
                   check=True)
    (wtb / "task_b.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(wtb), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wtb), "commit", "-q", "-m",
                    "work for task-b"], check=True)

    proj = state.Project.from_dir(root)
    assert proj is not None
    assert state._base_merging(root, "main") is not None, \
        "the base must still be mid-merge"
    t = next(x for x in proj.scan() if x.name == "task-b")
    assert not t.conflicted, "task-b is not the conflicted merge"
    assert t.status == state.MERGE_READY, t.status

    mem = daemon.LoopState()
    acts = daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NOTE], _kinds(acts)
    assert "held: base branch is mid-merge" in acts[0].reason, acts[0].reason
    assert daemon.VERIFY not in _kinds(acts), _kinds(acts)
    assert daemon.MERGE not in _kinds(acts), _kinds(acts)

    # still held on the next sweep: no re-notification, no verify/merge
    acts2 = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                                tasks=[t], mem=mem))
    assert _kinds(acts2) == [], _kinds(acts2)

    # resolve the base conflict; the held task resumes its settle-and-verify path
    (root / "task.py").write_text("x = 3\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "resolve"],
                   check=True)
    assert state._base_merging(root, "main") is None, "the merge must be complete"
    t = next(x for x in proj.scan() if x.name == "task-b")
    assert t.status == state.MERGE_READY, t.status
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    acts3 = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                                tasks=[t], mem=mem))
    assert _kinds(acts3) == [daemon.VERIFY], _kinds(acts3)
    tmp.cleanup()
    print("ok: a clean merge-ready task is held (one NOTE, no verify/merge) while "
          "the base is mid-merge and resumes to verify once the base resolves")


def test_snapshot_builds_the_session_to_worktree_map() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp.name) / "cfg")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        sup = daemon.Supervisor()
        orig_status = server.status
        orig_discover = state.discover_projects
        orig_scan = state.Project.scan
        try:
            server.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
            state.discover_projects = lambda _roots=None: {
                "/tmp/lab/lab.git": Path("/tmp/lab/.orchestration")}
            state.Project.scan = lambda self, port=None: [
                state.TaskState(project="/tmp/lab/lab.git", name="task-a",
                                base="main", retry_cap=3, declared=True,
                                claimed=True, branch="task-a",
                                worktree="/tmp/lab/wt",
                                session_ids=["ses_1", "ses_2"])]
            sup.snapshot()
            assert sup._session_owner("ses_1") == \
                ("/tmp/lab/lab.git", "main", "/tmp/lab/wt")
            assert sup._session_owner("ses_2") == \
                ("/tmp/lab/lab.git", "main", "/tmp/lab/wt")
            assert sup._session_owner("unknown") is None
        finally:
            server.status = orig_status
            state.discover_projects = orig_discover
            state.Project.scan = orig_scan
    finally:
        tmp.cleanup()
        _hook_env_restore(saved)
    print("ok: snapshot derives the session-to-project map for event routing")


def test_stale_target_returns_the_installed_bin_when_running_a_checkout() -> None:
    from orch import install

    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_DATA_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["HOME"] = str(Path(tmp.name) / "home")
        os.environ["XDG_DATA_HOME"] = str(Path(tmp.name) / "data")

        assert daemon._stale_target() is None, "no install means nothing to re-exec onto"

        install.install()
        target = daemon._stale_target()
        assert target is not None, \
            "running from a checkout with an install present must target the install"
        assert target == install.install_dir() / "bin" / "orch"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tmp.cleanup()
    print("ok: a checkout-running daemon re-execs onto the canonical install")


def _empty_claim_repo() -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    """A task declared on base and claimed by a branch/worktree that committed
    nothing since its claim: the shape a turn-1 crash or quota stop leaves."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"],
                   check=True)
    (root / ".orchestration").mkdir(parents=True)
    (root / ".orchestration" / "config.json").write_text('{"baseBranch": "main"}\n')
    d = root / "tasks" / "task-a"
    d.mkdir(parents=True)
    (d / "task.md").write_text("# Task A\n\nDo it.\n")
    (root / "seed").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "task"],
                   check=True)
    wt = root.parent / "task-a"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "task-a",
                    str(wt), "main"], check=True)
    return root, wt, tmp


def test_a_claimed_task_that_committed_nothing_is_held_intact() -> None:
    root, wt, tmp = _empty_claim_repo()
    proj = state.Project.from_dir(root)
    assert proj is not None
    t = next(x for x in proj.scan() if x.name == "task-a")
    assert t.claimed and not t.dirty and not t.diff
    mem = daemon.LoopState()
    mem.settled_at[(t.project, t.name)] = NOW - daemon.SETTLE_BUDGET_S - 1
    acts = daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert daemon.REMOVE not in _kinds(acts)

    sup = daemon.Supervisor(dry_run=False)
    sup.mem = mem
    log = "\n".join(sup.apply(acts))
    assert "needs-review" in log, log

    assert wt.exists(), "the worktree must survive"
    assert (root / "tasks" / "task-a" / "task.md").exists(), \
        "the spec must stay in tasks/, not move to archive/"
    assert not (root / "archive" / "task-a" / "task.md").exists()
    p = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "-q",
                        "refs/heads/task-a"], capture_output=True)
    assert p.returncode == 0, "the task branch must survive"
    tmp.cleanup()
    print("ok: a claimed task that committed nothing is held with its branch, "
          "worktree and spec intact")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} daemon tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
