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
        self.common = Path("/tmp/lab/lab.git")

    @classmethod
    def from_dir(cls, start: Path) -> "_FakeProject | None":
        return cls._instance

    _instance: "_FakeProject | None" = None

    @property
    def base_worktree(self) -> Path | None:
        return Path("/tmp/lab")

    def scan(self, port: int | None = None) -> list[state.TaskState]:
        return self._tasks


def _blocked(name: str = "task-a", kind: str = "bash") -> state.TaskState:
    b = observe.Blocked(kind=kind, request_id="per_1", session_id="ses_1",
                        detail="/etc/*")
    return state.TaskState(project="/tmp/lab/lab.git", name=name, base="main",
                           blocked=[b])


def _args(**kw) -> argparse.Namespace:
    a = argparse.Namespace(directory=None, global_=False, json=False)
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
        (port, pid, reply), kw = replies[0]
        assert (port, pid, reply) == (4096, "per_1", "once")
        assert kw == {"directory": None}, \
            "an allow maps to the v2 once reply, scoped to the worktree"
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


def test_task_list_local_shows_needs_you_for_a_blocked_task() -> None:
    b = observe.Blocked(kind="external_directory", request_id="per_1",
                        session_id="ses_1",
                        detail="/tmp/lab/base/*")
    t = state.TaskState(project="/tmp/lab/lab.git", name="task-a", base="main",
                        claimed=True, branch="task-a",
                        worktree="/tmp/lab/task-a",
                        session=state.SESSION_BLOCKED, blocked=[b])
    _FakeProject._instance = _FakeProject([t])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_task_list(_args())
        out = buf.getvalue()
        assert rc == 0, rc
        assert "NEEDS-YOU" in out
        assert "task answer" in out
        assert "allow|deny" in out
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: the local `task list` flags a blocked task as needs-you with the answer command")


def test_task_list_local_does_not_flag_unclaimed_tasks() -> None:
    t = state.TaskState(project="/tmp/lab/lab.git", name="todo-a", base="main")
    _FakeProject._instance = _FakeProject([t])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_task_list(_args())
        assert rc == 0, rc
        assert "NEEDS-YOU" not in buf.getvalue()
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: unclaimed todo tasks are not mislabelled as needs-you")


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


def _pargs(**kw) -> argparse.Namespace:
    a = argparse.Namespace(directory=None, global_=False, json=False, task=None,
                           reply=None, roots=None, reason=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _perm_blocked(name: str = "task-a", kind: str = "bash",
                  worktree: str = "/tmp/wt") -> state.TaskState:
    t = _blocked(name=name, kind=kind)
    t.claimed = True
    t.worktree = worktree
    return t


def _perm_saved() -> tuple:
    return (cli.state.Project.from_dir, cli.server_mod.status,
            cli.harness.reply_permission, cli._interactive, cli._prompt)


def _perm_restore(saved: tuple) -> None:
    (cli.state.Project.from_dir, cli.server_mod.status,
     cli.harness.reply_permission, cli._interactive, cli._prompt) = saved


def _perm_env(proj: "_FakeProject | None", prompts: list | None = None,
              interactive: bool = True) -> None:
    _FakeProject._instance = proj
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    cli._interactive = lambda: interactive
    it = iter(prompts or [])
    cli._prompt = lambda _m: next(it)


def test_permissions_oneshot_allow_routes_once() -> None:
    t = _perm_blocked()
    replied: list[tuple] = []
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    cli.harness.reply_permission = lambda *a, **k: replied.append((a, k))
    try:
        rc = cli.cmd_permissions(_pargs(task="task-a", reply="allow"))
        assert rc == 0, rc
        (port, pid, reply), kw = replied[0]
        assert (port, pid, reply) == (4096, "per_1", "once")
        assert kw == {"directory": "/tmp/wt"}, \
            "an allow maps to the v2 once reply, scoped to the worktree"
    finally:
        _perm_restore(saved)
    print("ok: `permissions --task <name> allow` replies once to the task")


def test_permissions_oneshot_deny_routes_reject_with_reason() -> None:
    t = _perm_blocked()
    replied: list[tuple] = []
    nudges: list[tuple] = []
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    cli.harness.reply_permission = lambda *a, **k: replied.append((a, k))
    cli.harness.prompt_async = lambda *a, **k: nudges.append((a, k))
    try:
        rc = cli.cmd_permissions(_pargs(task="task-a", reply="deny",
                                        reason="never touch /etc"))
        assert rc == 0, rc
        (port, pid, reply), kw = replied[0]
        assert (port, pid, reply) == (4096, "per_1", "reject")
        assert kw == {"directory": "/tmp/wt",
                      "message": "never touch /etc"}, \
            "a deny carries the reason to the agent"
        (nport, nsid, ntext), nkw = nudges[0]
        assert nport == 4096 and nsid == "ses_1"
        assert "never touch /etc" in ntext, \
            "the agent is told the reason and nudged to continue"
    finally:
        _perm_restore(saved)
    print("ok: `permissions --task <name> deny --reason` explains the denial "
          "to the agent and keeps it working")


def test_permissions_oneshot_deny_defaults_reason() -> None:
    t = _perm_blocked()
    replied: list[tuple] = []
    nudges: list[tuple] = []
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    cli.harness.reply_permission = lambda *a, **k: replied.append((a, k))
    cli.harness.prompt_async = lambda *a, **k: nudges.append((a, k))
    try:
        rc = cli.cmd_permissions(_pargs(task="task-a", reply="deny"))
        assert rc == 0, rc
        (port, pid, reply), kw = replied[0]
        assert kw["message"] == cli._DENY_DEFAULT, \
            "an empty reason falls back to the generic denial"
        assert cli._DENY_DEFAULT in nudges[0][0][2]
    finally:
        _perm_restore(saved)
    print("ok: a deny without --reason still explains itself to the agent")


def test_permissions_oneshot_rejects_bad_reply() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    try:
        rc = cli.cmd_permissions(_pargs(task="task-a", reply="maybe"))
        assert rc == 2, rc
    finally:
        _perm_restore(saved)
    print("ok: the --task one-shot only takes allow or deny")


def test_permissions_reply_without_task_is_an_error() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    try:
        rc = cli.cmd_permissions(_pargs(reply="allow"))
        assert rc == 2, rc
    finally:
        _perm_restore(saved)
    print("ok: a bare reply without --task is a usage error")


def test_permissions_oneshot_no_pending_is_an_error() -> None:
    t = state.TaskState(project="/tmp/lab/lab.git", name="task-a", base="main",
                        claimed=True, worktree="/tmp/wt")
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    try:
        rc = cli.cmd_permissions(_pargs(task="task-a", reply="allow"))
        assert rc == 1, rc
    finally:
        _perm_restore(saved)
    print("ok: answering a task with no pending permission exits 1")


def test_permissions_non_interactive_refused_without_oneshot() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]), interactive=False)
    try:
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.cmd_permissions(_pargs())
        assert rc == 2, rc
        assert "terminal" in err.getvalue()
    finally:
        _perm_restore(saved)
    print("ok: the interactive walk refuses a non-terminal without --task")


def test_permissions_json_lists_pending() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs(json=True))
        assert rc == 0, rc
        data = json.loads(buf.getvalue())
        assert data == [{"project": "lab", "owner": "task-a", "kind": "bash",
                         "detail": "/etc/*", "request_id": "per_1"}]
    finally:
        _perm_restore(saved)
    print("ok: `permissions --json` lists every pending ask machine-readably")


def test_permissions_interactive_walks_allow_and_deny() -> None:
    t = _perm_blocked()
    t.blocked.append(observe.Blocked(kind="write", request_id="per_2",
                                     session_id="ses_1", detail="/tmp/*"))
    replied: list[tuple] = []
    nudges: list[tuple] = []
    saved = _perm_saved()
    _perm_env(_FakeProject([t]), prompts=["a", "d", "no writes to /tmp"])

    def fake_reply(port, rid, reply, **kw) -> None:
        replied.append((rid, reply, kw))
        t.blocked = [b for b in t.blocked if b.request_id != rid]
    cli.harness.reply_permission = fake_reply
    cli.harness.prompt_async = lambda *a, **k: nudges.append((a, k))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs())
        out = buf.getvalue()
        assert rc == 0, (rc, out)
        assert replied == [("per_1", "once", {"directory": "/tmp/wt"}),
                           ("per_2", "reject",
                            {"directory": "/tmp/wt",
                             "message": "no writes to /tmp"})], \
            "a deny in the walk explains itself"
        assert nudges and "no writes to /tmp" in nudges[0][0][2], \
            "the denied session is told why and nudged to keep working"
        assert "no writes to /tmp" in out
        assert "all permissions for task-a answered" in out
    finally:
        _perm_restore(saved)
    print("ok: the walk allows, and denies with a reason sent to the agent")


def test_permissions_interactive_skip_reports_pending() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]), prompts=["s"])
    replied: list[tuple] = []
    cli.harness.reply_permission = lambda *a, **k: replied.append(a)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs())
        assert rc == 1, rc
        assert replied == [], "a skip never replies"
        assert "still pending" in buf.getvalue()
    finally:
        _perm_restore(saved)
    print("ok: skipping an ask leaves it pending and exits 1")


def test_permissions_interactive_quit_leaves_pending() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]), prompts=["q"])
    replied: list[tuple] = []
    cli.harness.reply_permission = lambda *a, **k: replied.append(a)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs())
        assert rc == 1, rc
        assert replied == []
    finally:
        _perm_restore(saved)
    print("ok: quitting the walk leaves the rest pending and exits 1")


def test_permissions_interactive_primary_agent_owner() -> None:
    t = _perm_blocked()
    perm_response: list[dict] = [{"id": "per_9", "sessionID": "ses_pa",
                                  "permission": "external_directory",
                                  "patterns": ["/tmp/opencode/*"],
                                  "metadata": {"filepath": "/tmp/opencode/a.sh",
                                               "parentDir": "/tmp/opencode"}}]
    replied: list[tuple] = []
    saved = _perm_saved()
    _perm_env(_FakeProject([t]), prompts=["2", "a"])
    orig_find = cli.agent_mod.find
    orig_perms = cli.harness.permissions
    cli.agent_mod.find = lambda port, base: "ses_pa"
    cli.harness.permissions = lambda port, directory: (
        [] if directory == "/tmp/wt" else perm_response)

    def fake_reply(port, rid, reply, **kw) -> None:
        replied.append((rid, reply, kw))
        perm_response.clear()
    cli.harness.reply_permission = fake_reply
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs())
        out = buf.getvalue()
        assert rc == 0, (rc, out)
        assert "primary-agent@main" in out, "the primary agent is a picker row"
        assert "/tmp/opencode/a.sh" in out, \
            "the walk shows the actual file the agent wants to touch, not the glob"
        assert replied == [("per_9", "once", {"directory": "/tmp/lab"})], \
            "the primary agent's ask is answered scoped to the base worktree"
    finally:
        _perm_restore(saved)
        cli.agent_mod.find = orig_find
        cli.harness.permissions = orig_perms
    print("ok: the walk answers the primary agent's pending permission")


def test_permissions_dir_flag_lists_non_interactively() -> None:
    t = _perm_blocked()
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs(directory="/tmp/lab"))
        out = buf.getvalue()
        assert rc == 0, (rc, out)
        assert "task-a" in out and "bash" in out and "/etc/*" in out
        assert "interactive" not in out
    finally:
        _perm_restore(saved)
    print("ok: -C alone lists the project's pending asks without prompting")


def test_permissions_dir_flag_no_pending_exits_0() -> None:
    t = state.TaskState(project="/tmp/lab/lab.git", name="task-a", base="main",
                        claimed=True, worktree="/tmp/wt")
    saved = _perm_saved()
    _perm_env(_FakeProject([t]))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs(directory="/tmp/lab"))
        assert rc == 0, rc
        assert "nothing pending" in buf.getvalue()
    finally:
        _perm_restore(saved)
    print("ok: -C with nothing pending prints clean and exits 0")


def test_permissions_global_picker_selects_project() -> None:
    asks = {
        "/p1/p1.git": [cli._PermAsk(owner="task-a", worktree="/p1/wt",
                                    kind="bash", detail="/etc/*",
                                    request_id="per_1", session_id="ses_1")],
        "/p2/p2.git": [cli._PermAsk(owner="task-b", worktree="/p2/wt",
                                    kind="write", detail="/tmp/*",
                                    request_id="per_2", session_id="ses_2")],
    }
    replied: list[tuple] = []
    saved = _perm_saved()
    orig_disc = cli.state.discover_projects
    orig_base = cli.state.base_from_config
    orig_from = cli.state.Project.from_dir
    orig_label = cli.daemon_mod.project_label
    orig_asks = cli._perm_asks
    _perm_env(None, prompts=["2", "a"])
    cli.state.discover_projects = lambda _r: {
        "/p1/p1.git": Path("/p1/.orchestration"),
        "/p2/p2.git": Path("/p2/.orchestration")}
    cli.state.base_from_config = lambda _o: "main"
    cli.state.Project.from_dir = lambda start: None
    cli.daemon_mod.project_label = lambda c: Path(c).parent.name
    cli._perm_asks = lambda port, proj: asks.get(str(proj.common), [])

    def fake_reply(port, rid, reply, **kw) -> None:
        replied.append((rid, reply, kw))
        for lst in asks.values():
            lst[:] = [a for a in lst if a.request_id != rid]
    cli.harness.reply_permission = fake_reply
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_permissions(_pargs(global_=True))
        out = buf.getvalue()
        assert rc == 0, (rc, out)
        assert "p1" in out and "p2" in out, "the project picker lists both"
        assert "task-b" in out, "the picked project's task is walked"
        assert replied == [("per_2", "once", {"directory": "/p2/wt"})], \
            "selecting project 2 answers its task, not project 1's"
    finally:
        _perm_restore(saved)
        cli.state.discover_projects = orig_disc
        cli.state.base_from_config = orig_base
        cli.state.Project.from_dir = orig_from
        cli.daemon_mod.project_label = orig_label
        cli._perm_asks = orig_asks
    print("ok: --global picks a project interactively, then walks its task")


def test_describe_permission_shows_the_actual_target() -> None:
    cases = [
        ({"permission": "bash", "patterns": ["*"],
          "metadata": {"command": "npm run build"}}, "npm run build"),
        ({"permission": "webfetch", "patterns": [],
          "metadata": {"url": "https://example.com/x"}}, "https://example.com/x"),
        ({"permission": "external_directory",
          "patterns": ["/tmp/opencode/*"],
          "metadata": {"filepath": "/tmp/opencode/a.sh"}}, "/tmp/opencode/a.sh"),
        ({"permission": "write", "resources": ["src/x.ts"],
          "metadata": {"filePath": "src/x.ts"}}, "src/x.ts"),
        ({"permission": "bash", "patterns": ["*"]}, "*"),
    ]
    for raw, want in cases:
        b = observe._describe_permission(raw)
        assert b.detail == want, (raw, b.detail, want)
    print("ok: a permission ask is described by what it tries to run")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} cli tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
