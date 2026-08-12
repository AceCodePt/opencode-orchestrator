from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import daemon, gate, observe, server, state  # noqa: E402

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
        assert marker.read_text().strip() == "alert:alert:lab.git:task-a"
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
        assert marker.read_text().strip() == "needs-review:needs-review:lab.git:task-a"
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
    acts = daemon.decide(_snap(now=NOW + 10, tasks=[t], mem=mem))
    assert daemon.VERIFY not in _kinds(acts), "before the budget no action"
    print("ok: a freshly settled task waits before being acted on")


def test_settled_dirty_task_is_verified() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY], _kinds(acts)
    assert acts[0].retry_cap == 3
    print("ok: a settled dirty task is verified (the gate decides commit vs retry)")


def test_settled_clean_diff_task_is_verified() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.VERIFY]
    print("ok: a settled clean+diff task is verified")


def test_settled_clean_noop_is_removed_without_a_gate() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=False)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.REMOVE]
    print("ok: a clean no-diff task is removed without running a gate")


def test_missing_worktree_needs_review() -> None:
    mem = daemon.LoopState()
    acts = daemon.decide(_snap(tasks=[_task(worktree=None)], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW]
    print("ok: a claimed task with no worktree needs review")


def test_wait_backoff_blocks_repeated_actions() -> None:
    mem = daemon.LoopState()
    mem.wait_until[("/tmp/lab/lab.git", "task-a")] = NOW + 9999
    snap = _snap(tasks=[_task(session=state.SESSION_IDLE, dirty=True)], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(snap)
    assert acts == [], "a task in backoff must not be acted on"
    print("ok: a task in backoff is left alone")


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
    print("ok: an unchanged worktree after a gate failure eventually stalls to review")


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
    t = _task(claimed=False, declared=True, hitl=False)
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


def _gated_repo(gate_body: str) -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    for cmd in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *cmd, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    h = root / ".orchestration" / "hooks"
    h.mkdir(parents=True)
    (h / "session-idle").write_text(gate_body)
    (h / "session-idle").chmod(0o755)
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "main", "gate": {"retryCap": 3}}\n')
    (root / "seed").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True)
    return root, tmp


def test_apply_verify_merges_a_passing_gate_and_archives() -> None:
    root, tmp = _gated_repo(
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
    # dirty work, gate will pass and commit it

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
    print("ok: a passing gate merges, archives, and tears the task down")


def test_apply_verify_failing_gate_retriggers_up_to_the_cap() -> None:
    root, tmp = _gated_repo("#!/usr/bin/env bash\necho 'tests red'\nexit 1\n")
    injected: list[tuple[int, str, str]] = []
    orig = daemon.prompt_async
    daemon.prompt_async = lambda port, sid, text: injected.append((port, sid, text)) or None
    try:
        sup = daemon.Supervisor(dry_run=False)
        a = daemon.Action(daemon.VERIFY, "task-a", "settled",
                          project=str(root), worktree=str(root), branch="task-a",
                          base="main", retry_cap=3, port=4096, session_ids=["ses_1"])
        sup.apply([a])
        assert len(injected) == 1, "a failing gate must inject the failure back"
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
    print("ok: a failing gate retriggers up to the cap, then needs-review")


def test_apply_verify_no_gate_is_reviewed() -> None:
    root, tmp = _gated_repo("#!/usr/bin/env bash\nexit 0\n")
    # no committed gate: remove the committed hook and commit
    subprocess.run(["git", "-C", str(root), "rm", "-q",
                    ".orchestration/hooks/session-idle"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "no gate"],
                   check=True)
    sup = daemon.Supervisor(dry_run=False)
    a = daemon.Action(daemon.VERIFY, "task-a", "settled",
                      project=str(root), worktree=str(root), branch="task-a",
                      base="main", retry_cap=3, session_ids=[])
    log = "\n".join(sup.apply([a]))
    assert "needs-review" in log
    assert "no committed gate" in log
    tmp.cleanup()
    print("ok: no committed gate lands on needs-review, never done")


def test_unit_text_keeps_the_expected_shape() -> None:
    text = daemon.unit_text(interval=10)
    assert "Restart=always" in text
    assert "KillMode=process" in text
    assert "daemon run --interval 10" in text
    print("ok: the systemd unit restarts always and keeps the server alive")


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


def test_scheduler_skips_hitl_tasks() -> None:
    mem = daemon.LoopState()
    hitl = _task(claimed=False, branch=None, worktree=None, hitl=True)
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    acts = daemon.decide(_snap(tasks=[hitl], mem=mem, policies={hitl.project: pol}))
    assert daemon.DISPATCH not in _kinds(acts), \
        "a task that needs a human is never auto-picked"
    auto = _task(name="task-auto", claimed=False, branch=None, worktree=None,
                 hitl=False)
    acts = daemon.decide(_snap(tasks=[auto], mem=mem, policies={auto.project: pol}))
    assert _kinds(acts) == [daemon.DISPATCH], _kinds(acts)
    print("ok: the scheduler leaves hitl tasks for a human and picks the rest")


def test_scheduler_holds_a_task_until_its_dependencies_are_archived() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=2,
                                start=Path("/tmp/lab"))
    dep_open = _task(name="feature", claimed=False, branch=None, worktree=None,
                     hitl=False)
    slice_ = _task(name="feature-slice", claimed=False, branch=None, worktree=None,
                   hitl=False, dependencies=["feature"])
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
    print("ok: the scheduler gates dispatch on archived dependencies")


def test_scheduler_treats_an_undeclared_dependency_as_satisfied() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=1,
                                start=Path("/tmp/lab"))
    slice_ = _task(name="slice-a", claimed=False, branch=None, worktree=None,
                   hitl=False, dependencies=["never-written"])
    acts = daemon.decide(_snap(tasks=[slice_], mem=mem, policies={slice_.project: pol}))
    assert _kinds(acts) == [daemon.DISPATCH], \
        "a slug that was never declared cannot block - only an open declared task does"
    print("ok: a dependency slug that was never declared does not hold the task")


def test_global_budget_caps_dispatches_across_projects() -> None:
    mem = daemon.LoopState()
    pol = state.SchedulerPolicy(enabled=True, max_concurrent=5,
                                start=Path("/tmp/lab"))
    p1_todo = _task(project="/tmp/lab/p1.git", name="p1-a", claimed=False,
                    branch=None, worktree=None, hitl=False)
    p2_todo = _task(project="/tmp/lab/p2.git", name="p2-a", claimed=False,
                    branch=None, worktree=None, hitl=False)
    p2_todo2 = _task(project="/tmp/lab/p2.git", name="p2-b", claimed=False,
                     branch=None, worktree=None, hitl=False)
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
                 branch=None, worktree=None, hitl=False)
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
    todo = _task(claimed=False, branch=None, worktree=None, hitl=False)
    snap = _snap(tasks=[todo], mem=mem, policies={todo.project: pol},
                 global_max=0)
    assert daemon.DISPATCH not in _kinds(daemon.decide(snap))
    print("ok: a machine-wide budget of 0 means nothing is auto-dispatched")


def test_hitl_conflicted_task_is_reviewed_not_reverified() -> None:
    mem = daemon.LoopState()
    t = _task(session=state.SESSION_IDLE, dirty=False, diff=True, conflicted=True,
              hitl=True)
    snap = _snap(tasks=[t], mem=mem)
    daemon.decide(snap)
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW], _kinds(acts)
    assert "merge-conflict" in acts[0].reason, acts[0].reason
    assert daemon.VERIFY not in _kinds(acts), \
        "a conflicted task must not be re-gated or re-merged"
    print("ok: a hitl conflicted task is routed to needs-review, never re-verified")


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
    (d / "task.md").write_text("---\nhitl: false\n---\n\n# Task A\n\nDo it.\n")
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
    assert t.conflicted and not t.hitl, "the task must be auto and conflicted"
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


def test_auto_resolved_conflict_archives_as_a_noop() -> None:
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
    assert t.status == state.NOOP, t.status
    mem = daemon.LoopState()
    daemon.decide(_snap(now=NOW, tasks=[t], mem=mem))
    acts = daemon.decide(_snap(now=NOW + daemon.SETTLE_BUDGET_S + 1,
                               tasks=[t], mem=mem))
    assert daemon.REMOVE in _kinds(acts), _kinds(acts)
    assert daemon.RESOLVE not in _kinds(acts)
    tmp.cleanup()
    print("ok: a resolved conflict falls through to the no-op archive path")


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


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} daemon tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
