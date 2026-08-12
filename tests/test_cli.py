from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, observe, state  # noqa: E402


class _FakeProject(state.Project):
    def __init__(self, tasks: list[state.TaskState]):
        self._tasks = tasks

    @classmethod
    def from_dir(cls, start: Path) -> "_FakeProject | None":
        return cls._instance

    _instance: "_FakeProject | None" = None

    def scan(self, port: int | None = None) -> list[state.TaskState]:
        return self._tasks


def _blocked(name: str = "task-a", kind: str = "bash") -> state.TaskState:
    b = observe.Blocked(kind=kind, request_id="per_1", session_id="ses_1",
                        detail="/etc/*")
    return state.TaskState(project="/tmp/lab/lab.git", name=name, base="main",
                           blocked=[b])


def _args(**kw) -> argparse.Namespace:
    a = argparse.Namespace(directory=None, global_=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_answer_allow_replies_to_a_permission() -> None:
    replies: list[tuple] = []
    t = _blocked()
    _FakeProject._instance = _FakeProject([t])
    orig_status = cli.server_mod.status
    orig_reply = cli.harness.reply_permission
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    cli.harness.reply_permission = lambda *a, **k: replies.append((a, k))
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="allow",
                                       request=None))
        assert rc == 0, rc
        (port, sid, pid, reply), kw = replies[0]
        assert (port, sid, pid, reply) == (4096, "ses_1", "per_1", "allow")
        assert kw == {}, "an answer is one-time; nothing is persisted"
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
        cli.harness.reply_permission = orig_reply
    print("ok: `task answer allow` replies to the permission request")


def test_answer_rejects_non_allow_deny_for_a_permission() -> None:
    t = _blocked()
    _FakeProject._instance = _FakeProject([t])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="maybe",
                                       request=None))
        assert rc == 2, rc
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: a permission only accepts allow or deny")


def test_answer_question_routes_to_reply_question() -> None:
    t = _blocked(kind="question")
    _FakeProject._instance = _FakeProject([t])
    sent: list[tuple] = []
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    orig_q = cli.harness.reply_question
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    cli.harness.reply_question = lambda *a, **k: sent.append((a, k))
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="go ahead",
                                       request=None))
        assert rc == 0, rc
        (port, rid, answers), kw = sent[0]
        assert port == 4096 and rid == "per_1"
        assert answers == [{"value": "go ahead"}]
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
        cli.harness.reply_question = orig_q
    print("ok: a question routes to the question reply endpoint")


def test_answer_unblocked_task_is_refused() -> None:
    _FakeProject._instance = _FakeProject(
        [state.TaskState(project="/tmp/lab/lab.git", name="task-a", base="main")])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="allow",
                                       request=None))
        assert rc == 1, rc
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: answering a task that is not blocked is refused")


def test_task_list_json_reports_dependencies() -> None:
    import io
    import json

    _FakeProject._instance = _FakeProject([
        state.TaskState(project="/tmp/lab/lab.git", name="slice-a", base="main",
                        declared=True, wait_human_start=False,
                        dependencies=["feature", "auth-migrate"]),
    ])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_task_list(_args(json=True))
        assert rc == 0, rc
        payload = json.loads(buf.getvalue())
        assert payload[0]["dependencies"] == ["feature", "auth-migrate"], payload
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: `task list --json` carries the dependency edges")


class _GProject:
    _base_wt: Path | None = None
    _tasks: list = []

    def __init__(self, common: Path, orch: Path, base: str) -> None:
        self.common = common
        self.base = base

    @property
    def base_worktree(self) -> Path | None:
        return _GProject._base_wt

    def scan(self, port: int | None = None) -> list[state.TaskState]:
        return _GProject._tasks


def _gargs(**kw) -> argparse.Namespace:
    a = _args()
    a.global_ = True
    a.json = False
    a.roots = None
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _git(cwd: Path, *args: str) -> str:
    p = __import__("subprocess").run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "seed").write_text("x")
    orch = root / ".orchestration"
    orch.mkdir(parents=True)
    (orch / "config.json").write_text(
        '{"baseBranch": "main"}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, tmp


def test_project_scheduler_sets_the_cap_preserving_keys() -> None:
    root, tmp = _repo()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_scheduler(_args(directory=str(root), max="2"))
        assert rc == 0, rc
        cfg = json.loads((root / ".orchestration" / "config.json").read_text())
        assert cfg["scheduler"] == {"enabled": True, "maxConcurrent": 2}
        assert "review and commit" in buf.getvalue()
        cli.cmd_project_scheduler(_args(directory=str(root), max="off"))
        cfg = json.loads((root / ".orchestration" / "config.json").read_text())
        assert cfg["scheduler"]["enabled"] is False
    finally:
        tmp.cleanup()
    print("ok: `project scheduler <n>` writes the cap into the base config")


def test_project_scheduler_rejects_non_numbers() -> None:
    root, tmp = _repo()
    try:
        rc = cli.cmd_project_scheduler(_args(directory=str(root), max="lots"))
        assert rc == 2, rc
    finally:
        tmp.cleanup()
    print("ok: `project scheduler` validates its argument")


def test_daemon_budget_writes_the_machine_config() -> None:
    saved = os.environ.get("XDG_CONFIG_HOME")
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_daemon(_args(action="budget", max="3"))
        assert rc == 0, rc
        assert state.machine_budget() == 3
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_daemon(_args(action="budget", max=None))
        assert rc == 0, rc
        with contextlib.redirect_stdout(buf):
            cli.cmd_daemon(_args(action="budget", max="off"))
        assert state.machine_budget() is None
    finally:
        tmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved
    print("ok: `daemon budget` reads and writes the machine-wide cap")


def _global_setup(*, server_up: bool = True, daemon_active: bool = True,
                  agent: str = "idle", base_wt: Path | None = None) -> list:
    saved: list = []

    def _set(attr: str, value) -> None:
        obj, name = attr.rsplit(".", 1)
        target = getattr(cli, obj)
        saved.append((obj, name, getattr(target, name)))
        setattr(target, name, value)

    _set("state.Project", _GProject)
    _set("state.base_from_config", lambda _orch: "main")
    _set("state.discover_projects",
         lambda _roots: {"/tmp/lab/lab.git": Path("/tmp/lab/.orchestration")})
    _set("server_mod.status", lambda: {
        "running": server_up, "unresponsive": False,
        "port": 4096 if server_up else None})
    _set("server_mod.current", lambda: object() if server_up else None)
    _set("daemon_mod.unit_status", lambda: {
        "installed": daemon_active,
        "ActiveState": "active" if daemon_active else "inactive"})
    _set("agent_mod.status", lambda _i, _b: {"state": agent})
    _set("daemon_mod.project_label", lambda common: Path(common).parent.name)
    _GProject._base_wt = base_wt
    _GProject._tasks = []
    return saved


def _global_teardown(saved: list) -> None:
    for obj, name, val in saved:
        setattr(getattr(cli, obj), name, val)


def _claimed(name: str = "task-a", *, worktree: str | None = None,
             session: str = state.SESSION_IDLE, dirty: bool = False,
             diff: bool = True, conflicted: bool = False,
             blocked: list | None = None,
             wait_human_merge: bool = False) -> state.TaskState:
    return state.TaskState(
        project="/tmp/lab/lab.git", name=name, base="main", claimed=True,
        branch=name, worktree=worktree, session=session, dirty=dirty,
        diff=diff, conflicted=conflicted, blocked=blocked or [],
        wait_human_merge=wait_human_merge)


def test_task_list_global_reports_blocked_and_exits_1() -> None:
    t = _blocked()
    t.session = state.SESSION_BLOCKED
    t.claimed = True
    t.branch = "task-a"
    saved = _global_setup()
    _GProject._tasks = [t]
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_task_list(_gargs())
        out = buf.getvalue()
        assert rc == 1, rc
        assert "NEEDS-YOU" in out
        assert "task-a" in out
        assert "task answer -C" in out
        assert "lab" in out
    finally:
        _global_teardown(saved)
    print("ok: --global lists a blocked permission as needs-you with the answer action")


def test_task_list_global_question_suggests_a_reply() -> None:
    t = _blocked(kind="question")
    t.session = state.SESSION_BLOCKED
    t.claimed = True
    t.branch = "task-a"
    saved = _global_setup()
    _GProject._tasks = [t]
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_task_list(_gargs())
        assert "<your reply>" in buf.getvalue()
    finally:
        _global_teardown(saved)
    print("ok: a blocked question suggests `answer <name> <your reply>`")


def test_task_list_global_conflict_and_missing_worktree() -> None:
    with tempfile.TemporaryDirectory() as wt:
        missing = _claimed("gone", worktree="/nonexistent/deep")
        conflicted = _claimed("conf", worktree=wt, conflicted=True)
        saved = _global_setup(base_wt=Path(wt))
        _GProject._tasks = [missing, conflicted]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_task_list(_gargs())
            out = buf.getvalue()
            assert rc == 1, rc
            assert "task reset -C" in out
            assert "resolve the merge conflict" in out
        finally:
            _global_teardown(saved)
    print("ok: missing worktrees and merge conflicts are needs-you")


def test_task_list_global_awaiting_merge_is_needs_you() -> None:
    with tempfile.TemporaryDirectory() as wt:
        t = _claimed("task-a", worktree=wt, wait_human_merge=True)
        saved = _global_setup(base_wt=Path(wt))
        _GProject._tasks = [t]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_task_list(_gargs())
            out = buf.getvalue()
            assert rc == 1, rc
            assert "task merge -C" in out, out
            assert "awaiting your merge" in out, out
        finally:
            _global_teardown(saved)
    print("ok: a wait_human_merge task is needs-you with a merge action")


def test_task_list_global_looks_done_is_reported() -> None:
    with tempfile.TemporaryDirectory() as wt:
        t = _claimed("task-a", worktree=wt)
        saved = _global_setup(base_wt=Path(wt))
        _GProject._tasks = [t]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_task_list(_gargs())
            out = buf.getvalue()
            assert rc == 0, rc
            assert "LOOKS-DONE" in out
            assert "task-a" in out
        finally:
            _global_teardown(saved)
    print("ok: a clean unmerged task is always reported as looks-done")


def test_task_list_global_daemon_down_stalls_looks_done() -> None:
    with tempfile.TemporaryDirectory() as wt:
        t = _claimed("task-a", worktree=wt)
        saved = _global_setup(daemon_active=False, base_wt=Path(wt))
        _GProject._tasks = [t]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_task_list(_gargs())
            out = buf.getvalue()
            assert rc == 1, rc
            assert "daemon down" in out
        finally:
            _global_teardown(saved)
    print("ok: with the daemon down, unmerged work is stalled (exit 1)")


def test_task_list_global_busy_tasks_are_not_listed() -> None:
    with tempfile.TemporaryDirectory() as wt:
        t = _claimed("task-a", worktree=wt, session=state.SESSION_BUSY)
        saved = _global_setup(base_wt=Path(wt))
        _GProject._tasks = [t]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_task_list(_gargs())
            assert rc == 0, rc
            assert "task-a" not in buf.getvalue()
        finally:
            _global_teardown(saved)
    print("ok: busy sessions are the automation working, not waiting")


def test_task_list_global_json_shape() -> None:
    with tempfile.TemporaryDirectory() as wt:
        t = _claimed("task-a", worktree=wt)
        saved = _global_setup(base_wt=Path(wt))
        _GProject._tasks = [t]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_task_list(_gargs(json=True))
            assert rc == 0, rc
            data = json.loads(buf.getvalue())
            assert data["server"] == "up" and data["daemon"] == "active"
            assert len(data["items"]) == 1
            it = data["items"][0]
            assert it["section"] == "looks-done" and it["task"] == "task-a"
            assert it["project"] == "lab"
        finally:
            _global_teardown(saved)
    print("ok: --json emits a machine-readable waiting report")


def test_task_list_global_no_projects_is_an_error() -> None:
    saved = _global_setup()
    cli.state.discover_projects = lambda _roots: {}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_task_list(_gargs())
        assert rc == 2, rc
    finally:
        _global_teardown(saved)
    print("ok: --global with nothing discovered exits 2")


def test_task_list_global_no_waiting_exits_0() -> None:
    saved = _global_setup()
    _GProject._tasks = [state.TaskState(
        project="/tmp/lab/lab.git", name="todo-a", base="main")]
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_task_list(_gargs())
        assert rc == 0, rc
        assert "nothing waiting" in buf.getvalue()
    finally:
        _global_teardown(saved)
    print("ok: nothing waiting prints clean and exits 0")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} cli tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
