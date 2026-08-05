from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

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


def session_status(port: int) -> dict:
    return get(port, "/session/status") or {}


def permissions(port: int) -> list[dict]:
    return _as_list(get(port, "/permission"))


def questions(port: int) -> list[dict]:
    return _as_list(get(port, "/question"))


def sessions(port: int, directory: str | None = None) -> list[dict]:
    path = "/session"
    if directory:
        from urllib.parse import quote
        path += f"?directory={quote(directory, safe='/')}"
    return _as_list(get(port, path))


def todos(port: int, session_id: str) -> list[dict]:
    return _as_list(get(port, f"/session/{session_id}/todo"))


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


def events(port: int, timeout: float | None = None) -> Iterator[dict]:
    req = urllib.request.Request(_url(port, "/event"), method="GET",
                                 headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            raw = r.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def reply_permission(port: int, request_id: str, reply: str) -> Any:
    return post(port, f"/permission/{request_id}/reply", {"reply": reply})


def reply_question(port: int, request_id: str, answers: Any) -> Any:
    return post(port, f"/question/{request_id}/reply", {"answers": answers})


def abort(port: int, session_id: str) -> Any:
    return post(port, f"/session/{session_id}/abort")


def prompt_async(port: int, session_id: str, text: str) -> Any:
    return post(port, f"/session/{session_id}/prompt_async",
                {"parts": [{"type": "text", "text": text}]})


EV_SERVER_CONNECTED = "server.connected"
EV_SERVER_HEARTBEAT = "server.heartbeat"
HEARTBEAT_PERIOD_S = 10.0

EV_IDLE = "session.idle"
EV_STATUS = "session.status"
EV_ERROR = "session.error"
EV_DIFF = "session.diff"
EV_COMPACTED = "session.compacted"

EV_PERMISSION_ASKED = ("permission.asked", "permission.v2.asked")
EV_PERMISSION_REPLIED = ("permission.replied", "permission.v2.replied")
EV_QUESTION_ASKED = ("question.asked", "question.v2.asked")
EV_QUESTION_REPLIED = ("question.replied", "question.v2.replied")
EV_QUESTION_REJECTED = ("question.rejected", "question.v2.rejected")

EV_TOOL_CALLED = "session.next.tool.called"
EV_TOOL_SUCCESS = "session.next.tool.success"
EV_TOOL_FAILED = "session.next.tool.failed"
EV_TOOL_PROGRESS = "session.next.tool.progress"

EV_SHELL_STARTED = "session.next.shell.started"
EV_SHELL_ENDED = "session.next.shell.ended"

EV_STEP_STARTED = "session.next.step.started"
EV_STEP_ENDED = "session.next.step.ended"
EV_STEP_FAILED = "session.next.step.failed"
EV_RETRIED = "session.next.retried"
EV_TODO_UPDATED = "todo.updated"

BLOCKING_EVENTS = set(EV_PERMISSION_ASKED) | set(EV_QUESTION_ASKED)
UNBLOCKING_EVENTS = (
    set(EV_PERMISSION_REPLIED) | set(EV_QUESTION_REPLIED) | set(EV_QUESTION_REJECTED)
)


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
