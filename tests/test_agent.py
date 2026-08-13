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


def _save_harness(*names: str) -> list:
    saved = [(name, getattr(agent.harness, name)) for name in names]
    return saved


def _restore_harness(saved: list) -> None:
    for name, orig in saved:
        setattr(agent.harness, name, orig)


def test_create_creates_a_session_and_waits_for_idle() -> None:
    calls: list = []
    orig_sleep = agent.time.sleep
    agent.time.sleep = lambda s: None
    busy = {"n": 0}

    def _status(p, d=None):
        busy["n"] += 1
        return {"ses_new": {}} if busy["n"] == 1 else {}

    saved = _save_harness("create_session", "prompt_async", "session_status",
                          "messages", "permissions", "questions")
    agent.harness.create_session = lambda p, d=None, title=None: (
        calls.append(("create_session", d, title)) or {"id": "ses_new"})
    agent.harness.prompt_async = lambda p, sid, text: (
        calls.append(("prompt_async", sid, text)) or None)
    agent.harness.session_status = _status
    agent.harness.messages = lambda p, sid: []
    agent.harness.permissions = lambda p, d=None: []
    agent.harness.questions = lambda p, d=None: []
    try:
        sid, notes = agent.create(_INFO, _FakeProject.base_worktree,
                                  "hello agent", timeout=5.0)
        assert sid == "ses_new" and notes == []
        assert ("create_session", str(_FakeProject.base_worktree),
                agent.AGENT_TITLE) in calls
        assert ("prompt_async", "ses_new", "hello agent") in calls
        assert not hasattr(agent, "subprocess"), \
            "the primary agent must not spawn an `opencode run` process"
    finally:
        _restore_harness(saved)
        agent.time.sleep = orig_sleep
    print("ok: first use creates a server session in the base worktree and waits")


def test_create_reports_when_the_agent_blocks() -> None:
    orig_sleep = agent.time.sleep
    agent.time.sleep = lambda s: None
    saved = _save_harness("create_session", "prompt_async", "session_status",
                          "messages", "permissions", "questions")
    agent.harness.create_session = lambda p, d=None, title=None: {"id": "ses_new"}
    agent.harness.prompt_async = lambda p, sid, text: None
    agent.harness.session_status = lambda p, d=None: {}
    agent.harness.messages = lambda p, sid: []
    agent.harness.permissions = lambda p, d=None: [{
        "id": "per_1", "sessionID": "ses_new", "permission": "bash",
        "patterns": ["/etc/*"]}]
    agent.harness.questions = lambda p, d=None: []
    try:
        sid, notes = agent.create(_INFO, _FakeProject.base_worktree, "x",
                                  timeout=5.0)
        assert sid == "ses_new"
        assert notes == ["blocked: /etc/*"], notes
    finally:
        _restore_harness(saved)
        agent.time.sleep = orig_sleep
    print("ok: a blocking agent is reported, not auto-rejected")


def test_create_fails_when_prompting_errors() -> None:
    saved = _save_harness("create_session", "prompt_async")
    agent.harness.create_session = lambda p, d=None, title=None: {"id": "ses_new"}
    agent.harness.prompt_async = lambda *a, **k: (_ for _ in ()).throw(
        agent.harness.HarnessError("POST failed"))
    try:
        sid, notes = agent.create(_INFO, _FakeProject.base_worktree, "x")
        assert sid is None and notes and "POST failed" in notes[0]
    finally:
        _restore_harness(saved)
    print("ok: a failed prompt surfaces the harness error")


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
    agent.harness.session_status = lambda _p, _d=None: {"ses_1": {}}
    agent.harness.permissions = lambda _p, _d=None: []
    agent.harness.questions = lambda _p, _d=None: []
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
    agent.harness.session_status = lambda _p, _d=None: {}
    agent.harness.permissions = lambda _p, _d=None: [{
        "id": "per_1", "sessionID": "ses_1", "permission": "bash",
        "patterns": ["/etc/*"]}]
    agent.harness.questions = lambda _p, _d=None: []
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


def test_stream_part_maps_text_thinking_tool_and_shell() -> None:
    assert agent._stream_part({"type": "text", "text": "hello"}) \
        == ("text", "hello")
    assert agent._stream_part({"type": "text", "text": "x", "synthetic": True}) \
        is None
    assert agent._stream_part({"type": "reasoning", "text": "think aloud"}) \
        == ("thinking", "think aloud")
    assert agent._stream_part(
        {"type": "tool", "tool": "create_task"}) == ("tool", "create_task")
    assert agent._stream_part({"type": "tool", "tool": "bash",
                               "state": {"input": {"command": "ls"}}}) \
        == ("shell", "ls")
    assert agent._stream_part({"type": "step-finish"}) is None
    print("ok: the stream renderer maps text, thinking, tool and shell parts")


def test_stream_part_shows_tool_args_capped() -> None:
    tool = agent._stream_part({"type": "tool", "tool": "create_task",
                               "state": {"input": {"name": "x",
                                                   "context": "why"}}})
    assert tool == ("tool", 'create_task({"context": "why", "name": "x"})')
    big = agent._stream_part({"type": "tool", "tool": "read_file",
                              "state": {"input": {"path": "x" * 500}}})
    assert big is not None and big[0] == "tool" and len(big[1]) <= 260, \
        "a giant tool payload must be capped so it cannot flood the terminal"
    assert "..." in big[1], "a truncated payload must say so"
    print("ok: tool args show up, capped; IDs stay hidden")


def test_stream_reply_yields_parts_and_the_exit_code() -> None:
    polls = {"n": 0}
    orig_sleep = agent.time.sleep
    agent.time.sleep = lambda s: None

    def _tick() -> int:
        polls["n"] += 1
        return polls["n"]

    def _messages(p, sid):
        _tick()
        return [{"info": {"role": "user"}, "parts": [
            {"type": "text", "text": "hi"}]},
            {"info": {"role": "assistant"}, "parts": [
                {"type": "text", "text": "first "},
                {"type": "tool", "tool": "create_task"},
                {"type": "text", "text": "reply"}]}]

    def _perms(p, d=None):
        _tick()
        return []

    def _ques(p, d=None):
        _tick()
        return []

    def _status(p, d=None):
        t = _tick()
        return {"ses_1": {}} if t < 5 else {}

    saved = _save_harness("messages", "permissions", "questions",
                          "session_status", "create_session", "prompt_async")
    agent.harness.messages = _messages
    agent.harness.permissions = _perms
    agent.harness.questions = _ques
    agent.harness.session_status = _status
    agent.harness.create_session = lambda p, d=None, title=None: {"id": "ses_1"}
    agent.harness.prompt_async = lambda p, sid, text: None
    try:
        events = list(agent.stream_reply(_INFO, _FakeProject.base_worktree,
                                         "hi", session_id="ses_1",
                                         timeout=30.0))
        text = "".join(p for k, p in events if k == "text")
        assert text == "first reply"
        assert ("tool", "create_task") in events
        assert ("exit", "0") in events
        assert not hasattr(agent, "subprocess"), \
            "stream_reply must not spawn an `opencode run` process"
    finally:
        _restore_harness(saved)
        agent.time.sleep = orig_sleep
    print("ok: stream_reply polls the session and yields text, tools, exit")


def test_stream_reply_reports_a_block_once_and_continues() -> None:
    ticks = {"n": 0}
    orig_sleep = agent.time.sleep
    agent.time.sleep = lambda s: None
    replied = []

    def _tick() -> int:
        ticks["n"] += 1
        return ticks["n"]

    def _messages(p, sid):
        t = _tick()
        parts = [{"type": "text", "text": "done"}] if t >= 4 else []
        return [{"info": {"role": "assistant"}, "parts": parts}]

    def _perms(p, d=None):
        t = _tick()
        if t < 3:
            return [{"id": "per_1", "sessionID": "ses_1", "permission": "bash",
                     "patterns": ["/etc/*"]}]
        return []

    def _ques(p, d=None):
        _tick()
        return []

    def _status(p, d=None):
        t = _tick()
        return {"ses_1": {}} if t < 6 else {}

    saved = _save_harness("messages", "permissions", "questions",
                          "session_status", "create_session", "prompt_async",
                          "reply_permission")
    agent.harness.messages = _messages
    agent.harness.permissions = _perms
    agent.harness.questions = _ques
    agent.harness.session_status = _status
    agent.harness.create_session = lambda p, d=None, title=None: {"id": "ses_1"}
    agent.harness.prompt_async = lambda p, sid, text: None
    agent.harness.reply_permission = lambda *a, **k: replied.append(a)
    try:
        events = list(agent.stream_reply(_INFO, _FakeProject.base_worktree,
                                         "hi", session_id="ses_1",
                                         timeout=30.0))
        assert events.count(("blocked", "/etc/*")) == 1, events
        assert ("text", "done") in events
        assert ("exit", "0") in events
        assert replied == [], "a blocked ask must stay pending, never auto-answered"
    finally:
        _restore_harness(saved)
        agent.time.sleep = orig_sleep
    print("ok: a block is reported once and left pending for a human")


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
