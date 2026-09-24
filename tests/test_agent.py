from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import agent, cli, server, state  # noqa: E402


_INFO = server.ServerInfo(pid=999, port=4096,
                          url="http://127.0.0.1:4096", started_at=0)


class _FakeProject:
    base_worktree = Path("/tmp/lab/lab")
    common = Path("/tmp/lab/lab.git")

    def scan(self, port: int | None = None) -> list:
        return []


def _task_state(name: str = "text",
                worktree: str = "/tmp/lab/lab/text") -> state.TaskState:
    return state.TaskState(project=str(_FakeProject.common), name=name,
                           base="main", claimed=True, worktree=worktree)


def _args(**kw) -> argparse.Namespace:
    a = argparse.Namespace(directory=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_project_of_requires_a_base_worktree() -> None:
    calls = []
    orig = agent.state.Project.from_dir

    def _none(start: Path) -> state.Project | None:
        calls.append(1)
        return None

    agent.state.Project.from_dir = _none
    try:
        _, err = agent.project_of(Path.cwd())
        assert err and "worktree" in err, err
        assert calls == [1]
    finally:
        agent.state.Project.from_dir = orig
    print("ok: project_of refuses to run outside a worktree")


def test_find_picks_the_newest_marked_session() -> None:
    orig = agent.harness.sessions
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "old", "title": agent.AGENT_TITLE, "time": {"updated": 1}},
        {"id": "new", "title": agent.AGENT_TITLE, "time": {"updated": 3}},
        {"id": "other", "title": "not ours", "time": {"updated": 9}},
    ]
    try:
        assert agent.find(_INFO.port, _FakeProject.base_worktree) == "new"
    finally:
        agent.harness.sessions = orig
    print("ok: find selects the newest session titled orch:agent")


def test_find_returns_none_when_no_marked_session() -> None:
    orig = agent.harness.sessions
    agent.harness.sessions = lambda _p, **kw: [{"id": "x", "title": "other"}]
    try:
        assert agent.find(_INFO.port, _FakeProject.base_worktree) is None
    finally:
        agent.harness.sessions = orig
    print("ok: find returns None when the agent was never created")


def _save_harness(*names: str) -> list:
    return [(name, getattr(agent.harness, name)) for name in names]


def _restore_harness(saved: list) -> None:
    for name, orig in saved:
        setattr(agent.harness, name, orig)


def test_ensure_returns_the_found_session() -> None:
    orig = agent.harness.sessions
    orig_create = agent.harness.create_session
    calls = []
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "ses_1", "title": agent.AGENT_TITLE}]
    agent.harness.create_session = lambda p, d=None, title=None: (
        calls.append((d, title)) or {"id": "ses_new"})
    try:
        assert agent.ensure(_INFO.port, _FakeProject.base_worktree) == "ses_1"
        assert calls == [], "an existing session must not be recreated"
    finally:
        agent.harness.sessions = orig
        agent.harness.create_session = orig_create
    print("ok: ensure reuses the existing primary-agent session")


def test_ensure_creates_a_session_bound_to_the_base_worktree() -> None:
    orig = agent.harness.sessions
    orig_create = agent.harness.create_session
    calls = []
    agent.harness.sessions = lambda _p, **kw: []
    agent.harness.create_session = lambda p, d=None, title=None: (
        calls.append((p, d, title)) or {"id": "ses_new"})
    try:
        sid = agent.ensure(_INFO.port, _FakeProject.base_worktree)
        assert sid == "ses_new"
        assert calls == [(_INFO.port, str(_FakeProject.base_worktree),
                          agent.AGENT_TITLE)]
    finally:
        agent.harness.sessions = orig
        agent.harness.create_session = orig_create
    print("ok: ensure creates the primary agent in the base worktree on first use")


def test_ensure_surfaces_a_failed_creation() -> None:
    orig = agent.harness.sessions
    orig_create = agent.harness.create_session
    agent.harness.sessions = lambda _p, **kw: []
    agent.harness.create_session = lambda p, d=None, title=None: {}
    try:
        try:
            agent.ensure(_INFO.port, _FakeProject.base_worktree)
            assert False, "ensure must raise when no session id comes back"
        except agent.harness.HarnessError as e:
            assert "session id" in str(e)
    finally:
        agent.harness.sessions = orig
        agent.harness.create_session = orig_create
    print("ok: a failed creation raises the harness error")


def _proj_with(tasks: list) -> state.Project:
    proj = state.Project(_FakeProject.common)
    proj.scan = lambda port=None: tasks
    return proj


def test_task_at_returns_the_claimed_task_whose_worktree_contains_start() -> None:
    t = _task_state()
    proj = _proj_with([t])
    assert agent.task_at(proj, _INFO.port, Path("/tmp/lab/lab/text")) is t
    assert agent.task_at(proj, _INFO.port, Path("/tmp/lab/lab/text/sub")) is t
    print("ok: task_at finds the claimed task whose worktree contains the path")


def test_task_at_returns_none_outside_any_task_worktree() -> None:
    proj = _proj_with([_task_state()])
    assert agent.task_at(proj, _INFO.port, Path("/tmp/lab/lab")) is None
    assert agent.task_at(proj, _INFO.port, Path("/tmp/other")) is None
    print("ok: task_at ignores the base worktree and unrelated paths")


def test_task_at_ignores_unclaimed_tasks() -> None:
    t = _task_state()
    t.claimed = False
    proj = _proj_with([t])
    assert agent.task_at(proj, _INFO.port, Path("/tmp/lab/lab/text")) is None
    print("ok: task_at never returns an unclaimed task")


def test_find_task_picks_the_newest_marked_session() -> None:
    orig = agent.harness.sessions
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "old", "title": "text", "time": {"updated": 1}},
        {"id": "new", "title": "text", "time": {"updated": 3}},
        {"id": "other", "title": agent.AGENT_TITLE, "time": {"updated": 9}},
    ]
    try:
        assert agent.find_task(_INFO.port, Path("/tmp/lab/lab/text"), "text") == "new"
    finally:
        agent.harness.sessions = orig
    print("ok: find_task selects the newest session titled like the task")


def test_find_task_returns_none_when_no_task_session() -> None:
    orig = agent.harness.sessions
    agent.harness.sessions = lambda _p, **kw: [{"id": "x", "title": "other"}]
    try:
        assert agent.find_task(_INFO.port, Path("/tmp/lab/lab/text"), "text") is None
    finally:
        agent.harness.sessions = orig
    print("ok: find_task returns None when the task was never started")


def test_ensure_task_reuses_the_existing_task_session() -> None:
    orig = agent.harness.sessions
    orig_create = agent.harness.create_session
    calls = []
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "ses_t", "title": "text"}]
    agent.harness.create_session = lambda p, d=None, title=None: (
        calls.append((d, title)) or {"id": "ses_new"})
    try:
        assert agent.ensure_task(_INFO.port, Path("/tmp/lab/lab/text"), "text") == "ses_t"
        assert calls == [], "an existing task session must not be recreated"
    finally:
        agent.harness.sessions = orig
        agent.harness.create_session = orig_create
    print("ok: ensure_task reuses the existing task session")


def test_ensure_task_creates_a_session_bound_to_the_task_worktree() -> None:
    orig = agent.harness.sessions
    orig_create = agent.harness.create_session
    calls = []
    agent.harness.sessions = lambda _p, **kw: []
    agent.harness.create_session = lambda p, d=None, title=None: (
        calls.append((p, d, title)) or {"id": "ses_new"})
    try:
        sid = agent.ensure_task(_INFO.port, Path("/tmp/lab/lab/text"), "text")
        assert sid == "ses_new"
        assert calls == [(_INFO.port, "/tmp/lab/lab/text", "text")]
    finally:
        agent.harness.sessions = orig
        agent.harness.create_session = orig_create
    print("ok: ensure_task creates the task session in the task worktree on first use")


def test_status_reports_busy() -> None:
    orig_sessions = agent.harness.sessions
    orig_active = agent.harness.active_sessions
    orig_perms = agent.harness.permissions
    orig_forms = agent.harness.forms
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "ses_1", "title": agent.AGENT_TITLE}]
    agent.harness.active_sessions = lambda _p: {"ses_1": {"type": "running"}}
    agent.harness.permissions = lambda _p, _d=None: []
    agent.harness.forms = lambda _p, _d=None: []
    try:
        st = agent.status(_INFO, _FakeProject.base_worktree)
        assert st == {"session": "ses_1", "state": "busy", "blocked": []}
    finally:
        agent.harness.sessions = orig_sessions
        agent.harness.active_sessions = orig_active
        agent.harness.permissions = orig_perms
        agent.harness.forms = orig_forms
    print("ok: status reports a streaming session as busy")


def test_status_reports_blocked_and_none_when_absent() -> None:
    orig_sessions = agent.harness.sessions
    orig_active = agent.harness.active_sessions
    orig_perms = agent.harness.permissions
    orig_forms = agent.harness.forms
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "ses_1", "title": agent.AGENT_TITLE}]
    agent.harness.active_sessions = lambda _p: {}
    agent.harness.permissions = lambda _p, _d=None: [{
        "id": "per_1", "sessionID": "ses_1", "action": "shell",
        "resources": ["/etc/*"]}]
    agent.harness.forms = lambda _p, _d=None: []
    try:
        st = agent.status(_INFO, _FakeProject.base_worktree)
        assert st["state"] == "blocked" and st["blocked"] == ["/etc/*"]
    finally:
        agent.harness.sessions = orig_sessions
        agent.harness.active_sessions = orig_active
        agent.harness.permissions = orig_perms
        agent.harness.forms = orig_forms
    print("ok: status reports a blocked permission and its target")

    agent.harness.sessions = lambda _p, **kw: []
    try:
        st = agent.status(_INFO, _FakeProject.base_worktree)
        assert st == {"session": None, "state": "none", "blocked": []}
    finally:
        agent.harness.sessions = orig_sessions
    print("ok: status reports none before the agent exists")


def _patch_cli(**fns) -> list:
    saved = [(cli.agent_mod, "project_of", cli.agent_mod.project_of),
             (cli.server_mod, "start", cli.server_mod.start),
             (cli.agent_mod, "ensure", cli.agent_mod.ensure),
             (cli.agent_mod, "task_at", cli.agent_mod.task_at),
             (cli.agent_mod, "ensure_task", cli.agent_mod.ensure_task),
             (cli.os, "execvp", cli.os.execvp)]
    cli.agent_mod.project_of = lambda _s: (_FakeProject(), None)
    cli.server_mod.start = lambda **k: _INFO
    for attr, val in fns.items():
        saved.append((cli.agent_mod, attr, getattr(cli.agent_mod, attr)))
        setattr(cli.agent_mod, attr, val)
    return saved


def _restore(saved: list) -> None:
    for mod, name, val in saved:
        setattr(mod, name, val)


def test_cmd_attach_attaches_to_the_found_session() -> None:
    execd = []
    saved = _patch_cli(ensure=lambda p, b: "ses_1")
    cli.os.execvp = lambda *a: execd.append(a)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_attach(_args())
        assert rc == 0, rc
        assert execd == [("opencode", ["opencode",
                                       str(_FakeProject.base_worktree),
                                       "--session", "ses_1"])]
        assert buf.getvalue() == ""
    finally:
        _restore(saved)
    print("ok: `attach` execs opencode on the found session, silent")


def test_cmd_attach_first_use_creates_then_attaches() -> None:
    ensured = []
    execd = []
    saved = _patch_cli(ensure=lambda p, b: ensured.append((p, b)) or "ses_new")
    cli.os.execvp = lambda *a: execd.append(a)
    try:
        rc = cli.cmd_attach(_args())
        assert rc == 0, rc
        assert ensured == [(_INFO.port, _FakeProject.base_worktree)]
        argv = execd[0][1]
        assert argv[argv.index("--session") + 1] == "ses_new"
    finally:
        _restore(saved)
    print("ok: first `attach` creates the session via ensure then attaches")


def test_cmd_attach_refuses_outside_a_worktree() -> None:
    saved = _patch_cli()
    cli.agent_mod.project_of = lambda _s: (None, "not in a worktree")
    try:
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.cmd_attach(_args())
        assert rc == 2, rc
        assert "not in a worktree" in err.getvalue()
    finally:
        _restore(saved)
    print("ok: `attach` outside a project worktree exits 2")


def test_cmd_attach_reports_a_failed_server_start() -> None:
    saved = _patch_cli()
    cli.server_mod.start = lambda **k: (_ for _ in ()).throw(
        RuntimeError("boom"))
    try:
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.cmd_attach(_args())
        assert rc == 1, rc
        assert "boom" in err.getvalue()
    finally:
        _restore(saved)
    print("ok: `attach` reports a server that would not start")


def test_cmd_attach_reports_a_failed_creation() -> None:
    saved = _patch_cli(ensure=lambda p, b: (_ for _ in ()).throw(
        cli.harness.HarnessError("no session id")))
    try:
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.cmd_attach(_args())
        assert rc == 1, rc
        assert "no session id" in err.getvalue()
    finally:
        _restore(saved)
    print("ok: `attach` reports when the session cannot be created")


def test_cmd_attach_reports_when_opencode_cannot_be_executed() -> None:
    saved = _patch_cli(ensure=lambda p, b: "ses_1")
    cli.os.execvp = lambda *a: (_ for _ in ()).throw(OSError("not found"))
    try:
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.cmd_attach(_args())
        assert rc == 1, rc
        assert "not found" in err.getvalue()
    finally:
        _restore(saved)
    print("ok: `attach` reports when opencode is missing")


def test_cmd_attach_attaches_to_the_task_session_in_a_task_worktree() -> None:
    execd = []
    saved = _patch_cli(task_at=lambda proj, port, start: _task_state(),
                       ensure_task=lambda port, wt, task: "ses_task")
    cli.os.execvp = lambda *a: execd.append(a)
    try:
        rc = cli.cmd_attach(_args())
        assert rc == 0, rc
        assert execd == [("opencode", ["opencode", "/tmp/lab/lab/text",
                                       "--session", "ses_task"])]
    finally:
        _restore(saved)
    print("ok: inside a task worktree `attach` execs opencode on the task session")


def test_cmd_attach_creates_the_task_session_on_first_use() -> None:
    created = []
    execd = []
    saved = _patch_cli(task_at=lambda proj, port, start: _task_state())
    cli.agent_mod.ensure_task = lambda port, wt, task: (
        created.append((port, str(wt), task)) or "ses_new")
    cli.os.execvp = lambda *a: execd.append(a)
    try:
        rc = cli.cmd_attach(_args())
        assert rc == 0, rc
        assert created == [(_INFO.port, "/tmp/lab/lab/text", "text")]
        argv = execd[0][1]
        assert argv[argv.index("--session") + 1] == "ses_new"
        assert argv[1] == "/tmp/lab/lab/text", \
            "the TUI is pointed at the task worktree"
    finally:
        _restore(saved)
    print("ok: a task worktree with no live session gets one created, then attached")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} agent tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
