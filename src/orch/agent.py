from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import envfilter, harness, observe, server, state

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
    """Create the primary agent by sending the first message through a
    one-shot `opencode run` bound to the base worktree."""
    cmd = ["opencode", "run", "--attach", info.url, "--dir", str(base),
           "--format", "json", "--title", AGENT_TITLE, text]
    try:
        p = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True,
                           timeout=timeout,
                           env=envfilter.filtered_env(
                               os.environ, repo_drop=envfilter.repo_drops(base)))
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, [f"could not start the primary agent: {e}"]
    if p.returncode != 0:
        return None, [f"primary agent exited {p.returncode}: "
                      f"{(p.stderr or p.stdout)[-500:]}"]
    sid = find(info.port, base)
    if sid is None:
        return None, ["the agent ran but its session was not found; try again"]
    return sid, []


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
    busy = sid in (harness.session_status(info.port) or {})
    perms = [p for p in harness.permissions(info.port)
             if str(p.get("sessionID") or "") == sid]
    ques = [q for q in harness.questions(info.port)
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


def _stream_event(ev: dict) -> tuple[str, str] | None:
    """Map one `opencode run --format json` event to ("text"|"tool"|"shell"|
    "error", payload), or None when it carries nothing worth showing."""
    kind = ev.get("type")
    part = ev.get("part") or {}
    ptype = part.get("type")
    if kind == "text" or ptype == "text":
        t = part.get("text")
        if t:
            return "text", str(t)
    if ptype == "tool":
        name = (part.get("tool") or part.get("name")
                or part.get("title") or "tool")
        return "tool", str(name)
    if ptype == "shell":
        return "shell", "shell"
    if kind == "error":
        detail = part.get("error") or ev.get("error") or "error"
        return "error", str(detail)
    return None


def stream_reply(info: server.ServerInfo, base: Path, text: str,
                 session_id: str | None = None,
                 timeout: float = DEFAULT_ASK_TIMEOUT):
    """Run the message through a one-shot `opencode run --attach` and yield
    ("kind", "payload") tuples as the reply streams: "text" parts as they are
    produced, "[tool]/[shell]" markers, "error" on a hard failure, and a final
    ("exit", "<code>") so the caller can judge success. With session_id it
    continues that session; without, it creates the primary agent."""
    cmd = ["opencode", "run", "--attach", info.url, "--dir", str(base),
           "--format", "json"]
    if session_id:
        cmd += ["-s", session_id]
    else:
        cmd += ["--title", AGENT_TITLE]
    cmd.append(text)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(base), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            env=envfilter.filtered_env(
                os.environ, repo_drop=envfilter.repo_drops(base)),
        )
    except OSError as e:
        raise harness.HarnessError(f"could not start the streaming agent: {e}") from e

    deadline = time.monotonic() + timeout
    rc: int | None = None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise harness.HarnessError(
                    f"agent did not finish within {timeout:.0f}s")
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            rendered = _stream_event(ev)
            if rendered is not None:
                yield rendered
        rc = proc.wait()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
    yield "exit", str(rc or 0)
