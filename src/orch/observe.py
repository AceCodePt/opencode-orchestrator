"""The observer: sensing only.

Reports facts and never verdicts. It does not decide that a session has been busy
too long — budgets, dedup and notification are the daemon's policy. Keeping the
judgement out of here is what stops the silence logic from being split across two
components where nobody can find it.

The observer also never writes. Because it holds no authority, it can never make a
bad situation worse, which is what makes it safe to run standalone when the daemon
itself is the broken thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import harness
from .discovery import Tui, discover

# States are mutually exclusive and ordered by how much they demand attention.
UNOBSERVABLE = "unobservable"
UNREACHABLE = "unreachable"
BLOCKED_PERMISSION = "blocked-on-permission"
BLOCKED_QUESTION = "blocked-on-question"
BUSY = "busy"
IDLE = "idle"


@dataclass
class Blocked:
    kind: str
    request_id: str
    session_id: str
    detail: str
    always: Any = None


@dataclass
class Fact:
    tui: Tui
    state: str
    worktree: str | None = None
    version: str | None = None
    busy_sessions: list[str] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.state in (BLOCKED_PERMISSION, BLOCKED_QUESTION)

    @property
    def is_alarm(self) -> bool:
        """States that mean the machinery itself is wrong, not the agent."""
        return self.state in (UNOBSERVABLE, UNREACHABLE)


def _describe_permission(p: dict) -> Blocked:
    """Handle both permission generations.

    v1 carries `permission`/`patterns`/`always`; v2 carries
    `action`/`resources`/`save`. Both are present in the API surface, so a reader
    that assumes one will silently see nothing on a server speaking the other.
    """
    rid = str(p.get("id") or p.get("requestID") or "?")
    sid = str(p.get("sessionID") or "?")
    kind = p.get("permission") or p.get("action") or "?"
    target = p.get("patterns") or p.get("resources") or []
    if isinstance(target, (list, tuple)):
        detail = ", ".join(str(t) for t in target) or str(kind)
    else:
        detail = str(target)
    return Blocked(
        kind=str(kind),
        request_id=rid,
        session_id=sid,
        detail=detail,
        always=p.get("always") or p.get("save"),
    )


def _describe_question(q: dict) -> Blocked:
    rid = str(q.get("id") or q.get("requestID") or "?")
    sid = str(q.get("sessionID") or "?")
    qs = q.get("questions") or []
    if isinstance(qs, list) and qs:
        first = qs[0]
        detail = str(first.get("question") if isinstance(first, dict) else first)
    else:
        detail = "question pending"
    return Blocked(kind="question", request_id=rid, session_id=sid, detail=detail)


def look(tui: Tui) -> Fact:
    """Read one TUI. Never raises for an absent or dying server."""
    if not tui.observable:
        # KNOWN AMBIGUITY: this state is also what a *dying* TUI looks like.
        # Teardown is not atomic - after `tmux kill-session` a process outlives its
        # pane by seconds and loses its listener before it exits, so "alive, no port"
        # is indistinguishable from "launched without --port" in a single sample.
        # One is a corpse to reap, the other is an agent that will run unobserved
        # forever. The daemon must corroborate before acting; a `(deleted)` cwd or a
        # missing tmux session is a hint that this is the former.
        dying = bool(tui.cwd and tui.cwd.endswith("(deleted)"))
        note = (
            "started without --port, so it has no HTTP server and cannot be "
            "inspected; only relaunching helps"
        )
        if dying:
            note = (
                "cwd is deleted and there is no listener - probably shutting down "
                "rather than launched without --port; confirm before acting"
            )
        return Fact(
            tui=tui,
            state=UNOBSERVABLE,
            worktree=tui.cwd,
            notes=[note],
        )

    port = tui.port
    assert port is not None
    caps = harness.capabilities(port)
    if not caps.healthy and caps.version is None:
        return Fact(
            tui=tui,
            state=UNREACHABLE,
            worktree=tui.cwd,
            notes=["port is open but /api/health did not answer"] + caps.notes,
        )

    loc = harness.location(port) or {}
    worktree = loc.get("directory") or tui.cwd
    version = ".".join(map(str, caps.version)) if caps.version else None

    perms = harness.permissions(port)
    ques = harness.questions(port)
    status = harness.session_status(port)
    busy = sorted(status.keys())

    if perms:
        state = BLOCKED_PERMISSION
    elif ques:
        state = BLOCKED_QUESTION
    elif busy:
        state = BUSY
    else:
        state = IDLE

    return Fact(
        tui=tui,
        state=state,
        worktree=worktree,
        version=version,
        busy_sessions=busy,
        blocked=[_describe_permission(p) for p in perms]
        + [_describe_question(q) for q in ques],
        notes=list(caps.notes),
    )


def look_all() -> list[Fact]:
    return [look(t) for t in discover()]


_ICON = {
    UNOBSERVABLE: "??",
    UNREACHABLE: "!!",
    BLOCKED_PERMISSION: ">>",
    BLOCKED_QUESTION: ">>",
    BUSY: "**",
    IDLE: "..",
}


def render(facts: list[Fact], verbose: bool = False) -> str:
    if not facts:
        return "no opencode TUIs running"

    lines: list[str] = []
    width = max((len(f.worktree or "?") for f in facts), default=10)
    width = min(width, 60)

    for f in sorted(facts, key=lambda x: (x.state, x.tui.pid)):
        wt = f.worktree or "?"
        if len(wt) > width:
            wt = "…" + wt[-(width - 1):]
        port = str(f.tui.port) if f.tui.port else "-"
        tmux = f" tmux={f.tui.tmux}" if f.tui.tmux else ""
        lines.append(
            f"{_ICON.get(f.state, '  ')} {wt:<{width}}  pid={f.tui.pid:<7} "
            f"port={port:<6} {f.state}{tmux}"
        )
        for b in f.blocked:
            extra = f" always={b.always}" if (verbose and b.always) else ""
            lines.append(f"     ↳ {b.kind}: {b.detail}  [{b.request_id}]{extra}")
        if verbose:
            for s in f.busy_sessions:
                lines.append(f"     ↳ busy session {s}")
            for n in f.notes:
                lines.append(f"     ! {n}")

    blind = [f for f in facts if f.state == UNOBSERVABLE]
    waiting = [f for f in facts if f.needs_human]
    summary = [f"{len(facts)} TUI(s)"]
    if waiting:
        summary.append(f"{len(waiting)} waiting on you")
    if blind:
        summary.append(f"{len(blind)} unobservable (no --port)")
    lines.append("")
    lines.append(" | ".join(summary))
    return "\n".join(lines)
