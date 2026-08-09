from __future__ import annotations

from dataclasses import dataclass, field

from . import harness

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
