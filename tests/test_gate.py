"""Tests for gate trustworthiness.

Every case here corresponds to a way the gate was observed to fail *silently* -
reporting configured-and-fine while nothing verified anything. That is the only
failure direction that matters: a gate that wrongly fails gets noticed within
minutes, and a gate that wrongly passes never does.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import gate  # noqa: E402

GATE_BODY = "#!/usr/bin/env bash\necho ok\n"
ROUTER_BODY = "export const X = 1;\n"


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root, tmp


def _commit(root: Path, msg: str = "c") -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg], check=True)


def _scaffold(root: Path, gate_body: str = GATE_BODY, router: bool = True) -> None:
    (root / gate.HOOKS_REL).mkdir(parents=True, exist_ok=True)
    h = root / gate.HOOKS_REL / "session-idle"
    h.write_text(gate_body)
    h.chmod(0o755)
    (root / gate.CONFIG_REL).write_text('{"baseBranch": "main"}\n')
    if router:
        p = root / gate.ROUTER_RELS[0]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ROUTER_BODY)


def _by_name(findings: list[gate.GateFinding], name: str) -> gate.GateFinding | None:
    for f in findings:
        if f.name == name:
            return f
    return None


def test_committed_and_clean_is_ok() -> None:
    root, tmp = _repo()
    _scaffold(root)
    _commit(root)
    findings = gate.inspect(root)
    f = _by_name(findings, "session-idle")
    assert f and f.state == gate.OK, findings
    assert not gate.blocking(findings), "a committed, unmodified gate must not block"
    tmp.cleanup()
    print("ok: committed and clean gate is trustworthy")


def test_uncommitted_gate_blocks() -> None:
    root, tmp = _repo()
    (root / "x").write_text("x")
    _commit(root)          # a commit exists, but the gate is not in it
    _scaffold(root)
    findings = gate.inspect(root)
    f = _by_name(findings, "session-idle")
    assert f and f.state == gate.UNCOMMITTED, findings
    assert not f.trustworthy
    assert gate.blocking(findings), "an uncommitted gate must stop a dispatch"
    tmp.cleanup()
    print("ok: uncommitted gate blocks")


def test_agent_rewriting_its_own_gate_is_flagged_but_still_trusted() -> None:
    """The case that motivated all of this.

    An agent rewrote its gate and was reported as passed. Divergence must be
    surfaced, but the verdict stays valid because the *committed* copy is what runs -
    treating it as a failure would train people to ignore the flag.
    """
    root, tmp = _repo()
    _scaffold(root)
    _commit(root)
    (root / gate.HOOKS_REL / "session-idle").write_text("#!/usr/bin/env bash\nexit 0\n")
    findings = gate.inspect(root)
    f = _by_name(findings, "session-idle")
    assert f and f.state == gate.DIVERGED, findings
    assert f.trustworthy, "the committed gate ran, so its verdict still counts"
    assert not gate.blocking(findings)
    tmp.cleanup()
    print("ok: a rewritten gate is flagged, and the committed verdict still stands")


def test_config_divergence_is_flagged() -> None:
    """`exclude: ['**']` switches the gate off without touching a hook."""
    root, tmp = _repo()
    _scaffold(root)
    _commit(root)
    (root / gate.CONFIG_REL).write_text(
        '{"baseBranch": "main", "gate": {"exclude": ["**"]}}\n'
    )
    findings = gate.inspect(root)
    f = _by_name(findings, "config.json")
    assert f and f.state == gate.DIVERGED, findings
    tmp.cleanup()
    print("ok: config divergence is flagged")


def test_missing_router_on_disk_is_reported() -> None:
    """The worst case: a valid committed gate that cannot possibly fire.

    Observed live - the agent worked, the gate was correct, and no gate ran, because
    the worktree predated the plugin commit.
    """
    root, tmp = _repo()
    _scaffold(root, router=False)
    _commit(root)
    findings = gate.inspect(root)
    f = _by_name(findings, "router plugin")
    assert f and f.state == gate.MISSING, findings
    assert gate.blocking(findings), "no router means nothing verifies anything"
    tmp.cleanup()
    print("ok: a missing router is reported rather than read as success")


def test_router_on_disk_but_uncommitted_is_reported() -> None:
    root, tmp = _repo()
    _scaffold(root, router=False)
    _commit(root)
    p = root / gate.ROUTER_RELS[0]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ROUTER_BODY)
    findings = gate.inspect(root)
    f = _by_name(findings, "router plugin")
    assert f and f.state == gate.UNCOMMITTED, findings
    tmp.cleanup()
    print("ok: a router that works here but not downstream is reported")


def test_base_branch_comes_from_config() -> None:
    root, tmp = _repo()
    (root / gate.ORCH_DIR).mkdir(parents=True, exist_ok=True)
    (root / gate.CONFIG_REL).write_text('{"baseBranch": "preview"}\n')
    assert gate.base_branch(root) == "preview"
    (root / gate.CONFIG_REL).write_text("not json at all")
    assert gate.base_branch(root) == "main", "a broken config must not crash the check"
    tmp.cleanup()
    print("ok: baseBranch read from config, with a safe fallback")


def test_absent_gate_yields_no_finding_not_a_pass() -> None:
    """A project with nothing configured produces no findings at all.

    The dispatcher treats that as a refusal. Encoding "no gate" as a passing finding
    here is exactly how absence would become a green light.
    """
    root, tmp = _repo()
    (root / "x").write_text("x")
    _commit(root)
    findings = gate.inspect(root)
    assert _by_name(findings, "session-idle") is None
    tmp.cleanup()
    print("ok: an absent gate yields no finding, never a pass")


# ------------------------------------------------------------------ running a gate
#
# `gate.run` is what lets the daemon reach a verdict of its own instead of being told
# one by a plugin living inside the agent's server. So the cases that matter are the
# ones where the working tree and the committed ref disagree.


def test_run_uses_the_committed_gate_not_the_working_copy() -> None:
    """The case that motivated the whole design, as an assertion.

    An agent was observed rewriting its own gate and then being reported as passed.
    Here the committed gate fails and the working copy would pass; the verdict must
    follow the commit.
    """
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\necho 'committed says no'\nexit 1\n")
    _commit(root)
    (root / gate.HOOKS_REL / "session-idle").write_text(
        "#!/usr/bin/env bash\nexit 0\n")

    v = gate.run(root)
    assert v.outcome == gate.FAILED, v
    assert not v.justifies_done
    assert v.diverged, "an edited gate must be reported even though the commit ran"
    assert "committed says no" in v.detail
    tmp.cleanup()
    print("ok: run judges by the committed gate, and flags that it was edited")


def test_run_passes_and_reports_divergence_separately() -> None:
    """Divergence does not veto a pass.

    The committed gate ran, so its verdict is sound. Conflating "was edited" with
    "cannot be trusted" would train people to ignore the flag.
    """
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\nexit 0\n")
    _commit(root)
    v = gate.run(root)
    assert v.outcome == gate.PASSED and v.justifies_done and not v.diverged
    tmp.cleanup()
    print("ok: a clean committed gate passes")


def test_run_without_a_committed_gate_is_not_a_pass() -> None:
    root, tmp = _repo()
    (root / "x").write_text("x")
    _commit(root)
    v = gate.run(root)
    assert v.outcome == gate.NO_GATE
    assert not v.justifies_done, "absence of a gate must never justify done"
    tmp.cleanup()
    print("ok: no committed gate is neither a pass nor an error")


def test_run_does_not_leave_anything_in_the_worktree() -> None:
    """The staged copy lives outside the worktree.

    Two concrete reasons: a copy inside changes the fingerprint the gate is meant to
    describe, and it appears in `git status` as if the agent had left it behind.
    """
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\nexit 0\n")
    _commit(root)
    gate.run(root)
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                         capture_output=True, text=True).stdout
    assert out.strip() == "", f"gate run dirtied the worktree: {out!r}"
    tmp.cleanup()
    print("ok: running a gate leaves the worktree clean")


def test_run_treats_a_hanging_gate_as_failure() -> None:
    """A gate that never finishes has not approved anything.

    Fail rather than inconclusive: treating a hang as "unknown, therefore fine" is how
    a wedged test suite would start passing work through.
    """
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\nsleep 30\n")
    _commit(root)
    v = gate.run(root, timeout=1)
    assert v.outcome == gate.FAILED and not v.justifies_done
    assert "timed out" in v.detail
    tmp.cleanup()
    print("ok: a hanging gate fails closed")


def test_run_reports_a_gate_that_cannot_execute() -> None:
    """A gate with no interpreter is a failure, not a pass."""
    root, tmp = _repo()
    _scaffold(root, "#!/nonexistent/interpreter\n")
    _commit(root)
    v = gate.run(root)
    assert v.outcome == gate.FAILED and not v.justifies_done
    tmp.cleanup()
    print("ok: an unexecutable gate fails closed")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} gate tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
