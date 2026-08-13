from __future__ import annotations

import json
import time
from pathlib import Path

from . import harness, observe, server, state

AGENT_TITLE = "orch:agent"

DEFAULT_ASK_TIMEOUT = 600.0
DEFAULT_CREATE_TIMEOUT = 600.0


def project_of(start: Path) -> tuple[state.Project | None, str | None]:
    proj = state.Project.from_dir(start)
    if proj is None:
        return None, ("not in a worktree - run `orch ask` from a project "
                      "checkout")
    if proj.base_worktree is None:
        return None, (f"no worktree on base branch {proj.base!r}; the primary "
                      "agent needs the main checkout")
    return proj, None


def find(port: int, base: Path) -> str | None:
    """The id of the primary agent's session in <base>, newest first."""
    best: str | None = None
    best_ts = -1
    for s in harness.sessions(port, directory=str(base)):
        if s.get("title") != AGENT_TITLE:
            continue
        sid = s.get("id")
        if not sid:
            continue
        ts = (s.get("time") or {}).get("updated") or 0
        if ts >= best_ts:
            best_ts = ts
            best = sid
    return best


def create(info: server.ServerInfo, base: Path, text: str,
           timeout: float = DEFAULT_CREATE_TIMEOUT) -> tuple[str | None, list[str]]:
    """Create the primary agent as a server session bound to the base worktree
    and send the first message, waiting for the agent to finish or block. No
    headless `opencode run` loop is attached, so permission asks stay pending
    until a human answers them - the run loop would auto-reject them instead."""
    sess = harness.create_session(info.port, str(base), title=AGENT_TITLE)
    sid = (sess or {}).get("id")
    if not sid:
        return None, ["the server did not return a session id"]
    try:
        harness.prompt_async(info.port, sid, text)
    except harness.HarnessError as e:
        return None, [f"could not prompt the primary agent: {e}"]
    deadline = time.monotonic() + timeout
    seen_busy = False
    seen_reply = False
    while True:
        if time.monotonic() > deadline:
            return None, [f"agent did not finish within {timeout:.0f}s"]
        blocked = _pending_blocks(info.port, str(base), sid)
        if blocked:
            return sid, [f"blocked: {detail}" for _, detail in blocked]
        if _assistant_parts(info.port, sid):
            seen_reply = True
        busy = sid in (harness.session_status(info.port, str(base)) or {})
        if busy:
            seen_busy = True
        elif seen_busy or seen_reply:
            return sid, []
        time.sleep(0.5)


def _assistant_parts(port: int, sid: str) -> list[dict]:
    """All parts of assistant messages in <sid> - what the agent has produced."""
    out: list[dict] = []
    for m in harness.messages(port, sid) or []:
        info = (m or {}).get("info") if isinstance(m, dict) else None
        if not info or info.get("role") != "assistant":
            continue
        out.extend((m or {}).get("parts") or [])
    return out


def _pending_blocks(port: int, directory: str, sid: str) -> list[tuple[str, str]]:
    """(request_id, detail) for every permission/question ask pending on <sid>."""
    out: list[tuple[str, str]] = []
    for p in harness.permissions(port, directory):
        if str(p.get("sessionID") or "") != sid:
            continue
        rid = str(p.get("id") or p.get("requestID") or "?")
        out.append((rid, observe._describe_permission(p).detail))
    for q in harness.questions(port, directory):
        if str(q.get("sessionID") or "") != sid:
            continue
        rid = str(q.get("id") or q.get("requestID") or "?")
        out.append((rid, observe._describe_question(q).detail))
    return out


def _text_parts(parts) -> list[str]:
    out: list[str] = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") != "text":
            continue
        if p.get("synthetic"):
            continue
        t = p.get("text")
        if t:
            out.append(str(t))
    return out


def render_message(msg) -> str:
    parts = (msg or {}).get("parts") if isinstance(msg, dict) else None
    return "\n".join(_text_parts(parts)).strip()


def last_reply(port: int, session_id: str) -> str:
    for m in reversed(harness.messages(port, session_id) or []):
        info = (m or {}).get("info") if isinstance(m, dict) else None
        if not info or info.get("role") != "assistant":
            continue
        text = render_message(m)
        if text:
            return text
    return ""


def ask(info: server.ServerInfo, session_id: str, text: str,
        timeout: float = DEFAULT_ASK_TIMEOUT) -> tuple[str, str | None]:
    try:
        msg = harness.message(info.port, session_id, text, timeout=timeout)
    except harness.HarnessError as e:
        return "", str(e)
    return render_message(msg), None


def fire(info: server.ServerInfo, session_id: str, text: str) -> str | None:
    try:
        harness.prompt_async(info.port, session_id, text)
    except harness.HarnessError as e:
        return str(e)
    return None


def compact(info: server.ServerInfo, session_id: str,
            timeout: float = DEFAULT_ASK_TIMEOUT) -> tuple[bool, str | None]:
    """Run opencode's own compaction via POST /session/{id}/summarize."""
    sinfo = harness.session_info(info.port, session_id)
    if not sinfo:
        return False, f"could not read session {session_id}"
    model = sinfo.get("model") or {}
    pid = model.get("providerID")
    mid = model.get("id")
    if not pid or not mid:
        return False, f"session {session_id} has no model recorded"
    try:
        harness.summarize(info.port, session_id, pid, mid, timeout=timeout)
    except harness.HarnessError as e:
        return False, str(e)
    return True, None


def status(info: server.ServerInfo, base: Path) -> dict:
    sid = find(info.port, base)
    if sid is None:
        return {"session": None, "state": "none", "blocked": []}
    busy = sid in (harness.session_status(info.port, str(base)) or {})
    perms = [p for p in harness.permissions(info.port, str(base))
             if str(p.get("sessionID") or "") == sid]
    ques = [q for q in harness.questions(info.port, str(base))
            if str(q.get("sessionID") or "") == sid]
    if perms or ques:
        state_ = "blocked"
    elif busy:
        state_ = "busy"
    else:
        state_ = "idle"
    blocked = []
    for p in perms:
        blocked.append(observe._describe_permission(p).detail)
    for q in ques:
        blocked.append(observe._describe_question(q).detail)
    return {"session": sid, "state": state_, "blocked": blocked}


def _stream_part(part: dict) -> tuple[str, str] | None:
    """Map one message part from the messages API to ("text"|"thinking"|"tool"|
    "shell", payload), or None when it carries nothing worth showing."""
    ptype = part.get("type")
    if ptype == "text":
        if part.get("synthetic"):
            return None
        t = part.get("text")
        return ("text", str(t)) if t else None
    if ptype == "reasoning":
        t = part.get("text")
        return ("thinking", str(t)) if t else None
    if ptype == "tool":
        name = (part.get("tool") or part.get("name")
                or part.get("title") or "tool")
        inp = (part.get("state") or {}).get("input") or {}
        cmd = inp.get("command")
        if name in ("bash", "sh") and cmd:
            return "shell", str(cmd)
        return "tool", _tool_detail(name, part)
    return None


def _tool_detail(name: str, part: dict) -> str:
    """Render a tool call as `name(args)` so the stream shows what the agent is
    doing, capped so huge payloads (a full spec JSON, a long command) cannot
    flood the terminal. Session/message IDs are deliberately never surfaced."""
    inp = (part.get("state") or {}).get("input")
    if not inp:
        return name
    try:
        detail = json.dumps(inp, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        detail = str(inp)
    if len(detail) > 240:
        detail = detail[:237] + "..."
    return f"{name}({detail})"


def stream_reply(info: server.ServerInfo, base: Path, text: str,
                 session_id: str | None = None,
                 timeout: float = DEFAULT_ASK_TIMEOUT):
    """Send the message through the server session API and yield
    ("kind", "payload") tuples as the reply streams: "text" parts as they are
    produced, "[thinking]/[tool]/[shell]" markers, "blocked" when a permission
    or question is pending for this session (it stays pending - never
    auto-answered), and a final ("exit", "0"). With session_id it continues
    that session; without, it creates the primary agent. No headless
    `opencode run` loop is attached, so asks are never auto-rejected."""
    port = info.port
    if session_id is None:
        sess = harness.create_session(port, str(base), title=AGENT_TITLE)
        session_id = (sess or {}).get("id")
        if not session_id:
            raise harness.HarnessError("the server did not return a session id")
    try:
        harness.prompt_async(port, session_id, text)
    except harness.HarnessError as e:
        raise harness.HarnessError(f"could not prompt the primary agent: {e}") from e

    deadline = time.monotonic() + timeout
    n = 0
    seen_busy = False
    blocked_seen: set[str] = set()
    try:
        while True:
            if time.monotonic() > deadline:
                raise harness.HarnessError(
                    f"agent did not finish within {timeout:.0f}s")
            parts = _assistant_parts(port, session_id)
            for part in parts[n:]:
                n += 1
                rendered = _stream_part(part)
                if rendered is not None:
                    yield rendered
            blocks = _pending_blocks(port, str(base), session_id)
            for rid, detail in blocks:
                if rid not in blocked_seen:
                    blocked_seen.add(rid)
                    yield "blocked", detail
            busy = session_id in (harness.session_status(port, str(base)) or {})
            if busy:
                seen_busy = True
            if not busy and not blocks and (seen_busy or n > 0):
                break
            time.sleep(0.5)
    finally:
        pass
    yield "exit", "0"
