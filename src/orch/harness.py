from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

MIN_VERSION = (1, 18, 11)

DEFAULT_TIMEOUT = 5.0


class HarnessError(Exception):
    pass


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def get(port: int, path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    try:
        req = urllib.request.Request(_url(port, path), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def post(port: int, path: str, payload: Any = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    data = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        _url(port, path), data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        raise HarnessError(f"POST {path} failed: {e}") from e


def health(port: int) -> dict | None:
    h = get(port, "/global/health")
    if isinstance(h, dict) and h.get("version"):
        return h
    fallback = get(port, "/api/health")
    if isinstance(fallback, dict):
        merged = dict(fallback)
        if isinstance(h, dict):
            merged.update({k: v for k, v in h.items() if v is not None})
        return merged
    return h


def version(port: int) -> tuple[int, ...] | None:
    h = health(port) or {}
    raw = h.get("version")
    if not raw:
        return None
    parts: list[int] = []
    for chunk in str(raw).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def location(port: int) -> dict | None:
    return get(port, "/api/location")


def session_status(port: int, directory: str | None = None) -> dict:
    """Status of the sessions in `directory` (a workspace). The server only
    reports sessions for the workspace named in the query - without it the map
    is empty even while sessions are streaming - so callers must pass the
    worktree/base to see whether a session is busy."""
    path = "/session/status"
    if directory:
        from urllib.parse import quote
        path += f"?directory={quote(directory, safe='/')}"
    return get(port, path) or {}


def _parse_sse_data(line: str) -> dict | None:
    """Parse one SSE `data:` line into an event dict, or None if it is not one."""
    line = line.rstrip("\n")
    if not line.startswith("data:"):
        return None
    raw = line[len("data:"):].strip()
    if not raw:
        return None
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) and ev.get("type") else None


def event_stream(port: int, timeout: float = 30.0):
    """Yield opencode server events from GET /event (SSE) as dicts of the shape
    {"id": "evt_…", "type": "…", "properties": {…}}. Blocks on a live stream;
    raises OSError/TimeoutError when the transport fails so the caller reconnects.
    Read with readline(), never by iterating the response object."""
    req = urllib.request.Request(
        _url(port, "/event"), method="GET",
        headers={"Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            line = r.readline()
            if not line:
                return
            ev = _parse_sse_data(line.decode("utf-8", "replace"))
            if ev is not None:
                yield ev


def permissions(port: int, directory: str | None = None) -> list[dict]:
    """Pending permission asks, scoped to `directory` (a workspace). The server
    only lists asks for the workspace named in the query, so callers must pass
    the worktree to see a task's pending permissions."""
    path = "/permission"
    if directory:
        from urllib.parse import quote
        path += f"?directory={quote(directory, safe='/')}"
    return _as_list(get(port, path))


def questions(port: int, directory: str | None = None) -> list[dict]:
    path = "/question"
    if directory:
        from urllib.parse import quote
        path += f"?directory={quote(directory, safe='/')}"
    return _as_list(get(port, path))


def sessions(port: int, directory: str | None = None) -> list[dict]:
    path = "/session"
    if directory:
        from urllib.parse import quote
        path += f"?directory={quote(directory, safe='/')}"
    return _as_list(get(port, path))


def create_session(port: int, directory: str | None = None,
                   title: str | None = None) -> dict:
    """Create a session bound to `directory` (the worktree). Returns the
    session dict. No run loop is attached, so permission asks stay pending
    until a human answers them - the headless `opencode run` loop would
    auto-reject them instead."""
    from urllib.parse import quote
    path = "/session"
    if directory:
        path += f"?directory={quote(directory, safe='/')}"
    body: dict = {}
    if title:
        body["title"] = title
    raw = post(port, path, body)
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict):
            return data
        return raw
    return {}


def _as_list(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if isinstance(v, dict):
                out.append({"id": k, **v})
        return out
    return []

def abort(port: int, session_id: str) -> Any:
    return post(port, f"/session/{session_id}/abort")


def reply_permission(port: int, permission_id: str, reply: str,
                     directory: str | None = None) -> Any:
    """Answer a pending permission ask. `reply` is once|always|reject (the v2
    vocabulary). `directory` is the worktree the ask lives in - asks are
    workspace-scoped on the server, so it is required to reach the right one."""
    path = f"/permission/{permission_id}/reply"
    if directory:
        from urllib.parse import quote
        path += f"?directory={quote(directory, safe='/')}"
    return post(port, path, {"reply": reply})


def reply_question(port: int, request_id: str, answers: Any) -> Any:
    return post(port, f"/question/{request_id}/reply", {"answers": answers})


def prompt_async(port: int, session_id: str, text: str,
                 agent: str | None = None, model: str | None = None) -> Any:
    body: dict = {"parts": [{"type": "text", "text": text}]}
    if agent:
        body["agent"] = agent
    if model:
        body["model"] = model
    return post(port, f"/session/{session_id}/prompt_async", body)


def message(port: int, session_id: str, text: str,
            timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST /session/{id}/message - send and wait for the full reply."""
    return post(port, f"/session/{session_id}/message",
                {"parts": [{"type": "text", "text": text}]}, timeout=timeout)


def messages(port: int, session_id: str) -> list[dict]:
    return _as_list(get(port, f"/session/{session_id}/message"))


def session_info(port: int, session_id: str) -> dict | None:
    return get(port, f"/session/{session_id}")


def summarize(port: int, session_id: str, provider_id: str, model_id: str,
              timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST /session/{id}/summarize - run opencode's real compaction."""
    return post(port, f"/session/{session_id}/summarize",
                {"providerID": provider_id, "modelID": model_id,
                 "auto": False}, timeout=timeout)


@dataclass
class Capabilities:

    version: tuple[int, ...] | None = None
    healthy: bool = False
    too_old: bool = False
    notes: list[str] = field(default_factory=list)


def capabilities(port: int) -> Capabilities:
    h = health(port)
    if not h:
        return Capabilities(notes=["no response from /api/health"])
    v = version(port)
    caps = Capabilities(version=v, healthy=bool(h.get("healthy")))
    if v and v < MIN_VERSION:
        caps.too_old = True
        caps.notes.append(
            f"version {'.'.join(map(str, v))} is below the checked minimum "
            f"{'.'.join(map(str, MIN_VERSION))}"
        )
    return caps
