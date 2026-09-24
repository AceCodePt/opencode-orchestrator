from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# OpenCode 2 replaced the V1 server API with an authenticated `/api/*` surface
# and a machine-managed background service. This module is the only place that
# knows the wire contract: paths, envelopes, and the service password. Every
# request carries HTTP Basic auth (user `opencode`, the password from
# `~/.local/state/opencode/service.json`), which `server.py` registers here when
# it discovers the service.
MIN_VERSION = (2, 0, 0)

DEFAULT_TIMEOUT = 30.0

_AUTH: dict[int, str] = {}


class HarnessError(Exception):
    pass


def set_auth(port: int, password: str | None) -> None:
    """Remember the service password for `port`, so every later call to it is
    authenticated. Called by server discovery; the password lives in opencode's
    own 0600 service.json."""
    if password:
        _AUTH[int(port)] = password


def clear_auth(port: int) -> None:
    _AUTH.pop(int(port), None)


def _auth_header(port: int) -> dict[str, str]:
    pw = _AUTH.get(int(port))
    if not pw:
        return {}
    token = base64.b64encode(f"opencode:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def get(port: int, path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    try:
        req = urllib.request.Request(
            _url(port, path), method="GET",
            headers={"Accept": "application/json", **_auth_header(port)},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def post(port: int, path: str, payload: Any = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    data = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        _url(port, path), data=data, method="POST",
        headers={"Content-Type": "application/json", **_auth_header(port)},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace").strip()
        except OSError:
            pass
        raise HarnessError(f"POST {path} failed: HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        raise HarnessError(f"POST {path} failed: {e}") from e


def _location_query(directory: str | None) -> str:
    """The V2 list endpoints scope by an object query `location[directory]=…`,
    not the flat `?directory=` the V1 server took. The session list is the one
    exception: it scopes by `?directory=` directly."""
    if not directory:
        return ""
    return "location[directory]=" + urllib.parse.quote(directory, safe="/")


def info(port: int) -> dict | None:
    """GET /api/info -> {version,pid,urls,paths}. This is the V2 replacement for
    `/global/health`: it is authenticated and carries the version."""
    i = get(port, "/api/info", timeout=5.0)
    return i if isinstance(i, dict) else None


def health(port: int) -> dict | None:
    """Normalized health: `{healthy, version, pid, url}` or None when the server
    does not answer/authenticate."""
    i = info(port)
    if not isinstance(i, dict) or not i.get("version"):
        return None
    urls = i.get("urls") or []
    return {
        "healthy": True,
        "version": i.get("version"),
        "pid": i.get("pid"),
        "url": urls[0] if urls else None,
    }


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


def location(port: int, directory: str | None = None) -> dict | None:
    path = "/api/location"
    q = _location_query(directory)
    if q:
        path += f"?{q}"
    return get(port, path)


def active_sessions(port: int) -> dict:
    """The sessions currently running, as `{sessionID: {type: "running"}}`. This
    replaces V1's `/session/status`: opencode reports `running` for the whole
    agent loop (model turns and long-running tools) and omits idle sessions."""
    raw = get(port, "/api/session/active")
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        return raw["data"]
    return {}


def sessions(port: int, directory: str | None = None) -> list[dict]:
    path = "/api/session"
    if directory:
        path += "?directory=" + urllib.parse.quote(directory, safe="/")
    return _as_list(get(port, path))


def session_info(port: int, session_id: str) -> dict | None:
    return _as_dict(get(port, f"/api/session/{session_id}"))


def create_session(port: int, directory: str | None = None,
                   title: str | None = None, agent: str | None = None,
                   model: Any = None) -> dict:
    """Create a session bound to `directory`. In V2 the location rides in the
    body; the session is created with no run loop, so permission asks stay
    pending until a human answers them."""
    body: dict = {}
    if directory:
        body["location"] = {"directory": directory}
    if title:
        body["title"] = title
    if agent:
        body["agent"] = agent
    if model:
        body["model"] = _model_ref(model)
    raw = post(port, "/api/session", body)
    return _as_dict(raw) or {}


def delete(port: int, path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    req = urllib.request.Request(
        _url(port, path), method="DELETE", headers=_auth_header(port),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def delete_session(port: int, session_id: str) -> Any:
    return delete(port, f"/api/session/{session_id}")


def _model_ref(model: Any) -> Any:
    if isinstance(model, dict):
        return model
    if isinstance(model, str) and "/" in model:
        provider_id, model_id = model.split("/", 1)
        return {"providerID": provider_id, "id": model_id}
    return model


def _as_dict(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if isinstance(data, dict):
        return data
    return value


def _as_list(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return [v for v in value["data"] if isinstance(v, dict)]
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
    return post(port, f"/api/session/{session_id}/interrupt", {})


def permissions(port: int, directory: str | None = None) -> list[dict]:
    """Pending permission asks, scoped to `directory`. V2 only returns asks for
    the location named in `location[directory]`, so callers must pass the
    worktree."""
    path = "/api/permission/request"
    q = _location_query(directory)
    if q:
        path += f"?{q}"
    return _as_list(get(port, path))


def reply_permission(port: int, session_id: str, permission_id: str, reply: str,
                     message: str | None = None) -> Any:
    """Answer a pending permission ask. In V2 the reply is session-scoped, and
    `reply` is `once`/`always`/`reject` (the server's `decision`). `message` is
    shown to the agent alongside the decision, so a rejection can say why."""
    body: dict = {"decision": reply}
    if message:
        body["message"] = message
    return post(port, f"/api/session/{session_id}/permission/{permission_id}/reply", body)


def forms(port: int, directory: str | None = None) -> list[dict]:
    """Pending forms (V2's replacement for V1 questions), location-scoped."""
    path = "/api/form"
    q = _location_query(directory)
    if q:
        path += f"?{q}"
    return _as_list(get(port, path))


def reply_form(port: int, session_id: str, form_id: str, answer: Any) -> Any:
    return post(port, f"/api/session/{session_id}/form/{form_id}/reply",
                {"answer": answer})


def prompt_async(port: int, session_id: str, text: str,
                 delivery: str = "queue") -> Any:
    """Queue a user turn without waiting for it. V2's `/prompt` admits the
    message into the session inbox; `delivery: "queue"` appends rather than
    steering an in-flight turn, which is what the daemon wants when injecting a
    verification failure."""
    body: dict = {"text": text, "delivery": delivery}
    return post(port, f"/api/session/{session_id}/prompt", body)


def message(port: int, session_id: str, text: str,
            timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST a prompt the daemon uses only to nudge a session onward."""
    return prompt_async(port, session_id, text)


def messages(port: int, session_id: str, limit: int | None = None) -> list[dict]:
    """The session's messages, newest first. V2 message infos are the objects
    themselves (`type` = user/assistant/…), not V1's `{info: {...}}` wrapper."""
    path = f"/api/session/{session_id}/message"
    if limit:
        path += f"?limit={limit}"
    return _as_list(get(port, path))


def message_by_id(port: int, session_id: str, message_id: str) -> dict | None:
    return _as_dict(get(port, f"/api/session/{session_id}/message/{message_id}"))


def summarize(port: int, session_id: str, provider_id: str, model_id: str,
              timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST /api/session/{id}/compact - run opencode's real compaction."""
    return post(port, f"/api/session/{session_id}/compact", {}, timeout=timeout)


def _parse_sse_data(line: str) -> dict | None:
    """Parse one SSE `data:` line into the V2 event dict
    `{id, created, type, location, data}`, or None if it is not one. Heartbeats
    arrive as `: heartbeat` comments and are ignored."""
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
    """Yield opencode server events from GET /api/event (SSE) as dicts of the
    shape {"id": "evt_…", "type": "…", "data": {…}, "location": {…}}. Blocks on
    a live stream; raises OSError/TimeoutError when the transport fails so the
    caller reconnects. Read with readline(), never by iterating the response
    object."""
    req = urllib.request.Request(
        _url(port, "/api/event"), method="GET",
        headers={"Accept": "text/event-stream", **_auth_header(port)},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            line = r.readline()
            if not line:
                return
            ev = _parse_sse_data(line.decode("utf-8", "replace"))
            if ev is not None:
                yield ev


@dataclass
class Capabilities:

    version: tuple[int, ...] | None = None
    healthy: bool = False
    too_old: bool = False
    notes: list[str] = field(default_factory=list)


def capabilities(port: int) -> Capabilities:
    h = health(port)
    if not h:
        return Capabilities(notes=["no response from /api/info"])
    v = version(port)
    caps = Capabilities(version=v, healthy=bool(h.get("healthy")))
    if v and v < MIN_VERSION:
        caps.too_old = True
        caps.notes.append(
            f"version {'.'.join(map(str, v))} is below the checked minimum "
            f"{'.'.join(map(str, MIN_VERSION))}"
        )
    return caps
