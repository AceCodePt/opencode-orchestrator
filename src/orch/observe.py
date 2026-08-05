from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import harness, server
from .discovery import Tui, discover

UNOBSERVABLE = "unobservable"
UNREACHABLE = "unreachable"
BLOCKED_PERMISSION = "blocked-on-permission"
BLOCKED_QUESTION = "blocked-on-question"
BUSY = "busy"
IDLE = "idle"
SHARED_SERVER = "shared-server"


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
        return self.state in (UNOBSERVABLE, UNREACHABLE)

    @property
    def is_agent(self) -> bool:
        return self.state != SHARED_SERVER


def _describe_permission(p: dict) -> Blocked:
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
    if not tui.observable:
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


@dataclass
class TaskFact:

    worktree: str
    session_ids: list[str] = field(default_factory=list)
    state: str = IDLE
    blocked: list[Blocked] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.state in (BLOCKED_PERMISSION, BLOCKED_QUESTION)


def look_task(port: int, worktree: str) -> TaskFact | None:
    sess = harness.sessions(port, worktree)
    if not sess:
        return None

    ids = [str(s.get("id")) for s in sess if s.get("id")]
    primary = [str(s.get("id")) for s in sess
               if s.get("id") and not s.get("parentID")]
    watch = primary or ids

    status = harness.session_status(port)
    busy = [sid for sid in watch if sid in status]

    perms = [p for p in harness.permissions(port)
             if str(p.get("sessionID") or "") in set(ids)]
    ques = [q for q in harness.questions(port)
            if str(q.get("sessionID") or "") in set(ids)]

    if perms:
        state = BLOCKED_PERMISSION
    elif ques:
        state = BLOCKED_QUESTION
    elif busy:
        state = BUSY
    else:
        state = IDLE

    return TaskFact(
        worktree=worktree,
        session_ids=watch,
        state=state,
        blocked=[_describe_permission(p) for p in perms]
        + [_describe_question(q) for q in ques],
    )


def look_all() -> list[Fact]:
    facts = [look(t) for t in discover()]
    info = server.read_state()
    if info is not None and info.alive:
        for f in facts:
            if f.tui.pid == info.pid:
                f.state = SHARED_SERVER
                f.notes.append(
                    "the orchestrator's shared server: it hosts every dispatched "
                    "task, so its death takes them all with it"
                )
    return facts


_ICON = {
    SHARED_SERVER: "==",
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
    agents = [f for f in facts if f.is_agent]
    summary = [f"{len(agents)} agent(s)"]
    if len(agents) != len(facts):
        summary.append("1 shared server")
    if waiting:
        summary.append(f"{len(waiting)} waiting on you")
    if blind:
        summary.append(f"{len(blind)} unobservable (no --port)")
    lines.append("")
    lines.append(" | ".join(summary))
    return "\n".join(lines)
