"""HTTP client for the opencode harness.

All harness knowledge lives on the Python side (see docs/architecture.md), so this
module is the single place that knows opencode's endpoint vocabulary. The TS router
plugin deliberately knows none of it.

Read-only helpers are used by the observer, which must never write. Mutating calls
live at the bottom and are for the daemon only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

# The oldest opencode this code has been checked against. Local is 1.18.13; the box
# is pinned to 1.18.11. Endpoints must degrade rather than lie across that gap.
MIN_VERSION = (1, 18, 11)

DEFAULT_TIMEOUT = 5.0


class HarnessError(Exception):
    pass


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def get(port: int, path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET and decode JSON. Returns None when the endpoint is unreachable.

    Unreachable is a normal condition, not an error: a TUI can exit between
    discovery and query.
    """
    try:
        req = urllib.request.Request(_url(port, path), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def post(port: int, path: str, payload: Any = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST JSON. Only the daemon should call this; the observer must not."""
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


# --------------------------------------------------------------------------- read


def health(port: int) -> dict | None:
    """Liveness plus version.

    Two health endpoints exist and they are not interchangeable:
    `/api/health` returns only `{"healthy": true}`, while `/global/health` returns
    `{"healthy": true, "version": "1.18.13"}`. Version negotiation therefore has to
    use the global one, with the api one as a liveness-only fallback.
    """
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
    """Maps a port to the directory it was started in."""
    return get(port, "/api/location")


def session_status(port: int) -> dict:
    """`{}` means every session is idle. A populated dict means work in flight.

    Verified against a live TUI: a busy session appears as
    `{"ses_…": {"type": "busy"}}`.
    """
    return get(port, "/session/status") or {}


def permissions(port: int) -> list[dict]:
    """Pending approval requests. Non-empty means an agent is blocked on a human.

    Use `/permission`, not `/api/permission/request`: the latter returned empty
    against a session that was demonstrably blocked.
    """
    return _as_list(get(port, "/permission"))


def questions(port: int) -> list[dict]:
    """Pending questions. Non-empty means an agent is blocked on a human."""
    return _as_list(get(port, "/question"))


def todos(port: int, session_id: str) -> list[dict]:
    return _as_list(get(port, f"/session/{session_id}/todo"))


def _as_list(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        # Some endpoints key by id rather than returning an array.
        out = []
        for k, v in value.items():
            if isinstance(v, dict):
                out.append({"id": k, **v})
        return out
    return []


def events(port: int, timeout: float | None = None) -> Iterator[dict]:
    """Yield decoded SSE events from `GET /event`.

    SSE catches transitions; polling catches absence. The watchdog needs both,
    because "no event" is never itself an event.
    """
    req = urllib.request.Request(_url(port, "/event"), method="GET",
                                 headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Read with readline(), not `for line in r`. Iterating an HTTPResponse
        # buffers, so an idle stream yields nothing until enough bytes accumulate -
        # which for a low-traffic event feed means appearing to receive nothing at
        # all. readline() returns as soon as a line lands.
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


# ------------------------------------------------------------------------- write
# Daemon only. The observer never calls anything below this line.


def reply_permission(port: int, request_id: str, reply: str) -> Any:
    return post(port, f"/permission/{request_id}/reply", {"reply": reply})


def reply_question(port: int, request_id: str, answers: Any) -> Any:
    return post(port, f"/question/{request_id}/reply", {"answers": answers})


def abort(port: int, session_id: str) -> Any:
    return post(port, f"/session/{session_id}/abort")


def prompt_async(port: int, session_id: str, text: str) -> Any:
    """Inject a message into a session.

    Use this, never `/tui/append-prompt` + `/tui/submit-prompt`. Driving the TUI
    text box destroys whatever a human was typing, requires a TUI to exist, and
    races with the user.
    """
    return post(port, f"/session/{session_id}/prompt_async",
                {"parts": [{"type": "text", "text": text}]})


# ------------------------------------------------------------- event vocabulary
# Extracted from the live OpenAPI document (89 event variants). See docs/events.md.

# Observed on the wire but absent from the OpenAPI schema list.
# `server.heartbeat` is the one that matters: it separates "stream alive, agent
# quiet" from "stream dead", which is exactly the absence problem the watchdog has
# to solve. Missing heartbeats mean the transport failed, not that work finished.
EV_SERVER_CONNECTED = "server.connected"
EV_SERVER_HEARTBEAT = "server.heartbeat"
HEARTBEAT_PERIOD_S = 10.0  # measured ~8-10s

EV_IDLE = "session.idle"
EV_STATUS = "session.status"
EV_ERROR = "session.error"
EV_DIFF = "session.diff"
EV_COMPACTED = "session.compacted"

# Two generations coexist. A reader must handle both: v1 carries
# `permission`/`patterns`/`always`, v2 carries `action`/`resources`/`save`.
EV_PERMISSION_ASKED = ("permission.asked", "permission.v2.asked")
EV_PERMISSION_REPLIED = ("permission.replied", "permission.v2.replied")
EV_QUESTION_ASKED = ("question.asked", "question.v2.asked")
EV_QUESTION_REPLIED = ("question.replied", "question.v2.replied")
EV_QUESTION_REJECTED = ("question.rejected", "question.v2.rejected")

# Exact tool-call boundaries, so "busy running a long tool" is observed rather
# than guessed. `progress` lets a slow tool prove it is alive.
EV_TOOL_CALLED = "session.next.tool.called"
EV_TOOL_SUCCESS = "session.next.tool.success"
EV_TOOL_FAILED = "session.next.tool.failed"
EV_TOOL_PROGRESS = "session.next.tool.progress"

EV_SHELL_STARTED = "session.next.shell.started"
EV_SHELL_ENDED = "session.next.shell.ended"

# `step.ended` carries cost and tokens, which is the cheap proxy for loop
# detection when paired with a static todo list.
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
    """What this particular server supports, so callers degrade instead of lying."""

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
