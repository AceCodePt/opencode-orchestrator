from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import daemon, observe  # noqa: E402
from orch.store import RUNNING, Store, Task  # noqa: E402

NOW = 1_700_000_000
WT = "/tmp/lab/task-a"


def _task(**kw) -> Task:
    base = dict(id=1, project="/tmp/lab/lab.git", name="task-a", status=RUNNING,
                lease_until=NOW + 300, worktree=WT, port=4096, pid=1234, retries=0)
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


def _store() -> Store:
    return Store(":memory:")


def _gated_repo(gate_body: str | None) -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for cmd in (["init", "-q", "-b", "main", str(root)],):
        subprocess.run(["git", *cmd], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    if gate_body is not None:
        h = root / ".orchestration" / "hooks"
        h.mkdir(parents=True)
        (h / "session-idle").write_text(gate_body)
        (h / "session-idle").chmod(0o755)
    (root / "README").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True)
    return root, tmp


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
    acts = daemon.decide(_snap(server_running=False))
    assert daemon.RESTART_SERVER in _kinds(acts)
    review = [a for a in acts if a.kind == daemon.NEEDS_REVIEW]
    assert len(review) == 1 and review[0].task == "task-a"
    assert daemon.RECLAIM not in _kinds(acts)
    print("ok: a dead shared server escalates every running task")


def test_unresponsive_server_counts_as_dead() -> None:
    acts = daemon.decide(_snap(server_unresponsive=True))
    assert daemon.RESTART_SERVER in _kinds(acts)
    print("ok: an unresponsive server is treated as dead")


def test_busy_session_is_renewed_not_touched() -> None:
    acts = daemon.decide(_snap(facts={WT: _fact(observe.BUSY)}))
    assert _kinds(acts) == [daemon.RENEW]
    print("ok: a busy session is renewed")


def test_blocked_agent_is_alerted_and_kept_alive() -> None:
    b = observe.Blocked(kind="external_directory", request_id="per_1",
                        session_id="ses_1", detail="/etc/*")
    acts = daemon.decide(_snap(facts={WT: _fact(observe.BLOCKED_PERMISSION, [b])}))
    assert daemon.ALERT in _kinds(acts)
    assert daemon.RENEW in _kinds(acts)
    assert daemon.RECLAIM not in _kinds(acts)
    print("ok: a blocked agent is alerted and its lease renewed")


def test_session_outliving_its_dispatch_is_not_dead() -> None:
    acts = daemon.decide(_snap(dispatch_alive={1234: False},
                               facts={WT: _fact(observe.BUSY)}))
    assert _kinds(acts) == [daemon.RENEW]
    assert daemon.RECLAIM not in _kinds(acts)
    assert daemon.NEEDS_REVIEW not in _kinds(acts)
    print("ok: a session still working after its dispatch exited is left alone")


def _stopped(outcome: str | None, quiet: int = 0) -> list[daemon.Action]:
    return daemon.decide(_snap(
        dispatch_alive={1234: False},
        facts={WT: _fact(observe.IDLE)},
        router_outcome={WT: outcome},
        quiet_for={WT: quiet},
    ))


def test_a_finished_agent_is_verified_not_believed() -> None:
    acts = _stopped("passed")
    assert _kinds(acts) == [daemon.VERIFY]
    assert acts[0].reason == daemon.FINISHED_COMPLETE
    print("ok: a finished agent is verified rather than assumed either way")


def test_stopped_is_not_finished_while_the_gate_loop_is_still_driving() -> None:
    acts = _stopped("failed")
    assert _kinds(acts) == [daemon.RENEW]
    assert daemon.VERIFY not in _kinds(acts)
    print("ok: idle mid-retry is not treated as finished")


def test_a_stalled_retry_loop_is_eventually_escalated() -> None:
    acts = _stopped("failed", quiet=daemon.BUDGET_IDLE_S + 1)
    assert daemon.NEEDS_REVIEW in _kinds(acts)
    assert "mid-retry" in [a.reason for a in acts if a.kind == daemon.NEEDS_REVIEW][0]
    print("ok: a retry loop that stopped driving is escalated, not renewed forever")


def test_spending_the_retry_cap_counts_as_finished() -> None:
    acts = _stopped("stuck")
    assert _kinds(acts) == [daemon.VERIFY]
    assert acts[0].reason == daemon.FINISHED_GAVE_UP
    print("ok: a spent retry cap ends the loop, so the task is finished")


def test_a_running_gate_is_not_an_unexamined_agent() -> None:
    acts = _stopped("running", quiet=200)
    assert _kinds(acts) == [daemon.RENEW]
    assert acts[0].reason == daemon.GATE_IN_FLIGHT
    assert daemon.NEEDS_REVIEW not in _kinds(acts)
    print("ok: a gate that is still running is not mistaken for an unchecked agent")


def test_a_gate_running_past_its_own_timeout_is_escalated() -> None:
    acts = _stopped("running", quiet=daemon.BUDGET_GATE_S + 1)
    assert daemon.NEEDS_REVIEW in _kinds(acts)
    assert daemon.BUDGET_GATE_S * 1.0 > 900, "must exceed the gate's own timeout"
    print("ok: a gate running past its own timeout is escalated")


def test_never_gated_waits_out_a_grace_period_first() -> None:
    early = _stopped(None, quiet=1)
    assert _kinds(early) == [daemon.RENEW]
    assert daemon.NEEDS_REVIEW not in _kinds(early)
    print("ok: a missing verdict is given a grace period before it is believed")


def test_an_agent_that_was_never_gated_is_not_verified() -> None:
    acts = _stopped(None, quiet=daemon.GRACE_UNGATED_S + 1)
    assert _kinds(acts) == [daemon.NEEDS_REVIEW]
    assert acts[0].reason == daemon.NEVER_GATED
    assert daemon.VERIFY not in _kinds(acts)
    print("ok: an agent no gate ever examined is reviewed, not verified")


def test_no_gate_configured_is_not_finished_either() -> None:
    acts = _stopped("no-gate-configured", quiet=daemon.GRACE_UNGATED_S + 1)
    assert _kinds(acts) == [daemon.NEEDS_REVIEW]
    print("ok: an ungated project cannot produce a finished task")


def test_stop_reasons_are_exhaustive() -> None:
    for outcome in ("running", "passed", "failed", "stuck", "gate-modified",
                    "no-gate-configured", None, "something-new"):
        assert daemon.stop_reason(outcome) in (
            daemon.FINISHED_COMPLETE, daemon.FINISHED_GAVE_UP,
            daemon.STILL_RETRYING, daemon.GATE_IN_FLIGHT, daemon.NEVER_GATED)
    assert daemon.stop_reason("something-new") == daemon.NEVER_GATED
    print("ok: unknown router outcomes classify as not-finished")


def test_dry_run_never_runs_a_gate() -> None:
    sup = daemon.Supervisor("/tmp/lab/lab.git", store=_store(), dry_run=True)
    ran: list[str] = []
    sup.verify = lambda *a, **k: ran.append("ran") or []  # type: ignore[assignment]
    sup.apply([daemon.Action(daemon.VERIFY, "task-a", "stopped")])
    assert ran == [], "dry-run executed the gate"
    sup.close()
    print("ok: dry-run decides without running a gate")


def test_reclaim_requires_all_three_proofs() -> None:
    acts = daemon.decide(_snap(
        tasks=[_task(lease_until=NOW - 1)],
        dispatch_alive={1234: False},
        facts={},
    ))
    assert _kinds(acts) == [daemon.RECLAIM]
    print("ok: reclaim needs expired lease + gone dispatch + no session")


def test_expired_lease_alone_does_not_reclaim() -> None:
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
    for snap in (
        _snap(server_running=False),
        _snap(facts={WT: _fact(observe.BUSY)}),
        _snap(dispatch_alive={1234: False}, facts={WT: _fact(observe.IDLE)}),
        _snap(tasks=[_task(lease_until=NOW - 1)], dispatch_alive={1234: False}),
    ):
        for a in daemon.decide(snap):
            assert a.reason, f"{a.kind} carried no reason"
    print("ok: every action carries a reason")


def _running_task(st: Store, project: str, wt: Path) -> None:
    st.sync_intent(project, ["task-a"])
    assert st.claim(project, "task-a") == "task-a"
    st.record_launch(project, "task-a", worktree=str(wt), pid=1234)


def test_verify_promotes_only_on_a_pass() -> None:
    root, tmp = _gated_repo("#!/usr/bin/env bash\nexit 0\n")
    project = str(root / "lab.git")
    st = _store()
    _running_task(st, project, root)
    sup = daemon.Supervisor(project, store=st)

    sup.verify("task-a")
    t = st.get(project, "task-a")
    assert t is not None and t.status == "done", t
    v = st.last_verdict(project, "task-a")
    assert v is not None and v["source"] == "daemon" and v["outcome"] == "passed"
    sup.close()
    tmp.cleanup()
    print("ok: a passing committed gate promotes the task to done")


def test_a_pass_after_a_spent_cap_is_not_done() -> None:
    root, tmp = _gated_repo("#!/usr/bin/env bash\nexit 0\n")
    project = str(root / "lab.git")
    st = _store()
    _running_task(st, project, root)
    sup = daemon.Supervisor(project, store=st)

    out = "\n".join(sup.verify("task-a", why=daemon.FINISHED_GAVE_UP))
    t = st.get(project, "task-a")
    assert t is not None and t.status == "needs-review", t
    assert "flakiness" in out
    v = st.last_verdict(project, "task-a", source="daemon")
    assert v is not None and v["outcome"] == "passed", "the evidence should still exist"
    sup.close()
    tmp.cleanup()
    print("ok: a pass after the cap was spent is recorded but not called done")


def test_verify_sends_a_failing_gate_to_review() -> None:
    root, tmp = _gated_repo("#!/usr/bin/env bash\necho 'tests are red'\nexit 1\n")
    project = str(root / "lab.git")
    st = _store()
    _running_task(st, project, root)
    sup = daemon.Supervisor(project, store=st)

    out = "\n".join(sup.verify("task-a"))
    t = st.get(project, "task-a")
    assert t is not None and t.status == "needs-review", t
    assert "tests are red" in out or "gate said no" in out
    sup.close()
    tmp.cleanup()
    print("ok: a failing gate lands on needs-review, never done")


def test_verify_refuses_done_when_no_gate_is_committed() -> None:
    root, tmp = _gated_repo(None)
    project = str(root / "lab.git")
    st = _store()
    _running_task(st, project, root)
    sup = daemon.Supervisor(project, store=st)

    out = "\n".join(sup.verify("task-a"))
    t = st.get(project, "task-a")
    assert t is not None and t.status == "needs-review", t
    assert "nothing verified" in out
    v = st.last_verdict(project, "task-a")
    assert v is not None and v["outcome"] == "no-gate-configured"
    sup.close()
    tmp.cleanup()
    print("ok: no committed gate cannot produce done")


def test_verify_ignores_a_router_self_report() -> None:
    root, tmp = _gated_repo("#!/usr/bin/env bash\nexit 1\n")
    project = str(root / "lab.git")
    st = _store()
    _running_task(st, project, root)
    st.record_verdict(project, "task-a", "session-idle", "passed",
                      "all good, honestly", source="router")
    sup = daemon.Supervisor(project, store=st)

    sup.verify("task-a")
    t = st.get(project, "task-a")
    assert t is not None and t.status == "needs-review", (
        "a router self-report was allowed to decide the outcome")
    sup.close()
    tmp.cleanup()
    print("ok: a router self-report cannot override the daemon's own gate run")


def test_verify_reviews_a_vanished_worktree() -> None:
    st = _store()
    project = "/tmp/lab/lab.git"
    _running_task(st, project, Path("/tmp/lab/definitely-not-here"))
    sup = daemon.Supervisor(project, store=st)
    out = "\n".join(sup.verify("task-a"))
    t = st.get(project, "task-a")
    assert t is not None and t.status == "needs-review"
    assert "no longer exists" in out
    sup.close()
    print("ok: a missing worktree is reviewed, not passed")


def test_actions_carry_their_project() -> None:
    a = _task(project="/a/one.git", worktree="/wt/a", pid=1)
    b = _task(project="/b/two.git", worktree="/wt/b", pid=2)
    acts = daemon.decide(_snap(
        tasks=[a, b],
        dispatch_alive={1: True, 2: True},
        facts={"/wt/a": _fact(observe.BUSY), "/wt/b": _fact(observe.BUSY)},
    ))
    assert len(acts) == 2
    assert {x.project for x in acts} == {"/a/one.git", "/b/two.git"}
    print("ok: every per-task action names its project")


def test_same_name_in_two_projects_is_not_confused() -> None:
    st = _store()
    for p in ("/a/one.git", "/b/two.git"):
        st.sync_intent(p, ["task-a"])
        st.claim(p, "task-a")
    sup = daemon.Supervisor(store=st)
    sup.apply([daemon.Action(daemon.RECLAIM, "task-a", "dead", project="/a/one.git")])
    assert st.get("/a/one.git", "task-a").status == "todo"
    assert st.get("/b/two.git", "task-a").status == "running", (
        "reclaiming one project's task disturbed another's")
    sup.close()
    print("ok: identically named tasks in two projects stay independent")


def test_systemd_unit_survives_logout_and_keeps_children() -> None:
    text = daemon.unit_text(interval=10)
    assert "Restart=always" in text
    assert "KillMode=process" in text
    assert "WantedBy=default.target" in text
    assert "daemon run --interval 10" in text
    assert Path(text.split("ExecStart=")[1].split()[0]).name == "orch"
    print("ok: the unit restarts always and does not kill the server it supervises")


def main() -> int:
    assert time.time() > 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} daemon tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
