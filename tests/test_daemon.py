"""Tests for supervision policy.

`decide()` is pure, which is the only reason these cases are testable at all: an
agent that has been quiet for 21 minutes, a server that died mid-task, or a session
that outlived its dispatch process cannot be produced on demand against live agents.

The most important test here is `test_session_outliving_its_dispatch_is_not_dead`.
That case was measured, not imagined, and getting it wrong destroys live work.
"""

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
    """A worktree with a gate committed on main, as a dispatched task would have."""
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


def test_stopped_agent_is_verified_not_believed() -> None:
    """A stopped agent triggers a gate run, not a status guess.

    `decide` cannot mark anything done because it cannot run anything - which is the
    point of it being pure. It says "check this", and the executor checks.
    """
    acts = daemon.decide(_snap(dispatch_alive={1234: False},
                               facts={WT: _fact(observe.IDLE)}))
    assert _kinds(acts) == [daemon.VERIFY]
    assert daemon.NEEDS_REVIEW not in _kinds(acts), (
        "needs-review is now an outcome of verification, not a substitute for it")
    print("ok: a stopped agent is verified rather than assumed either way")


def test_dry_run_never_runs_a_gate() -> None:
    """`--dry-run` must not execute a test suite.

    Running the gate is the one action here with real cost and real side effects, so a
    mode advertised as changing nothing has to skip it.
    """
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


# ------------------------------------------------------------------- verification
#
# The executor half. These are the only tests here that touch the disk, because the
# behaviour under test is precisely "run something the agent did not run".


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
    """The failure this project exists to prevent, at the last possible moment.

    An unchecked branch must not receive the same status as a checked one just because
    nothing objected. Nothing objected because nothing ran.
    """
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
    """A `passed` written from inside the agent's server does not decide anything.

    This is the whole reason the daemon runs the gate itself. The router's report is
    stored - it is useful evidence - but the committed gate here fails, and the failure
    is what counts.
    """
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


# ------------------------------------------------------------- machine-wide scope
#
# One daemon covers every project, because there is one shared server, one database
# and one set of leases per machine. A per-project daemon has to be remembered once
# per project, and supervises nothing for any project nobody started.


def test_actions_carry_their_project() -> None:
    """Task names are unique only within a project.

    Without the project on the action, a machine-wide daemon eventually renews or
    reclaims a same-named task in the wrong repository.
    """
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
    sup = daemon.Supervisor(store=st)          # no project: machine-wide
    sup.apply([daemon.Action(daemon.RECLAIM, "task-a", "dead", project="/a/one.git")])
    assert st.get("/a/one.git", "task-a").status == "todo"
    assert st.get("/b/two.git", "task-a").status == "running", (
        "reclaiming one project's task disturbed another's")
    sup.close()
    print("ok: identically named tasks in two projects stay independent")


def test_systemd_unit_survives_logout_and_keeps_children() -> None:
    """Two properties that are silent when wrong.

    `Restart=always` because a supervisor that exited cleanly is still not
    supervising, and nothing else notices. `KillMode=process` because the shared
    server and the agents are deliberately started in their own sessions and must not
    be killed when the unit is restarted.
    """
    text = daemon.unit_text(interval=10)
    assert "Restart=always" in text
    assert "KillMode=process" in text
    assert "WantedBy=default.target" in text
    assert "daemon --interval 10" in text
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
