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


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} gate tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
