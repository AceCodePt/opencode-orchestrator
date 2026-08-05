from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import dispatch, tasks  # noqa: E402

BACKLOG = """# Project Tasks

## Backlog

Seeded in rough execution order. Later additions go at the top per the protocol.

**Foundations**

- [x] [Old Thing](tasks/old-thing/task.md) - done ages ago
"""


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return root, tmp


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


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
    assert body.startswith("# Task: Fix Auth\n")
    for section in ("## Metadata", "## Context", "## Requirements",
                    "## Verification", "## Prohibited Patterns"):
        assert section in body, section
    print("ok: the template carries every required section")


def test_entry_shape_with_and_without_a_description() -> None:
    assert tasks.backlog_entry("fix-auth", "Fix Auth") == \
        "- [ ] [Fix Auth](tasks/fix-auth/task.md)"
    e = tasks.backlog_entry("fix-auth", "Fix Auth", "harden login")
    assert e.endswith("\u2014 harden login"), e
    print("ok: backlog entry shape")


def test_entries_go_to_the_top_of_the_backlog_in_order() -> None:
    out = tasks.insert_backlog(BACKLOG, ["- [ ] [A](tasks/a/task.md)",
                                         "- [ ] [B](tasks/b/task.md)"])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines.index("- [ ] [A](tasks/a/task.md)") \
        < lines.index("- [ ] [B](tasks/b/task.md)"), "given order must be preserved"
    assert lines.index("- [ ] [B](tasks/b/task.md)") < lines.index("**Foundations**")
    assert lines.index("## Backlog") < lines.index("- [ ] [A](tasks/a/task.md)")
    assert "Seeded in rough execution order" in out
    print("ok: entries land at the top of the backlog, in the given order")


def test_backlog_section_is_appended_when_absent() -> None:
    out = tasks.insert_backlog("# Notes\n\nnothing here\n",
                               ["- [ ] [A](tasks/a/task.md)"])
    assert "## Backlog" in out and "- [ ] [A](tasks/a/task.md)" in out
    assert out.endswith("\n")
    print("ok: a missing Backlog section is appended")


def test_add_creates_the_spec_and_the_entry() -> None:
    root, tmp = _repo()
    (root / "TASKS.md").write_text(BACKLOG)
    r = tasks.add_many(root, ["fix-auth"], desc="harden login")
    assert r.ok, r.notes
    body = (root / "tasks" / "fix-auth" / "task.md").read_text()
    assert body.startswith("# Task: Fix Auth")
    listed = (root / "TASKS.md").read_text()
    assert "- [ ] [Fix Auth](tasks/fix-auth/task.md) \u2014 harden login" in listed
    assert any("COMMIT" in n for n in r.notes), "must say the files need committing"
    tmp.cleanup()
    print("ok: add creates the spec and lists it")


def test_add_is_idempotent() -> None:
    root, tmp = _repo()
    (root / "TASKS.md").write_text(BACKLOG)
    tasks.add_many(root, ["fix-auth"])
    (root / "tasks" / "fix-auth" / "task.md").write_text("# Task: edited by hand\n")
    r = tasks.add_many(root, ["fix-auth"])
    assert r.ok, r.notes
    assert (root / "tasks" / "fix-auth" / "task.md").read_text() \
        == "# Task: edited by hand\n", "an existing spec must never be overwritten"
    assert (root / "TASKS.md").read_text().count("tasks/fix-auth/task.md") == 1
    assert len(r.skipped) == 2, r.skipped
    tmp.cleanup()
    print("ok: re-adding overwrites nothing and duplicates nothing")


def test_tasks_md_is_created_when_missing() -> None:
    root, tmp = _repo()
    r = tasks.add_many(root, ["fix-auth"])
    assert r.ok, r.notes
    listed = (root / "TASKS.md").read_text()
    assert "## Backlog" in listed
    assert "- [ ] [Fix Auth](tasks/fix-auth/task.md)" in listed
    assert any("did not exist" in n for n in r.notes)
    tmp.cleanup()
    print("ok: a missing TASKS.md is created")


def test_several_names_in_one_call() -> None:
    root, tmp = _repo()
    (root / "TASKS.md").write_text(BACKLOG)
    r = tasks.add_many(root, ["one-thing", "two-thing"])
    assert r.ok, r.notes
    for n in ("one-thing", "two-thing"):
        assert (root / "tasks" / n / "task.md").is_file()
        assert f"tasks/{n}/task.md" in (root / "TASKS.md").read_text()
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
        "the default target is where you are, not where it would be dispatchable"
    assert "tasks/urgent-fix/task.md" in (feature / "TASKS.md").read_text()
    assert not (main / "tasks").exists(), "no other worktree may be touched"
    assert not (main / "TASKS.md").exists()
    tmp.cleanup()
    print("ok: the current worktree is the default target")


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


def test_directory_places_the_task_in_another_worktree() -> None:
    main, feature, tmp = _project()
    r = tasks.add_many(main, ["urgent-fix"])
    assert r.ok, r.notes
    assert (main / "tasks" / "urgent-fix" / "task.md").is_file()
    assert not (feature / "tasks").exists()
    assert not any("not dispatchable" in n for n in r.notes), r.notes
    tmp.cleanup()
    print("ok: an explicit directory places the task there, quietly")


def test_a_detached_head_is_still_named_in_the_warning() -> None:
    main, _feature, tmp = _project()
    _git(main, "checkout", "-q", "--detach", "HEAD")
    r = tasks.add_many(main, ["urgent-fix"])
    assert r.ok, r.notes
    assert any("detached HEAD" in n for n in r.notes), r.notes
    tmp.cleanup()
    print("ok: a detached HEAD is named rather than shown as None")


def test_declared_names_reads_the_base_branch_only() -> None:
    main, feature, tmp = _project()
    tasks.add_many(main, ["on-base"])
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "task")
    tasks.add_many(feature, ["only-here"])
    _git(feature, "add", "-A")
    _git(feature, "commit", "-q", "-m", "task")
    assert dispatch.declared_names(feature, "main") == ["on-base"], \
        "the project's task set is what base declares, not what a branch holds"
    tmp.cleanup()
    print("ok: declared names come from the base branch only")


def test_uncommitted_specs_are_not_declared() -> None:
    main, _feature, tmp = _project()
    tasks.add_many(main, ["not-yet"])
    assert dispatch.declared_names(main, "main") == []
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "task")
    assert dispatch.declared_names(main, "main") == ["not-yet"]
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
