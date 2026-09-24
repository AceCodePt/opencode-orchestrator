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
    kind = p.get("action") or p.get("permission") or "?"
    meta = p.get("metadata") or {}
    detail = None
    if isinstance(meta, dict):
        for key in _PERM_TARGET_KEYS:
            val = meta.get(key)
            if val:
                detail = str(val)
                break
    if detail is None:
        target = p.get("resources") or p.get("patterns") or []
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


def _describe_form(f: dict) -> Blocked:
    """A V2 form is what V1 called a question: a title plus typed fields. The
    daemon treats it as `blocked-on-question`, answerable with `orch task
    answer`."""
    rid = str(f.get("id") or f.get("requestID") or "?")
    sid = str(f.get("sessionID") or "?")
    fields = f.get("fields") or []
    detail = str(f.get("title") or "question pending")
    if isinstance(fields, list) and fields and isinstance(fields[0], dict):
        detail = str(fields[0].get("title") or fields[0].get("key") or detail)
    return Blocked(kind="question", request_id=rid, session_id=sid, detail=detail)


def _ask_message_id(ask: dict) -> str | None:
    """The message a pending ask belongs to. V2 permissions carry
    `source: {type: "tool", messageID, id}`; V1 carried `tool.messageID`. A form
    has neither (None), so it is treated as a live block."""
    source = ask.get("source")
    if isinstance(source, dict):
        mid = source.get("messageID")
        if mid:
            return str(mid)
    tool = ask.get("tool")
    if isinstance(tool, dict):
        mid = tool.get("messageID")
        return str(mid) if mid else None
    return None


def _error(value: dict) -> dict | None:
    err = value.get("error")
    if isinstance(err, dict):
        return err
    info = value.get("info")
    if isinstance(info, dict) and isinstance(info.get("error"), dict):
        return info["error"]
    return None


def _is_abort(err: dict) -> bool:
    name = str(err.get("name") or err.get("type") or "")
    if "abort" in name.lower():
        return True
    return "abort" in str(err.get("message") or "").lower()


def _message_aborted(port: int, session_id: str, message_id: str) -> bool:
    """Whether a message ended in an abort, i.e. the turn that raised an ask was
    stopped by a user interrupt (the TUI stop) or died - so the ask it created is
    orphaned."""
    try:
        m = harness.message_by_id(port, session_id, message_id)
    except Exception:
        return False
    if not isinstance(m, dict):
        return False
    err = _error(m)
    return bool(err and _is_abort(err))


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
        role = m.get("role")
        if role is None and isinstance(m.get("info"), dict):
            role = m["info"].get("role")
        if role != "assistant" and m.get("type") != "assistant":
            continue
        err = _error(m)
        return bool(err and _is_abort(err))
    return False


def _session_interrupted(port: int, session_id: str) -> bool:
    """V2 records the turn outcome on the session (`outcome:
    interrupted`), which is sturdier than re-deriving it from the last message."""
    try:
        s = harness.session_info(port, session_id)
    except Exception:
        return False
    return bool(isinstance(s, dict) and s.get("outcome") == "interrupted")


def look_task(port: int, worktree: str) -> TaskFact | None:
    sess = harness.sessions(port, worktree)
    if not sess:
        return None

    ids = [str(s.get("id")) for s in sess if s.get("id")]
    primary = [str(s.get("id")) for s in sess
               if s.get("id") and not s.get("parentID")]
    watch = primary or ids

    active = harness.active_sessions(port)
    busy = [sid for sid in watch if sid in active]

    perms = [p for p in harness.permissions(port, worktree)
             if str(p.get("sessionID") or "") in set(ids)]
    forms = [q for q in harness.forms(port, worktree)
             if str(q.get("sessionID") or "") in set(ids)]

    interrupted = False
    orphaned: list[dict] = []
    if not busy and ids:
        sid = (primary or ids)[0]
        interrupted = (_session_interrupted(port, sid)
                       or _last_turn_aborted(port, sid))
        for ask in perms + forms:
            mid = _ask_message_id(ask)
            if mid and _message_aborted(port, sid, mid):
                orphaned.append(ask)

    orphan_ids = {str(a.get("id") or a.get("requestID") or "") for a in orphaned}
    live_perms = [p for p in perms
                  if str(p.get("id") or p.get("requestID") or "") not in orphan_ids]
    live_forms = [q for q in forms
                  if str(q.get("id") or q.get("requestID") or "") not in orphan_ids]

    if interrupted:
        state = INTERRUPTED
    elif live_perms:
        state = BLOCKED_PERMISSION
    elif live_forms:
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
        + [_describe_form(q) for q in live_forms],
    )
