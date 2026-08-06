from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import state, tasks  # noqa: E402


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root, tmp


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _project() -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    home = Path(tmp.name)
    bare = home / "app.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    main = home / "app-main"
    _git(bare, "worktree", "add", "-q", "--orphan", "-b", "main", str(main))
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "seed").write_text("x")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "seed")
    feature = home / "app-feature"
    _git(bare, "worktree", "add", "-q", str(feature), "-b", "feature", "main")
    return main, feature, tmp


def test_title_is_derived_from_the_slug() -> None:
    assert tasks.title_from_name("fix-auth") == "Fix Auth"
    assert tasks.title_from_name("Silence Watchdog") == "Silence Watchdog"
    assert tasks.title_from_name("weird__NAME!!") == "Weird Name"
    print("ok: titles derive from the slug")


def test_template_carries_every_required_section() -> None:
    body = tasks.template("Fix Auth")
    assert body.startswith("---\nhitl: true\n---\n\n# Task: Fix Auth\n")
    for section in ("## Metadata", "## Context", "## Requirements",
                    "## Verification", "## Prohibited Patterns"):
        assert section in body, section
    print("ok: the template carries the hitl front matter and every required section")


def test_add_creates_the_spec_and_never_touches_tasks_md() -> None:
    root, tmp = _repo()
    r = tasks.add_many(root, ["fix-auth"])
    assert r.ok, r.notes
    body = (root / "tasks" / "fix-auth" / "task.md").read_text()
    assert body.startswith("---\nhitl: true\n---\n\n# Task: Fix Auth")
    assert not (root / "TASKS.md").exists(), \
        "no TASKS.md is written or generated any more"
    assert any("COMMIT" in n for n in r.notes), "must say the files need committing"
    tmp.cleanup()
    print("ok: add creates the spec and writes no TASKS.md")


def test_add_is_idempotent() -> None:
    root, tmp = _repo()
    tasks.add_many(root, ["fix-auth"])
    (root / "tasks" / "fix-auth" / "task.md").write_text("# Task: edited by hand\n")
    r = tasks.add_many(root, ["fix-auth"])
    assert r.ok, r.notes
    assert (root / "tasks" / "fix-auth" / "task.md").read_text() \
        == "# Task: edited by hand\n", "an existing spec must never be overwritten"
    assert len(r.skipped) == 1 and "already exists" in r.skipped[0], r.skipped
    tmp.cleanup()
    print("ok: re-adding overwrites nothing")


def test_several_names_in_one_call() -> None:
    root, tmp = _repo()
    r = tasks.add_many(root, ["one-thing", "two-thing"])
    assert r.ok, r.notes
    for n in ("one-thing", "two-thing"):
        assert (root / "tasks" / n / "task.md").is_file()
    tmp.cleanup()
    print("ok: several names in one call")


def test_title_or_desc_with_several_names_is_refused() -> None:
    root, tmp = _repo()
    r = tasks.add_many(root, ["a-thing", "b-thing"], title="One Title")
    assert not r.ok
    assert any("several names" in n for n in r.notes), r.notes
    assert not (root / "tasks").exists(), "a refusal must write nothing"
    tmp.cleanup()
    print("ok: --title/--desc with several names is refused")


def test_the_current_worktree_is_the_default_target() -> None:
    main, feature, tmp = _project()
    r = tasks.add_many(feature, ["urgent-fix"])
    assert r.ok, r.notes
    assert (feature / "tasks" / "urgent-fix" / "task.md").is_file(), \
        "the default target is where you are"
    assert not (main / "tasks").exists(), "no other worktree may be touched"
    tmp.cleanup()
    print("ok: the current worktree is the default target")


def test_template_carries_the_hitl_front_matter() -> None:
    manual = tasks.template("Manual Pick")
    assert manual.startswith("---\nhitl: true\n"), manual[:40]
    auto = tasks.template("Auto Pick", hitl=False)
    assert "hitl: false" in auto, auto[:60]
    main, feature, tmp = _project()
    r = tasks.add_many(feature, ["urgent-fix"], auto=True)
    assert r.ok, r.notes
    body = (feature / "tasks" / "urgent-fix" / "task.md").read_text()
    assert "hitl: false" in body, body[:60]
    r = tasks.add_many(feature, ["manual-fix"])
    body = (feature / "tasks" / "manual-fix" / "task.md").read_text()
    assert "hitl: true" in body, body[:60]
    tmp.cleanup()
    print("ok: task.md carries the hitl flag in its front matter; auto opts in")


def test_being_off_base_is_warned_about_but_not_overridden() -> None:
    main, feature, tmp = _project()
    r = tasks.add_many(feature, ["urgent-fix"])
    assert r.ok, r.notes
    joined = " ".join(r.notes)
    assert "not dispatchable" in joined, r.notes
    assert "-C" in joined, "the note must name the way to place it on base"
    assert str(main) in joined, "and point at the worktree that has base"
    tmp.cleanup()
    print("ok: being off base warns loudly without moving the files")


def test_declared_names_reads_the_base_branch_only() -> None:
    main, feature, tmp = _project()
    tasks.add_many(main, ["on-base"])
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "task")
    tasks.add_many(feature, ["only-here"])
    _git(feature, "add", "-A")
    _git(feature, "commit", "-q", "-m", "task")
    proj = state.Project.from_dir(feature)
    assert proj is not None
    declared = [t.name for t in proj.scan() if t.declared]
    assert declared == ["on-base"], \
        "the project's task set is what base declares, not what a branch holds"
    tmp.cleanup()
    print("ok: declared names come from the base branch only")


def test_uncommitted_specs_are_not_declared() -> None:
    main, _feature, tmp = _project()
    tasks.add_many(main, ["not-yet"])
    proj = state.Project.from_dir(main)
    assert proj is not None
    assert [t.name for t in proj.scan() if t.declared] == []
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "task")
    assert [t.name for t in proj.scan() if t.declared] == ["not-yet"]
    tmp.cleanup()
    print("ok: only committed specs count as declared")


def test_outside_a_worktree_is_refused() -> None:
    tmp = tempfile.TemporaryDirectory()
    r = tasks.add_many(Path(tmp.name), ["fix-auth"])
    assert not r.ok and any("worktree" in n for n in r.notes), r.notes
    tmp.cleanup()
    print("ok: adding outside a worktree is refused")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} task-add tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
