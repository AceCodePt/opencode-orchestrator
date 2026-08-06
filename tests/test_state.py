from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import state  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "seed").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    orch = root / ".orchestration"
    orch.mkdir(parents=True)
    (orch / "config.json").write_text('{"baseBranch": "main"}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "orchestration")
    return root, tmp


def _task(root: Path, name: str) -> None:
    d = root / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(f"# {name}\n\nDo it.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"task {name}")


def _claim(root: Path, name: str) -> Path:
    wt = root.parent / name
    _git(root, "worktree", "add", "-b", name, str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    return wt


def _proj(root: Path) -> state.Project:
    proj = state.Project.from_dir(root)
    assert proj is not None
    return proj


def test_scan_lists_declared_tasks_as_todo() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    _task(root, "b-task")
    proj = _proj(root)
    tasks = proj.scan()
    by = {t.name: t for t in tasks}
    assert set(by) == {"a-task", "b-task"}
    assert by["a-task"].declared and not by["a-task"].claimed
    assert by["a-task"].status == state.TODO
    assert by["a-task"].base == "main"
    tmp.cleanup()
    print("ok: declared tasks scan as todo")


def test_hitl_defaults_to_true_and_comes_from_the_committed_front_matter() -> None:
    root, tmp = _repo()
    (root / "tasks" / "manual").mkdir(parents=True)
    (root / "tasks" / "manual" / "task.md").write_text("# Manual\n\nDo it.\n")
    (root / "tasks" / "auto").mkdir(parents=True)
    (root / "tasks" / "auto" / "task.md").write_text(
        "---\nhitl: false\n---\n\n# Auto\n\nDo it.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tasks")
    by = {t.name: t for t in _proj(root).scan()}
    assert by["manual"].hitl, "no front matter means a human must pick it"
    assert not by["auto"].hitl, "hitl: false opts the task into the scheduler"
    (root / "tasks" / "auto" / "task.md").write_text("# Auto\n\nDo it.\n")
    assert not {t.name: t for t in _proj(root).scan()}["auto"].hitl, \
        "an uncommitted working-tree edit must not count - the committed copy rules"
    tmp.cleanup()
    print("ok: hitl defaults to true and is read from the committed task.md "
          "front matter, never the working tree")


def test_scan_sees_a_claim_and_worktree() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.claimed and t.branch == "a-task"
    assert t.worktree == str(wt)
    assert t.diff is False and t.dirty is False
    tmp.cleanup()
    print("ok: a branch is a claim, and its worktree is found")


def test_scan_reports_dirty_and_diff() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    (wt / "work.txt").write_text("uncommitted")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.dirty, "uncommitted work must read as dirty"

    (wt / "work.txt").write_text("committed")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "work")
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert not t.dirty and t.diff, "a committed branch differs from base"
    assert t.status == state.MERGE_READY
    tmp.cleanup()
    print("ok: dirty and diff derive from the worktree")


def test_scan_clean_no_diff_is_a_noop() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    _claim(root, "a-task")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert not t.dirty and not t.diff
    assert t.status == state.NOOP
    tmp.cleanup()
    print("ok: a clean branch identical to base is a no-op")


def test_scan_marks_a_missing_worktree() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(wt)],
                   capture_output=True, check=True)
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.claimed and t.worktree is None
    assert t.status == state.NEEDS_REVIEW
    tmp.cleanup()
    print("ok: a claimed task with a missing worktree needs review")


def test_archived_tasks_are_listed_as_archived() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    (root / "archive" / "a-task").mkdir(parents=True)
    _git(root, "mv", "tasks/a-task/task.md", "archive/a-task/task.md")
    _git(root, "commit", "-q", "-m", "archive")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.archived and t.status == state.ARCHIVED
    tmp.cleanup()
    print("ok: an archived task is not todo")


def test_discover_finds_orchestration_dirs() -> None:
    root, tmp = _repo()
    found = state.discover_projects([str(Path(tmp.name))])
    assert str(root) in " ".join(str(v) for v in found.values())
    tmp.cleanup()
    print("ok: discovery finds a project by its .orchestration dir")


def test_fingerprint_tracks_worktree_content() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    fp1 = state.fingerprint(wt)
    (wt / "x").write_text("1")
    fp2 = state.fingerprint(wt)
    assert fp1 != fp2
    (wt / "x").write_text("1")
    assert state.fingerprint(wt) == fp2
    tmp.cleanup()
    print("ok: the fingerprint reflects the worktree state")


def test_base_branch_comes_from_config() -> None:
    root, tmp = _repo()
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "preview"}\n')
    proj = _proj(root)
    assert proj.base == "preview"
    tmp.cleanup()
    print("ok: base branch read from the project config")


def test_default_roots_include_home_and_mnt() -> None:
    saved = os.environ.get("ORCH_ROOTS")
    os.environ.pop("ORCH_ROOTS", None)
    try:
        roots = state.default_roots()
        assert str(Path.home()) in roots and "/mnt" in roots
    finally:
        if saved:
            os.environ["ORCH_ROOTS"] = saved
        else:
            os.environ.pop("ORCH_ROOTS", None)
    print("ok: default roots are $HOME and /mnt")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} state tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
