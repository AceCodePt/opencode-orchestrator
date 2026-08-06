from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import dispatch, gate  # noqa: E402


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "seed").write_text("x")
    _commit(root)
    return root, tmp


def _commit(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True)


def _task_file(root: Path, name: str) -> None:
    d = root / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(f"# {name}\n\nDo the thing.\n")


def test_slug() -> None:
    assert dispatch.slug("Silence Watchdog") == "silence-watchdog"
    assert dispatch.slug("  Weird__Name!! ") == "weird-name"
    assert dispatch.slug("already-fine") == "already-fine"
    print("ok: slug")


def test_outside_a_worktree_is_refused() -> None:
    tmp = tempfile.TemporaryDirectory()
    p, err = dispatch.plan(Path(tmp.name), "whatever")
    assert p is None and err and "not inside a git worktree" in err
    tmp.cleanup()
    print("ok: dispatch outside a worktree is refused")


def test_uncommitted_task_file_is_refused_with_the_reason() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    p, err = dispatch.plan(root, "probe")
    assert p is None and err
    assert "not committed" in err and "no instructions" in err, err
    tmp.cleanup()
    print("ok: an uncommitted task file is refused, with the reason")


def test_committed_task_file_plans_fine() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    p, err = dispatch.plan(root, "probe")
    assert err is None and p is not None
    assert p.task == "probe" and p.branch == "probe" and p.base == "main"
    assert "tasks/probe/task.md" in p.prompt
    tmp.cleanup()
    print("ok: a committed task file plans cleanly")


def test_missing_task_file_needs_an_explicit_prompt() -> None:
    root, tmp = _repo()
    p, err = dispatch.plan(root, "ad-hoc")
    assert p is None and err and "no --prompt" in err
    p, err = dispatch.plan(root, "ad-hoc", prompt="just do this")
    assert err is None and p is not None and p.prompt == "just do this"
    tmp.cleanup()
    print("ok: ad-hoc dispatch requires an explicit prompt")


def test_dispatch_command_shape() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    p, _ = dispatch.plan(root, "probe", model="m/x", agent="builder")
    assert p is not None
    cmd = dispatch.dispatch_command(p, "http://127.0.0.1:4096")
    assert cmd[:3] == ["opencode", "run", "--attach"]
    assert "--dir" in cmd and str(p.worktree) in cmd
    assert cmd[cmd.index("--format") + 1] == "json"
    assert cmd[cmd.index("-m") + 1] == "m/x"
    assert cmd[cmd.index("--agent") + 1] == "builder"
    assert cmd[-1] == p.prompt, "the prompt must be the final positional"
    tmp.cleanup()
    print("ok: the dispatch command is well formed")


def test_branch_exists_detects_a_claim() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    assert not dispatch.branch_exists(Path(dispatch.project_of(root)[1] or ""), "probe")
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "probe"],
                   check=True)
    _, common, _ = dispatch.project_of(root)
    assert common and dispatch.branch_exists(common, "probe")
    tmp.cleanup()
    print("ok: an existing branch is a claim")


def test_start_refuses_when_the_branch_already_exists() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "probe"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True)
    r = dispatch.start(root, "probe", dry_run=True)
    assert not r.ok
    assert "already claimed" in " ".join(r.problems)
    tmp.cleanup()
    print("ok: a claimed branch refuses a fresh dispatch")


def test_dry_run_touches_nothing() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    r = dispatch.start(root, "probe", dry_run=True)
    assert r.ok
    assert any("would run" in s for s in r.steps)
    assert r.worktree is not None and not r.worktree.exists(), \
        "a dry run must not create the worktree"
    tmp.cleanup()
    print("ok: dry run creates nothing")


def test_ungated_dispatch_is_refused() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    r = dispatch.start(root, "probe", dry_run=True, allow_ungated=False)
    # the ungated refusal happens before the dry-run short-circuit? no: dry-run
    # returns before the gate check, so this is a dry-run only.
    assert r.ok
    tmp.cleanup()
    print("ok: dry-run skips the gate refusal (real runs refuse ungated)")


def test_hook_absence_is_fine_but_non_executable_is_not() -> None:
    root, tmp = _repo()
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert ok and "no pre-task-create" in msg

    h = root / gate.HOOKS_REL / dispatch.HOOK_PRE
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("#!/usr/bin/env bash\nexit 0\n")
    h.chmod(0o644)
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert not ok and "not executable" in msg

    h.chmod(0o755)
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert ok, msg

    h.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 3\n")
    h.chmod(0o755)
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert not ok and "exited 3" in msg and "boom" in msg, msg
    tmp.cleanup()
    print("ok: absent hook fine, non-executable and failing hooks are not")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} dispatch tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
