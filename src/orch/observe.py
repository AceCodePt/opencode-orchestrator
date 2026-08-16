from __future__ import annotations

from dataclasses import dataclass, field

from . import harness

BLOCKED_PERMISSION = "blocked-on-permission"
BLOCKED_QUESTION = "blocked-on-question"
BUSY = "busy"
IDLE = "idle"
INTERRUPTED = "interrupted"


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
    interrupted: bool = False
    orphaned: list[dict] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.state in (BLOCKED_PERMISSION, BLOCKED_QUESTION)


_PERM_TARGET_KEYS = ("filepath", "filePath", "path", "command", "url",
                     "script", "target")


def _describe_permission(p: dict) -> Blocked:
    rid = str(p.get("id") or p.get("requestID") or "?")
    sid = str(p.get("sessionID") or "?")
    kind = p.get("permission") or p.get("action") or "?"
    meta = p.get("metadata") or {}
    detail = None
    if isinstance(meta, dict):
        for key in _PERM_TARGET_KEYS:
            val = meta.get(key)
            if val:
                detail = str(val)
                break
    if detail is None:
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


def _ask_message_id(ask: dict) -> str | None:
    """The message a pending ask belongs to, from the ask's `tool.messageID`.
    None when the server did not attach one - such an ask is treated as a live
    block (the safe default)."""
    tool = ask.get("tool")
    if isinstance(tool, dict):
        mid = tool.get("messageID")
        return str(mid) if mid else None
    return None


def _message_aborted(port: int, session_id: str, message_id: str) -> bool:
    """Whether a message ended in an abort (`MessageAbortedError`), i.e. the
    turn that raised an ask was stopped by a user interrupt (the TUI stop) or
    died for another reason - so the ask it created is orphaned."""
    try:
        m = harness.message_by_id(port, session_id, message_id)
    except Exception:
        return False
    if not isinstance(m, dict):
        return False
    info = m.get("info")
    if not isinstance(info, dict):
        return False
    err = info.get("error")
    return bool(isinstance(err, dict) and err.get("name") == "MessageAbortedError")


def _last_turn_aborted(port: int, session_id: str) -> bool:
    """Whether the session's last assistant message was aborted - the task is
    currently interrupted (a user stopped it in the TUI, or its turn died)."""
    try:
        msgs = harness.messages(port, session_id, limit=5)
    except Exception:
        return False
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        info = m.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        err = info.get("error")
        return bool(isinstance(err, dict) and err.get("name") == "MessageAbortedError")
    return False


def look_task(port: int, worktree: str) -> TaskFact | None:
    sess = harness.sessions(port, worktree)
    if not sess:
        return None

    ids = [str(s.get("id")) for s in sess if s.get("id")]
    primary = [str(s.get("id")) for s in sess
               if s.get("id") and not s.get("parentID")]
    watch = primary or ids

    status = harness.session_status(port, worktree)
    busy = [sid for sid in watch if sid in status]

    perms = [p for p in harness.permissions(port, worktree)
             if str(p.get("sessionID") or "") in set(ids)]
    ques = [q for q in harness.questions(port, worktree)
            if str(q.get("sessionID") or "") in set(ids)]

    interrupted = False
    orphaned: list[dict] = []
    if not busy and ids:
        sid = (primary or ids)[0]
        interrupted = _last_turn_aborted(port, sid)
        for ask in perms + ques:
            mid = _ask_message_id(ask)
            if mid and _message_aborted(port, sid, mid):
                orphaned.append(ask)

    orphan_ids = {str(a.get("id") or a.get("requestID") or "") for a in orphaned}
    live_perms = [p for p in perms
                  if str(p.get("id") or p.get("requestID") or "") not in orphan_ids]
    live_ques = [q for q in ques
                 if str(q.get("id") or q.get("requestID") or "") not in orphan_ids]

    if interrupted:
        state = INTERRUPTED
    elif live_perms:
        state = BLOCKED_PERMISSION
    elif live_ques:
        state = BLOCKED_QUESTION
    elif busy:
        state = BUSY
    else:
        state = IDLE

    return TaskFact(
        worktree=worktree,
        session_ids=watch,
        state=state,
        interrupted=interrupted,
        orphaned=orphaned,
        blocked=[_describe_permission(p) for p in live_perms]
        + [_describe_question(q) for q in live_ques],
    )
