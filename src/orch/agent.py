from __future__ import annotations

import os
from pathlib import Path

from . import harness, observe, server, state

AGENT_TITLE = "orch:agent"


def project_of(start: Path) -> tuple[state.Project | None, str | None]:
    proj = state.Project.from_dir(start)
    if proj is None:
        return None, ("not in a worktree - run `orch attach` from a project "
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


def ensure(port: int, base: Path) -> str:
    """The primary agent's session id in <base>, creating it if the project
    has none yet. No run loop is attached, so a session created here holds
    permission asks pending until a human answers them in the attached TUI."""
    sid = find(port, base)
    if sid is not None:
        return sid
    sess = harness.create_session(port, str(base), title=AGENT_TITLE)
    sid = (sess or {}).get("id")
    if not sid:
        raise harness.HarnessError("the server did not return a session id")
    return sid


def find_task(port: int, worktree: Path, task: str) -> str | None:
    """The id of the newest session titled <task> in <worktree>."""
    best: str | None = None
    best_ts = -1
    for s in harness.sessions(port, directory=str(worktree)):
        if s.get("title") != task:
            continue
        sid = s.get("id")
        if not sid:
            continue
        ts = (s.get("time") or {}).get("updated") or 0
        if ts >= best_ts:
            best_ts = ts
            best = sid
    return best


def ensure_task(port: int, worktree: Path, task: str) -> str:
    """The task's session id in <worktree>, creating it if the task has none
    yet. Like ensure for the primary agent, the session is created without a
    run loop, so a human attaching drives the agent in that worktree and any
    permission asks stay pending until answered."""
    sid = find_task(port, worktree, task)
    if sid is not None:
        return sid
    sess = harness.create_session(port, str(worktree), title=task)
    sid = (sess or {}).get("id")
    if not sid:
        raise harness.HarnessError("the server did not return a session id")
    return sid


def task_at(proj: state.Project, port: int, start: Path) -> state.TaskState | None:
    """The claimed task whose worktree <start> is inside, if any. A declared
    but unclaimed task has no worktree and is never returned."""
    real = os.path.realpath(start)
    for t in proj.scan(port):
        if not t.claimed or not t.worktree:
            continue
        wt = os.path.realpath(t.worktree)
        if real == wt or real.startswith(wt + os.sep):
            return t
    return None


def status(info: server.ServerInfo, base: Path) -> dict:
    sid = find(info.port, base)
    if sid is None:
        return {"session": None, "state": "none", "blocked": []}
    busy = sid in harness.active_sessions(info.port)
    perms = [p for p in harness.permissions(info.port, str(base))
             if str(p.get("sessionID") or "") == sid]
    forms = [q for q in harness.forms(info.port, str(base))
             if str(q.get("sessionID") or "") == sid]
    if perms or forms:
        state_ = "blocked"
    elif busy:
        state_ = "busy"
    else:
        state_ = "idle"
    blocked = []
    for p in perms:
        blocked.append(observe._describe_permission(p).detail)
    for q in forms:
        blocked.append(observe._describe_form(q).detail)
    return {"session": sid, "state": state_, "blocked": blocked}
