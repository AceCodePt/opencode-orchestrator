from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import merge, state  # noqa: E402


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def _bare_repo(tmp: tempfile.TemporaryDirectory) -> Path:
    seed = Path(tmp.name) / "seed"
    _git(Path(tmp.name), "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README").write_text("hello")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")

    bare = Path(tmp.name) / "lab.git"
    _git(Path(tmp.name), "clone", "--bare", str(seed), str(bare))

    main_wt = Path(tmp.name) / "main"
    _git(bare, "worktree", "add", str(main_wt), "main")
    _git(main_wt, "config", "user.email", "t@t")
    _git(main_wt, "config", "user.name", "t")
    (main_wt / ".orchestration" / "hooks").mkdir(parents=True)
    (main_wt / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "main"}\n')
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "orchestration setup")

    # declare the task on base so archive/scan can see it
    (main_wt / "tasks" / "task-a").mkdir(parents=True)
    (main_wt / "tasks" / "task-a" / "task.md").write_text("# Task A\n")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "declare task-a")
    return bare


def _claim_task(bare: Path, name: str, content: str = "# task-a") -> Path:
    wt = bare.parent / name
    _git(bare, "worktree", "add", "-b", name, str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    (wt / "task.py").write_text(content)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", f"work for {name}")
    return wt


def test_merge_done_task_folds_branch_archives_and_cleans_up() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")

    r = merge.merge_and_cleanup(bare, "task-a", "task-a", wt, "main")
    assert r.ok, r.problems
    assert any("merged" in s for s in r.steps)
    assert any("archived" in s for s in r.steps)

    main_wt = bare.parent / "main"
    log = _git(main_wt, "log", "--oneline")
    assert "work for task-a" in log

    assert not wt.exists(), "worktree should be gone"
    assert "task-a" not in _git(bare, "branch", "--list")
    assert (main_wt / "archive" / "task-a" / "task.md").exists(), \
        "the spec must be archived"
    assert not (main_wt / "tasks" / "task-a").exists(), \
        "the spec must leave the active backlog"
    tmp.cleanup()
    print("ok: done task merged, archived, worktree + branch cleaned up")


def test_merge_archives_a_reused_slug_to_a_timestamped_spot() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt1 = _claim_task(bare, "task-a")
    r1 = merge.merge_and_cleanup(bare, "task-a", "task-a", wt1, "main")
    assert r1.ok, r1.problems

    # Reuse the slug: an old generation sits at archive/task-a/, a fresh
    # declared task-a is created, claimed, and merged again.
    main_wt = bare.parent / "main"
    (main_wt / "tasks" / "task-a").mkdir(parents=True)
    (main_wt / "tasks" / "task-a" / "task.md").write_text("# Task A gen 2\n")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "declare task-a again")

    wt2 = _claim_task(bare, "task-a", content="# task-a gen2")
    r2 = merge.merge_and_cleanup(bare, "task-a", "task-a", wt2, "main")
    assert r2.ok, r2.problems

    assert (main_wt / "archive" / "task-a" / "task.md").exists(), \
        "generation 1 must survive the reuse"
    gens = sorted(p for p in (main_wt / "archive" / "task-a").iterdir()
                  if p.is_dir() and p.name != "task.md")
    assert len(gens) == 1, gens
    assert (gens[0] / "task.md").exists(), \
        "generation 2 must land in a timestamped archive spot"
    tmp.cleanup()
    print("ok: the daemon merge path archives a reused slug without clobbering gen 1")


def test_merge_refuses_a_dirty_worktree() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")
    (wt / "uncommitted.py").write_text("# dirty")

    r = merge.merge_and_cleanup(bare, "task-a", "task-a", wt, "main")
    assert not r.ok
    assert any("uncommitted" in p for p in r.problems), r.problems
    assert wt.exists()
    tmp.cleanup()
    print("ok: a dirty worktree is never merged")


def test_merge_conflict_is_left_for_a_human_and_marked() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")

    main_wt = bare.parent / "main"
    (main_wt / "task.py").write_text("# conflicting")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "conflicting change on main")

    r = merge.merge_and_cleanup(bare, "task-a", "task-a", wt, "main")
    assert not r.ok
    assert r.conflicted, "a conflict must be marked, not silently retried"
    assert any("conflicts" in p for p in r.problems), r.problems
    assert wt.exists(), "worktree should survive a failed merge"
    assert "task-a" in _git(main_wt, "branch", "--list"), "branch survives"
    head = _git(main_wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    assert head, "the merge is left in progress so the state is derivable"
    tmp.cleanup()
    print("ok: a merge conflict is left in progress and marked, branch kept")


def test_merge_refuses_while_the_base_is_mid_merge() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")
    wt2 = _claim_task(bare, "task-b")

    main_wt = bare.parent / "main"
    (main_wt / "task.py").write_text("# conflicting")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "conflicting change on main")

    r1 = merge.merge_and_cleanup(bare, "task-a", "task-a", wt, "main")
    assert r1.conflicted
    r2 = merge.merge_and_cleanup(bare, "task-b", "task-b", wt2, "main")
    assert not r2.ok and any("mid-merge" in p for p in r2.problems), r2.problems
    assert wt2.exists(), "the second task must not be torn down either"
    tmp.cleanup()
    print("ok: the daemon cannot merge a second task over a mid-merge base")


def test_remove_task_handles_the_noop_case() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    # claim but produce nothing (branch == base)
    wt = bare.parent / "noop-task"
    _git(bare, "worktree", "add", "-b", "noop-task", str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "nothing")

    main_wt = bare.parent / "main"
    (main_wt / "tasks" / "noop-task").mkdir(parents=True)
    (main_wt / "tasks" / "noop-task" / "task.md").write_text("# Noop\n")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "declare noop-task")

    r = merge.remove_task(bare, "noop-task", "noop-task", wt, "main")
    assert r.ok, r.problems
    assert not wt.exists()
    assert "noop-task" not in _git(main_wt, "branch", "--list")
    assert (main_wt / "archive" / "noop-task" / "task.md").exists()
    tmp.cleanup()
    print("ok: a no-op task is removed and archived")


def test_reset_task_returns_to_todo_without_archiving() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")

    main_wt = bare.parent / "main"
    r = merge.reset_task(bare, "task-a", "task-a", wt, "main")
    assert r.ok, r.problems
    assert not wt.exists()
    assert "task-a" not in _git(main_wt, "branch", "--list")
    assert (main_wt / "tasks" / "task-a" / "task.md").exists(), \
        "reset keeps the spec in the backlog"
    assert not (main_wt / "archive" / "task-a").exists()
    tmp.cleanup()
    print("ok: reset deletes the branch and worktree, keeps the spec")


def test_pre_task_cleanup_hook_blocks() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")

    main_wt = bare.parent / "main"
    hook = main_wt / ".orchestration" / "hooks" / "pre-task-cleanup"
    hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    hook.chmod(0o755)
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "add blocking hook")

    r = merge.merge_and_cleanup(bare, "task-a", "task-a", wt, "main")
    assert not r.ok
    assert any("blocked" in p for p in r.problems)
    assert wt.exists(), "worktree should survive a blocked cleanup"

    assert "work for task-a" in _git(main_wt, "log", "--oneline"), \
        "merge should have landed before the hook"
    tmp.cleanup()
    print("ok: pre-task-cleanup hook blocks worktree removal, merge survives")


def test_scan_after_merge_sees_nothing_claimed() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    wt = _claim_task(bare, "task-a")
    merge.merge_and_cleanup(bare, "task-a", "task-a", wt, "main")

    main_wt = bare.parent / "main"
    proj = state.Project.from_dir(main_wt)
    assert proj is not None
    tasks = {t.name: t for t in proj.scan()}
    assert "task-a" in tasks and tasks["task-a"].archived
    assert tasks["task-a"].status == state.ARCHIVED
    tmp.cleanup()
    print("ok: after merge the task reads as archived")


def test_cli_merge_fires_the_merged_global_hook() -> None:
    import argparse
    from orch import cli

    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        marker = Path(tmp.name) / "marker"
        hooks_dir = Path(tmp.name) / "orch" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "merged").write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"$ORCH_EVENT:$ORCH_ACTION:$PROJECT_NAME:$TASK_NAME\" > {marker}\n")
        (hooks_dir / "merged").chmod(0o755)

        bare = _bare_repo(tmp)
        _claim_task(bare, "task-a")
        main_wt = bare.parent / "main"

        rc = cli.cmd_task_merge(argparse.Namespace(directory=str(main_wt),
                                                   name="task-a"))
        assert rc == 0, rc
        deadline = time.time() + 5
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the CLI merge must fire the merged global hook"
        assert marker.read_text().strip() == "merged:cli:lab:task-a"
    finally:
        tmp.cleanup()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: `orch task merge` fires the merged global hook with the env contract")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} merge tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
