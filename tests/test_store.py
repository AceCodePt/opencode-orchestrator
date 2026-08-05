from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch.store import RUNNING, STUCK, TODO, Store  # noqa: E402

PROJECT = "/tmp/lab/lab.git"


def _store() -> tuple[Store, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "orch.db"), tmp


def test_sync_is_idempotent() -> None:
    s, tmp = _store()
    assert s.sync_intent(PROJECT, ["a", "b", "c"]) == 3
    assert s.sync_intent(PROJECT, ["a", "b", "c"]) == 0, "re-sync must not duplicate"
    assert [t.name for t in s.tasks(PROJECT)] == ["a", "b", "c"]
    s.close(); tmp.cleanup()
    print("ok: sync_intent idempotent")


def test_claim_is_exclusive() -> None:
    s, tmp = _store()
    s.sync_intent(PROJECT, ["only"])
    assert s.claim(PROJECT) == "only"
    assert s.claim(PROJECT) is None, "a claimed task must not be claimable again"
    s.close(); tmp.cleanup()
    print("ok: claim is exclusive")


def test_claim_under_concurrency() -> None:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "orch.db"
    boot = Store(path)
    boot.sync_intent(PROJECT, ["contested"])
    boot.close()

    winners: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        st = Store(path)
        try:
            got = st.claim(PROJECT)
            if got:
                with lock:
                    winners.append(got)
        finally:
            st.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert winners == ["contested"], f"expected exactly one winner, got {winners}"
    tmp.cleanup()
    print("ok: exactly one of 10 concurrent claimers won")


def test_expired_lease_is_reclaimable() -> None:
    s, tmp = _store()
    s.sync_intent(PROJECT, ["crashy"])
    assert s.claim(PROJECT, lease_seconds=-1) == "crashy"

    t = s.get(PROJECT, "crashy")
    assert t is not None and t.status == RUNNING
    assert t.lease_expired, "a lapsed lease must be visible as expired"

    assert [x.name for x in s.expired(PROJECT)] == ["crashy"]

    s.reclaim(PROJECT, "crashy", "port unreachable and tmux gone")
    t = s.get(PROJECT, "crashy")
    assert t is not None and t.status == TODO
    assert s.claim(PROJECT) == "crashy", "a reclaimed task must be claimable again"
    s.close(); tmp.cleanup()
    print("ok: expired lease is reclaimable, healthy claim is not")


def test_live_lease_is_not_reclaimable() -> None:
    s, tmp = _store()
    s.sync_intent(PROJECT, ["healthy"])
    s.claim(PROJECT, lease_seconds=600)
    assert s.expired(PROJECT) == [], "a live lease must never look expired"
    s.close(); tmp.cleanup()
    print("ok: live lease is not reclaimable")


def test_renew_extends() -> None:
    s, tmp = _store()
    s.sync_intent(PROJECT, ["long"])
    s.claim(PROJECT, lease_seconds=-1)
    assert len(s.expired(PROJECT)) == 1, "a negative lease must be expired at once"
    s.renew(PROJECT, "long", lease_seconds=600)
    assert s.expired(PROJECT) == [], "renewal must rescue a lapsed task"
    s.close(); tmp.cleanup()
    print("ok: renew extends a lease")


def test_retry_cap_bookkeeping() -> None:
    s, tmp = _store()
    s.sync_intent(PROJECT, ["failing"])
    s.claim(PROJECT)
    counts = [s.bump_retry(PROJECT, "failing") for _ in range(3)]
    assert counts == [1, 2, 3]
    s.finish(PROJECT, "failing", STUCK, "gate failed 3x")
    t = s.get(PROJECT, "failing")
    assert t is not None and t.status == STUCK
    s.reset_retry(PROJECT, "failing")
    assert s.get(PROJECT, "failing").retries == 0  # type: ignore[union-attr]
    s.close(); tmp.cleanup()
    print("ok: retry counter survives and resets")


def test_fingerprint_persists() -> None:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "orch.db"
    a = Store(path)
    a.sync_intent(PROJECT, ["fp"])
    a.set_fingerprint(PROJECT, "fp", "hash-v1")
    a.close()

    b = Store(path)
    assert b.fingerprint(PROJECT, "fp") == "hash-v1"
    b.close(); tmp.cleanup()
    print("ok: fingerprint survives a restart")


def test_alerts_dedupe_by_request_id() -> None:
    s, tmp = _store()
    assert s.note_alert("per_aaa", "ses_1", "/wt", "external_directory", "/tmp/*")
    assert not s.note_alert("per_aaa", "ses_1", "/wt", "external_directory", "/tmp/*"), \
        "the same request id must not alert twice"
    assert s.note_alert("per_bbb", "ses_1", "/wt", "external_directory", "/tmp/*"), \
        "a new id in the same session is a new alert"
    assert len(s.unresolved_alerts()) == 2
    s.resolve_alert("per_aaa")
    assert [r["request_id"] for r in s.unresolved_alerts()] == ["per_bbb"]
    s.close(); tmp.cleanup()
    print("ok: alerts dedupe on request id and resolve individually")


def test_verdicts_are_appended_not_overwritten() -> None:
    s, tmp = _store()
    s.sync_intent("/p", ["a"])
    s.record_verdict("/p", "a", "session-idle", "failed", "red", source="router")
    s.record_verdict("/p", "a", "session-idle", "passed", "green", source="daemon")
    rows = s.verdicts("/p", "a")
    assert [r["outcome"] for r in rows] == ["passed", "failed"]
    assert s.last_verdict("/p", "a")["source"] == "daemon"
    s.close(); tmp.cleanup()
    print("ok: verdicts accumulate as evidence rather than replacing each other")


def test_verdicts_are_filterable_by_who_ran_the_gate() -> None:
    s, tmp = _store()
    s.sync_intent("/p", ["a"])
    s.record_verdict("/p", "a", "session-idle", "passed", "", source="router")
    assert s.last_verdict("/p", "a", source="daemon") is None
    assert s.last_verdict("/p", "a", source="router")["outcome"] == "passed"
    s.close(); tmp.cleanup()
    print("ok: a router verdict is never mistaken for a daemon one")


def test_harness_versions_accumulate_and_a_change_is_reported() -> None:
    s, tmp = _store()
    assert s.note_harness((1, 18, 11), below_min=False) is None
    assert s.note_harness((1, 18, 11), below_min=False) is None, "no change, no noise"
    assert s.note_harness((1, 18, 13), below_min=False) == "1.18.11"
    assert [r["version"] for r in s.harness_history()] == ["1.18.11", "1.18.13"]
    assert s.get_meta("harness_min") == "1.18.11"
    s.close(); tmp.cleanup()
    print("ok: harness versions are recorded and a change is reported once")


def test_a_harness_below_the_floor_is_recorded_as_such() -> None:
    s, tmp = _store()
    s.note_harness((1, 18, 10), below_min=True)
    assert s.harness_history()[0]["below_min"] == 1
    s.close(); tmp.cleanup()
    print("ok: running below the supported floor is recorded, not just warned about")


def test_task_is_addressable_by_worktree() -> None:
    s, tmp = _store()
    s.sync_intent("/p", ["a"])
    s.claim("/p", "a")
    s.record_launch("/p", "a", worktree="/tmp/wt/a")
    assert s.by_worktree("/tmp/wt/a").name == "a"
    assert s.by_worktree("/tmp/wt/a/").name == "a", "a trailing slash must still match"
    assert s.by_worktree("/tmp/wt/nope") is None
    s.close(); tmp.cleanup()
    print("ok: tasks can be found by worktree, trailing slash included")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests)} tests, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
