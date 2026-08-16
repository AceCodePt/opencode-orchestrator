from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import observe  # noqa: E402


class _Fake:
    def __init__(self, sessions=None, status=None, perms=None, ques=None,
                 messages=None, message_errors=None):
        self.sessions = sessions or [{"id": "ses_1"}]
        self.status = status or {}
        self.perms = perms or []
        self.ques = ques or []
        self.messages = messages or []
        self.message_errors = message_errors or {}
        self._orig = {}
        for name in ("sessions", "session_status", "permissions", "questions",
                     "messages", "message_by_id"):
            self._orig[name] = getattr(observe.harness, name)

    def install(self) -> None:
        observe.harness.sessions = lambda port, worktree: self.sessions
        observe.harness.session_status = lambda port, worktree: self.status
        observe.harness.permissions = lambda port, worktree: self.perms
        observe.harness.questions = lambda port, worktree: self.ques
        observe.harness.messages = lambda port, sid, limit=None: self.messages

        def message_by_id(port, sid, mid):
            return {"info": {"id": mid, "role": "assistant",
                             "error": self.message_errors.get(mid)}}

        observe.harness.message_by_id = message_by_id

    def restore(self) -> None:
        for name, fn in self._orig.items():
            setattr(observe.harness, name, fn)


def _ask(**kw) -> dict:
    base = {"id": "per_1", "sessionID": "ses_1",
            "tool": {"messageID": "m_old", "callID": "c1"}}
    base.update(kw)
    return base


def _msg(error, mid="m_latest", role="assistant") -> dict:
    return {"info": {"id": mid, "role": role, "error": error}}


ABORTED = {"name": "MessageAbortedError", "data": {"message": "Aborted"}}


def test_last_turn_aborted_is_interrupted_and_orphans_its_ask() -> None:
    fake = _Fake(perms=[_ask()], messages=[_msg(ABORTED)],
                 message_errors={"m_old": ABORTED})
    fake.install()
    try:
        tf = observe.look_task(4096, "/wt")
        assert tf is not None
        assert tf.state == observe.INTERRUPTED, tf.state
        assert tf.interrupted is True
        assert [a["id"] for a in tf.orphaned] == ["per_1"]
        assert tf.blocked == [], "an aborted turn's ask is not a live block"
    finally:
        fake.restore()
    print("ok: an aborted turn is interrupted and its ask is orphaned, not blocked")


def test_a_finished_turn_with_an_orphaned_ask_is_idle_not_blocked() -> None:
    """The real stuck-task shape: the last turn completed, but an older ask
    whose owning message was aborted still lingers. It must not freeze the
    task as blocked."""
    fake = _Fake(perms=[_ask()], messages=[_msg(None)],
                 message_errors={"m_old": ABORTED})
    fake.install()
    try:
        tf = observe.look_task(4096, "/wt")
        assert tf is not None
        assert tf.state == observe.IDLE, tf.state
        assert tf.interrupted is False
        assert [a["id"] for a in tf.orphaned] == ["per_1"]
        assert tf.blocked == []
    finally:
        fake.restore()
    print("ok: a finished turn with an orphaned ask is idle, not blocked")


def test_a_live_pending_ask_is_still_blocked() -> None:
    fake = _Fake(perms=[_ask()], messages=[_msg(None)],
                 message_errors={"m_old": None})
    fake.install()
    try:
        tf = observe.look_task(4096, "/wt")
        assert tf is not None
        assert tf.state == observe.BLOCKED_PERMISSION, tf.state
        assert tf.interrupted is False
        assert tf.orphaned == []
        assert [b.request_id for b in tf.blocked] == ["per_1"]
    finally:
        fake.restore()
    print("ok: a live pending ask is still blocked")


def test_busy_session_is_busy_not_interrupted() -> None:
    fake = _Fake(status={"ses_1": "busy"}, messages=[_msg(ABORTED)])
    fake.install()
    try:
        tf = observe.look_task(4096, "/wt")
        assert tf is not None
        assert tf.state == observe.BUSY, tf.state
        assert tf.interrupted is False
        assert tf.orphaned == []
    finally:
        fake.restore()
    print("ok: a busy session is never misread as interrupted")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} observe tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
