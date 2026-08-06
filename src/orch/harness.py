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


def prompt_async(port: int, session_id: str, text: str) -> Any:
    return post(port, f"/session/{session_id}/prompt_async",
                {"parts": [{"type": "text", "text": text}]})


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
