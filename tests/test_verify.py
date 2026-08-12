from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import verify  # noqa: E402

VERIFY_BODY = "#!/usr/bin/env bash\necho ok\n"


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


def _scaffold(root: Path, hook_body: str = VERIFY_BODY) -> None:
    (root / verify.HOOKS_REL).mkdir(parents=True, exist_ok=True)
    h = root / verify.HOOKS_REL / "session-idle"
    h.write_text(hook_body)
    h.chmod(0o755)
    (root / verify.CONFIG_REL).write_text('{"baseBranch": "main"}\n')


def _by_name(findings: list[verify.HookFinding], name: str) -> verify.HookFinding | None:
    for f in findings:
        if f.name == name:
            return f
    return None


def test_committed_and_clean_is_ok() -> None:
    root, tmp = _repo()
    _scaffold(root)
    _commit(root)
    findings = verify.inspect(root)
    f = _by_name(findings, "session-idle")
    assert f and f.state == verify.OK, findings
    assert not verify.blocking(findings), "a committed, unmodified verification hook must not block"
    tmp.cleanup()
    print("ok: committed and clean verification hook is trustworthy")


def test_uncommitted_hook_blocks() -> None:
    root, tmp = _repo()
    (root / "x").write_text("x")
    _commit(root)
    _scaffold(root)
    findings = verify.inspect(root)
    f = _by_name(findings, "session-idle")
    assert f and f.state == verify.UNCOMMITTED, findings
    assert not f.trustworthy
    assert verify.blocking(findings), "an uncommitted verification hook must stop a dispatch"
    tmp.cleanup()
    print("ok: uncommitted verification hook blocks")


def test_agent_rewriting_its_own_hook_is_flagged_but_still_trusted() -> None:
    root, tmp = _repo()
    _scaffold(root)
    _commit(root)
    (root / verify.HOOKS_REL / "session-idle").write_text("#!/usr/bin/env bash\nexit 0\n")
    findings = verify.inspect(root)
    f = _by_name(findings, "session-idle")
    assert f and f.state == verify.DIVERGED, findings
    assert f.trustworthy, "the committed hook ran, so its verdict still counts"
    assert not verify.blocking(findings)
    tmp.cleanup()
    print("ok: a rewritten verification hook is flagged, and the committed verdict still stands")


def test_config_divergence_is_flagged() -> None:
    root, tmp = _repo()
    _scaffold(root)
    _commit(root)
    (root / verify.CONFIG_REL).write_text(
        '{"baseBranch": "main", "verify": {"retryCap": 9}}\n'
    )
    findings = verify.inspect(root)
    f = _by_name(findings, "config.json")
    assert f and f.state == verify.DIVERGED, findings
    tmp.cleanup()
    print("ok: config divergence is flagged")


def test_base_branch_comes_from_config() -> None:
    root, tmp = _repo()
    (root / verify.ORCH_DIR).mkdir(parents=True, exist_ok=True)
    (root / verify.CONFIG_REL).write_text('{"baseBranch": "preview"}\n')
    assert verify.base_branch(root) == "preview"
    (root / verify.CONFIG_REL).write_text("not json at all")
    assert verify.base_branch(root) == "main", "a broken config must not crash the check"
    tmp.cleanup()
    print("ok: baseBranch read from config, with a safe fallback")


def test_absent_hook_yields_no_finding_not_a_pass() -> None:
    root, tmp = _repo()
    (root / "x").write_text("x")
    _commit(root)
    findings = verify.inspect(root)
    assert _by_name(findings, "session-idle") is None
    tmp.cleanup()
    print("ok: an absent verification hook yields no finding, never a pass")


def test_run_uses_the_committed_hook_not_the_working_copy() -> None:
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\necho 'committed says no'\nexit 1\n")
    _commit(root)
    (root / verify.HOOKS_REL / "session-idle").write_text(
        "#!/usr/bin/env bash\nexit 0\n")

    v = verify.run(root)
    assert v.outcome == verify.FAILED, v
    assert not v.justifies_done
    assert v.diverged, "an edited verification hook must be reported even though the commit ran"
    assert "committed says no" in v.detail
    tmp.cleanup()
    print("ok: run judges by the committed hook, and flags that it was edited")


def test_run_passes_and_reports_divergence_separately() -> None:
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\nexit 0\n")
    _commit(root)
    v = verify.run(root)
    assert v.outcome == verify.PASSED and v.justifies_done and not v.diverged
    tmp.cleanup()
    print("ok: a clean committed verification hook passes")


def test_run_without_a_committed_hook_is_not_a_pass() -> None:
    root, tmp = _repo()
    (root / "x").write_text("x")
    _commit(root)
    v = verify.run(root)
    assert v.outcome == verify.NO_VERIFY
    assert not v.justifies_done, "absence of a verification hook must never justify done"
    tmp.cleanup()
    print("ok: no committed verification hook is neither a pass nor an error")


def test_run_treats_a_hanging_hook_as_failure() -> None:
    root, tmp = _repo()
    _scaffold(root, "#!/usr/bin/env bash\nsleep 30\n")
    _commit(root)
    v = verify.run(root, timeout=1)
    assert v.outcome == verify.FAILED and not v.justifies_done
    assert "timed out" in v.detail
    tmp.cleanup()
    print("ok: a hanging verification hook fails closed")


def test_a_passing_hook_commits_the_work_and_is_idempotent() -> None:
    root, tmp = _repo()
    body = (
        "#!/usr/bin/env bash\n"
        "cd \"${WORKTREE_PATH:-$(git rev-parse --show-toplevel)}\" || exit 1\n"
        "if [ -n \"$(git status --porcelain)\" ]; then\n"
        "  git add -A\n"
        "  git -c user.name=orch -c user.email=orch@localhost commit -q -m g\n"
        "fi\n"
        "exit 0\n"
    )
    _scaffold(root, body)
    _commit(root)
    (root / "work.txt").write_text("done")
    v = verify.run(root)
    assert v.outcome == verify.PASSED, v
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                         capture_output=True, text=True).stdout
    assert out.strip() == "", f"a passing verification hook must commit the work: {out!r}"
    log = subprocess.run(["git", "-C", str(root), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "g" in log, "the verification hook's commit must exist"
    v2 = verify.run(root)
    assert v2.outcome == verify.PASSED, "re-running on a clean tree must still pass"
    print("ok: a passing verification hook commits the work, and re-running is idempotent")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} verification tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
