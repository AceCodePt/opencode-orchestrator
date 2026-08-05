"""Tests for supervision policy.

`decide()` is pure, which is the only reason these cases are testable at all: an
agent that has been quiet for 21 minutes, a server that died mid-task, or a session
that outlived its dispatch process cannot be produced on demand against live agents.

The most important test here is `test_session_outliving_its_dispatch_is_not_dead`.
That case was measured, not imagined, and getting it wrong destroys live work.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import daemon, observe  # noqa: E402
from orch.store import RUNNING, Task  # noqa: E402

NOW = 1_700_000_000
WT = "/tmp/lab/task-a"


def _task(**kw) -> Task:
    base = dict(id=1, project="/tmp/lab/lab.git", name="task-a", status=RUNNING,
                lease_until=NOW + 300, worktree=WT, port=4096, pid=1234, retries=0)
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


def _fact(state: str, blocked: list | None = None) -> observe.TaskFact:
    return observe.TaskFact(worktree=WT, session_ids=["ses_1"], state=state,
                            blocked=blocked or [])


def _snap(**kw) -> daemon.Snapshot:
    base = dict(now=NOW, server_running=True, server_unresponsive=False,
                tasks=[_task()], facts={}, dispatch_alive={1234: True}, quiet_for={})
    base.update(kw)
    return daemon.Snapshot(**base)  # type: ignore[arg-type]


def _kinds(actions: list[daemon.Action]) -> list[str]:
    return [a.kind for a in actions]


def test_dead_server_escalates_every_running_task() -> None:
    """One shared server is a single failure domain, so its death is everyone's."""
    acts = daemon.decide(_snap(server_running=False))
    assert daemon.RESTART_SERVER in _kinds(acts)
    review = [a for a in acts if a.kind == daemon.NEEDS_REVIEW]
    assert len(review) == 1 and review[0].task == "task-a"
    # Not reclaimed: the worktree may hold partial work, so re-dispatch is a
    # decision rather than a default.
    assert daemon.RECLAIM not in _kinds(acts)
    print("ok: a dead shared server escalates every running task")


def test_unresponsive_server_counts_as_dead() -> None:
    """A live pid that answers nothing is the state a `kill -0` check calls healthy,
    and every task on the machine is inside it."""
    acts = daemon.decide(_snap(server_unresponsive=True))
    assert daemon.RESTART_SERVER in _kinds(acts)
    print("ok: an unresponsive server is treated as dead")


def test_busy_session_is_renewed_not_touched() -> None:
    acts = daemon.decide(_snap(facts={WT: _fact(observe.BUSY)}))
    assert _kinds(acts) == [daemon.RENEW]
    print("ok: a busy session is renewed")


def test_blocked_agent_is_alerted_and_kept_alive() -> None:
    """Waiting on a human is the opposite of dead. Letting the lease lapse here
    invites a reclaim of work that is merely paused."""
    b = observe.Blocked(kind="external_directory", request_id="per_1",
                        session_id="ses_1", detail="/etc/*")
    acts = daemon.decide(_snap(facts={WT: _fact(observe.BLOCKED_PERMISSION, [b])}))
    assert daemon.ALERT in _kinds(acts)
    assert daemon.RENEW in _kinds(acts)
    assert daemon.RECLAIM not in _kinds(acts)
    print("ok: a blocked agent is alerted and its lease renewed")


def test_session_outliving_its_dispatch_is_not_dead() -> None:
    """The measured case, and the expensive one to get wrong.

    A dispatch exited 0 while its session went on to perform 24 more tool calls over
    36 seconds, because the router injected a follow-up prompt. Anything that can
    inject a prompt outlives the dispatcher, so a gone pid is not evidence of
    completion - and reclaiming on it would kill live work.
    """
    acts = daemon.decide(_snap(dispatch_alive={1234: False},
                               facts={WT: _fact(observe.BUSY)}))
    assert _kinds(acts) == [daemon.RENEW]
    assert daemon.RECLAIM not in _kinds(acts)
    assert daemon.NEEDS_REVIEW not in _kinds(acts)
    print("ok: a session still working after its dispatch exited is left alone")


def test_stopped_agent_is_reviewed_never_marked_done() -> None:
    """Nothing here can know the gate passed. The verdict lives in the router, which
    currently reports to the server log rather than to a daemon endpoint."""
    acts = daemon.decide(_snap(dispatch_alive={1234: False},
                               facts={WT: _fact(observe.IDLE)}))
    assert _kinds(acts) == [daemon.NEEDS_REVIEW]
    assert "gate verdict" in acts[0].reason
    print("ok: a stopped agent is marked needs-review, never done")


def test_reclaim_requires_all_three_proofs() -> None:
    acts = daemon.decide(_snap(
        tasks=[_task(lease_until=NOW - 1)],
        dispatch_alive={1234: False},
        facts={},                      # no session for this worktree
    ))
    assert _kinds(acts) == [daemon.RECLAIM]
    print("ok: reclaim needs expired lease + gone dispatch + no session")


def test_expired_lease_alone_does_not_reclaim() -> None:
    """A busy agent whose renewal broke looks identical to a dead one by lease
    alone, which is why expiry is necessary but never sufficient."""
    acts = daemon.decide(_snap(
        tasks=[_task(lease_until=NOW - 1)],
        dispatch_alive={1234: False},
        facts={WT: _fact(observe.BUSY)},
    ))
    assert daemon.RECLAIM not in _kinds(acts)
    print("ok: an expired lease alone is not proof of death")


def test_valid_lease_with_no_session_waits() -> None:
    acts = daemon.decide(_snap(dispatch_alive={1234: False}, facts={}))
    assert _kinds(acts) == [daemon.NOTE]
    assert "not proof of death" in acts[0].detail
    print("ok: a valid lease with no session waits instead of reclaiming")


def test_non_running_tasks_are_ignored() -> None:
    acts = daemon.decide(_snap(tasks=[_task(status="done"), _task(status="todo")]))
    assert acts == []
    print("ok: only running tasks are supervised")


def test_silence_budgets_differ_by_state() -> None:
    """A blocked agent gets a shorter fuse than one legitimately grinding: it is not
    quiet, it is waiting, and nothing will change until a human answers."""
    quiet_busy = daemon.decide(_snap(facts={WT: _fact(observe.BUSY)},
                                     quiet_for={WT: daemon.BUDGET_BUSY_S - 1}))
    assert daemon.NOTE not in _kinds(quiet_busy)

    loud_busy = daemon.decide(_snap(facts={WT: _fact(observe.BUSY)},
                                    quiet_for={WT: daemon.BUDGET_BUSY_S + 1}))
    assert daemon.NOTE in _kinds(loud_busy)

    b = observe.Blocked("external_directory", "per_1", "ses_1", "/etc/*")
    blocked = daemon.decide(_snap(
        facts={WT: _fact(observe.BLOCKED_PERMISSION, [b])},
        quiet_for={WT: daemon.BUDGET_BLOCKED_S + 1}))
    assert daemon.NOTE in _kinds(blocked)
    assert daemon.BUDGET_BLOCKED_S < daemon.BUDGET_BUSY_S
    print("ok: silence budgets differ by state")


def test_every_action_carries_a_reason() -> None:
    """An action with no reason is unreviewable after the fact, and this component
    is the one that kills things."""
    for snap in (
        _snap(server_running=False),
        _snap(facts={WT: _fact(observe.BUSY)}),
        _snap(dispatch_alive={1234: False}, facts={WT: _fact(observe.IDLE)}),
        _snap(tasks=[_task(lease_until=NOW - 1)], dispatch_alive={1234: False}),
    ):
        for a in daemon.decide(snap):
            assert a.reason, f"{a.kind} carried no reason"
    print("ok: every action carries a reason")


def main() -> int:
    assert time.time() > 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} daemon tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
