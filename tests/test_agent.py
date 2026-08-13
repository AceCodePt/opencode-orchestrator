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


def _args(**kw) -> argparse.Namespace:
    a = argparse.Namespace(directory=None, timeout=60.0, status=False,
                           compact=False, async_=False, stream=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _text_part(text: str, synthetic: bool = False) -> dict:
    p: dict[str, object] = {"type": "text", "text": text}
    if synthetic:
        p["synthetic"] = True
    return p


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


def test_create_runs_opencode_run_then_finds_the_session() -> None:
    ran = []
    orig_run = agent.subprocess.run
    orig_find = agent.find
    agent.subprocess.run = lambda cmd, **kw: (
        ran.append((cmd, kw)) or type("P", (), {"returncode": 0})())
    agent.find = lambda _p, _b: "ses_new"
    try:
        sid, notes = agent.create(_INFO, _FakeProject.base_worktree,
                                  "hello agent")
        assert sid == "ses_new"
        assert notes == []
        cmd, kw = ran[0]
        assert cmd[0] == "opencode"
        assert "--attach" in cmd and "--title" in cmd
        assert agent.AGENT_TITLE in cmd
        assert "--dir" in cmd and str(_FakeProject.base_worktree) in cmd
        assert cmd[-1] == "hello agent"
        assert kw["cwd"] == str(_FakeProject.base_worktree)
    finally:
        agent.subprocess.run = orig_run
        agent.find = orig_find
    print("ok: first use creates the session via opencode run in the base worktree")


def test_create_fails_when_run_errors() -> None:
    orig_run = agent.subprocess.run
    agent.subprocess.run = lambda cmd, **kw: type("P", (), {
        "returncode": 1, "stdout": "", "stderr": "boom"})()
    try:
        sid, notes = agent.create(_INFO, _FakeProject.base_worktree, "x")
        assert sid is None and notes and "boom" in notes[0]
    finally:
        agent.subprocess.run = orig_run
    print("ok: a failed agent run surfaces its stderr")


def test_render_message_keeps_only_non_synthetic_text() -> None:
    msg = {"info": {}, "parts": [
        {"type": "reasoning", "text": "think"},
        _text_part("the answer"),
        _text_part("Called the Read tool", synthetic=True),
        {"type": "tool", "state": {"status": "completed"}},
        _text_part("and the rest"),
    ]}
    assert agent.render_message(msg) == "the answer\nand the rest"
    print("ok: render skips reasoning, tool and synthetic parts")


def test_ask_posts_a_message_and_returns_the_rendered_reply() -> None:
    sent = []
    orig = agent.harness.message
    agent.harness.message = lambda p, sid, text, timeout=None: (
        sent.append((p, sid, text)) or {"parts": [_text_part("reply text")]})
    try:
        reply, err = agent.ask(_INFO, "ses_1", "why?")
        assert err is None
        assert reply == "reply text"
        assert sent == [(_INFO.port, "ses_1", "why?")]
    finally:
        agent.harness.message = orig
    print("ok: ask sends the message and renders the assistant reply")


def test_ask_surfaces_harness_failures() -> None:
    orig = agent.harness.message
    agent.harness.message = lambda *a, **k: (_ for _ in ()).throw(
        agent.harness.HarnessError("POST failed"))
    try:
        reply, err = agent.ask(_INFO, "ses_1", "x")
        assert reply == "" and err and "POST failed" in err
    finally:
        agent.harness.message = orig
    print("ok: a failed ask reports the harness error")


def test_fire_uses_prompt_async() -> None:
    sent = []
    orig = agent.harness.prompt_async
    agent.harness.prompt_async = lambda p, sid, text: sent.append((p, sid, text))
    try:
        assert agent.fire(_INFO, "ses_1", "go do it") is None
        assert sent == [(_INFO.port, "ses_1", "go do it")]
    finally:
        agent.harness.prompt_async = orig
    print("ok: fire sends asynchronously through prompt_async")


def test_compact_calls_opencode_summarize_with_the_session_model() -> None:
    got = []
    orig_info = agent.harness.session_info
    orig_sum = agent.harness.summarize
    agent.harness.session_info = lambda _p, _s: {
        "model": {"providerID": "anthropic", "id": "claude-x"}}
    agent.harness.summarize = lambda *a, **k: got.append((a, k)) or True
    try:
        ok, err = agent.compact(_INFO, "ses_1", timeout=123.0)
        assert ok and err is None
        (port, sid, pid, mid), kw = got[0]
        assert (port, sid, pid, mid) == (_INFO.port, "ses_1", "anthropic",
                                         "claude-x")
        assert kw == {"timeout": 123.0}
    finally:
        agent.harness.session_info = orig_info
        agent.harness.summarize = orig_sum
    print("ok: compact resolves the session model and POSTs to /summarize")


def test_compact_requires_a_model() -> None:
    orig_info = agent.harness.session_info
    agent.harness.session_info = lambda _p, _s: {"model": None}
    try:
        ok, err = agent.compact(_INFO, "ses_1")
        assert not ok and err and "no model" in err
    finally:
        agent.harness.session_info = orig_info
    print("ok: compact refuses a session with no recorded model")


def test_status_reports_busy() -> None:
    orig_sessions = agent.harness.sessions
    orig_status = agent.harness.session_status
    orig_perms = agent.harness.permissions
    orig_ques = agent.harness.questions
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "ses_1", "title": agent.AGENT_TITLE}]
    agent.harness.session_status = lambda _p: {"ses_1": {}}
    agent.harness.permissions = lambda _p: []
    agent.harness.questions = lambda _p: []
    try:
        st = agent.status(_INFO, _FakeProject.base_worktree)
        assert st == {"session": "ses_1", "state": "busy", "blocked": []}
    finally:
        agent.harness.sessions = orig_sessions
        agent.harness.session_status = orig_status
        agent.harness.permissions = orig_perms
        agent.harness.questions = orig_ques
    print("ok: status reports a streaming session as busy")


def test_status_reports_blocked_and_none_when_absent() -> None:
    orig_sessions = agent.harness.sessions
    orig_status = agent.harness.session_status
    orig_perms = agent.harness.permissions
    orig_ques = agent.harness.questions
    agent.harness.sessions = lambda _p, **kw: [
        {"id": "ses_1", "title": agent.AGENT_TITLE}]
    agent.harness.session_status = lambda _p: {}
    agent.harness.permissions = lambda _p: [{
        "id": "per_1", "sessionID": "ses_1", "permission": "bash",
        "patterns": ["/etc/*"]}]
    agent.harness.questions = lambda _p: []
    try:
        st = agent.status(_INFO, _FakeProject.base_worktree)
        assert st["state"] == "blocked" and st["blocked"] == ["/etc/*"]
    finally:
        agent.harness.sessions = orig_sessions
        agent.harness.session_status = orig_status
        agent.harness.permissions = orig_perms
        agent.harness.questions = orig_ques
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
             (cli.server_mod, "start", cli.server_mod.start)]
    cli.agent_mod.project_of = lambda _s: (_FakeProject(), None)
    cli.server_mod.start = lambda **k: _INFO
    for attr, val in fns.items():
        saved.append((cli.agent_mod, attr, getattr(cli.agent_mod, attr)))
        setattr(cli.agent_mod, attr, val)
    return saved


def _restore(saved: list) -> None:
    for mod, name, val in saved:
        setattr(mod, name, val)


def test_cmd_ask_status_when_no_session() -> None:
    saved = _patch_cli(find=lambda p, b: None)
    try:
        rc = cli.cmd_ask(_args(status=True))
        assert rc == 0, rc
    finally:
        for mod, name, val in saved:
            setattr(mod, name, val)
    print("ok: `ask --status` with no agent yet exits cleanly")


def test_cmd_ask_first_use_creates_and_prints_reply() -> None:
    saved = _patch_cli(find=lambda p, b: None,
                       create=lambda info, b, text, timeout: ("ses_new", []),
                       last_reply=lambda p, s: "first answer")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_ask(_args(message="hi"))
        assert rc == 0, rc
        assert "first answer" in buf.getvalue()
    finally:
        _restore(saved)
    print("ok: first `ask` creates the session and prints the reply")


def test_cmd_ask_existing_session_posts_and_prints() -> None:
    sent = []
    saved = _patch_cli(find=lambda p, b: "ses_1",
                       ask=lambda info, sid, text, timeout: (
                           sent.append(text) or ("the reply", None)))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_ask(_args(message="hello"))
        assert rc == 0 and sent == ["hello"]
        assert "the reply" in buf.getvalue()
    finally:
        _restore(saved)
    print("ok: `ask` on an existing session posts and prints the reply")


def test_cmd_ask_async_returns_immediately() -> None:
    saved = _patch_cli(find=lambda p, b: "ses_1",
                       fire=lambda info, sid, text: None)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_ask(_args(message="do it", async_=True))
        assert rc == 0
        assert "sent to session ses_1" in buf.getvalue()
    finally:
        _restore(saved)
    print("ok: `ask --async` fires and returns the session id")


def test_cmd_ask_compact_without_session_is_refused() -> None:
    saved = _patch_cli(find=lambda p, b: None)
    try:
        rc = cli.cmd_ask(_args(compact=True, message=None, status=False,
                               async_=False))
        assert rc == 1, rc
    finally:
        for mod, name, val in saved:
            setattr(mod, name, val)
    print("ok: `ask --compact` refuses before a conversation exists")


def test_cmd_ask_compact_then_send() -> None:
    compacted = []
    saved = _patch_cli(find=lambda p, b: "ses_1",
                       compact=lambda info, sid, timeout: (
                           compacted.append(sid) or (True, None)),
                       ask=lambda info, sid, text, timeout: ("after", None))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_ask(_args(compact=True, message="continue",
                                   status=False, async_=False))
        assert rc == 0
        assert compacted == ["ses_1"]
        assert "after" in buf.getvalue()
    finally:
        _restore(saved)
    print("ok: `ask --compact <msg>` compacts then sends")


def test_stream_event_maps_text_tool_shell_and_error() -> None:
    assert agent._stream_event(
        {"type": "text", "part": {"type": "text", "text": "hello"}}) \
        == ("text", "hello")
    assert agent._stream_event(
        {"type": "text", "part": {"type": "tool", "tool": "create_task"}}) \
        == ("tool", "create_task")
    assert agent._stream_event(
        {"type": "text", "part": {"type": "shell"}}) == ("shell", "shell")
    assert agent._stream_event(
        {"type": "error", "part": {"type": "error", "error": "boom"}}) \
        == ("error", "boom")
    assert agent._stream_event(
        {"type": "step_finish", "part": {"type": "step-finish"}}) is None
    print("ok: the stream renderer maps text, tool, shell and error events")


def test_stream_event_shows_thinking_and_tool_input() -> None:
    assert agent._stream_event(
        {"type": "reasoning", "part": {"type": "reasoning",
                                       "text": "think aloud"}}) \
        == ("thinking", "think aloud")
    tool = agent._stream_event(
        {"type": "text", "part": {"type": "tool", "tool": "create_task",
                                  "state": {"status": "completed",
                                            "input": {"name": "x",
                                                      "context": "why"}}}})
    assert tool is not None and tool[0] == "tool"
    assert 'create_task({"context": "why", "name": "x"})' == tool[1]
    huge = agent._stream_event(
        {"type": "text", "part": {"type": "tool", "tool": "bash",
                                  "state": {"input": {"command": "x" * 500}}}})
    assert huge is not None and len(huge[1]) <= 250, \
        "a giant tool payload must be capped so it cannot flood the terminal"
    assert "..." in huge[1], "a truncated payload must say so"
    print("ok: reasoning and tool args show up, capped; IDs stay hidden")


def test_stream_reply_yields_parts_and_the_exit_code() -> None:
    lines = [
        '{"type": "text", "part": {"type": "text", "text": "first "}}',
        '{"type": "text", "part": {"type": "tool", "tool": "create_task"}}',
        '{"type": "text", "part": {"type": "text", "text": "reply"}}',
    ]

    class _Stream:
        def __init__(self, items) -> None:
            self._it = iter(items)

        def __iter__(self):
            return self._it

        def close(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _Stream(l + "\n" for l in lines)

        def wait(self, timeout=None) -> int:
            return 0

        def poll(self) -> int:
            return 0

        def kill(self) -> None:
            pass

    captured = []
    orig_popen = agent.subprocess.Popen
    agent.subprocess.Popen = lambda *a, **k: captured.append(a) or _FakeProc()
    try:
        events = list(agent.stream_reply(_INFO, _FakeProject.base_worktree,
                                         "hi", session_id="ses_1",
                                         timeout=30.0))
        text = "".join(p for k, p in events if k == "text")
        assert text == "first reply"
        assert ("tool", "create_task") in events
        assert ("exit", "0") in events
        cmd = captured[0][0]
        assert "-s" in cmd and "ses_1" in cmd
        assert cmd[0] == "opencode" and "--format" in cmd
    finally:
        agent.subprocess.Popen = orig_popen
    print("ok: stream_reply yields text, tool markers, and the exit code")


def test_cmd_ask_stream_uses_stream_reply_on_the_found_session() -> None:
    streamed = []
    saved = _patch_cli(find=lambda p, b: "ses_1")
    orig_stream = cli.agent_mod.stream_reply

    def fake_stream(info, base, text, session_id=None, timeout=60.0):
        streamed.append((session_id, text))
        yield "text", "live answer"

    cli.agent_mod.stream_reply = fake_stream
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_ask(_args(stream=True, message="go"))
        assert rc == 0, rc
        assert "live answer" in buf.getvalue()
        assert streamed == [("ses_1", "go")]
    finally:
        _restore(saved)
        cli.agent_mod.stream_reply = orig_stream
    print("ok: `ask --stream` streams the reply on the found session")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} agent tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
