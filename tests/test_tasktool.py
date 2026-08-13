from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import tasktool  # noqa: E402
from orch.tasktool import ToolError  # noqa: E402


def _git(cwd: Path, *args: str, ok: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=20)
    if ok:
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


def _commit_task(root: Path, name: str, wait_human_start: bool = False,
                 dependencies: list[str] | None = None) -> None:
    d = root / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    deps = ", ".join(dependencies or [])
    (d / "task.md").write_text(
        "---\n"
        f"wait_human_start: {'true' if wait_human_start else 'false'}\n"
        f"wait_human_merge: false\n"
        f"dependencies: [{deps}]\n"
        "---\n\n"
        f"# Task: {name.title()}\n\nDo it.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"task {name}")


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _created(root: Path, name: str) -> str:
    return (root / "tasks" / name / "task.md").read_text()


# --- resolution / refusal ---------------------------------------------------

def test_refuses_outside_a_project() -> None:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    _git(root, "init", "-q", "-b", "main")
    try:
        tasktool._project(root)
        assert False, "expected ToolError"
    except ToolError as e:
        assert ".orchestration" in str(e)
    tmp.cleanup()
    print("ok: the tools refuse a git repo that is not an orch project")


def test_refuses_off_base() -> None:
    root, tmp = _repo()
    wt = Path(tmp.name) / "feature"
    _git(root, "worktree", "add", "-q", "-b", "feature-x", str(wt), "main")
    try:
        tasktool._project(wt)
        assert False, "expected ToolError"
    except ToolError as e:
        assert "feature-x" in str(e) and "base" in str(e)
    _git(root, "worktree", "remove", "-f", str(wt))
    tmp.cleanup()
    print("ok: the tools refuse a session running off the base branch")


# --- create ---------------------------------------------------------------

def test_create_writes_a_spec_and_never_commits() -> None:
    root, tmp = _repo()
    before = _head(root)
    r = tasktool.create(root, root, "main", {
        "name": "auth-login",
        "title": "Add Login",
        "wait_human_start": True,
        "wait_human_merge": False,
        "requirements": ["user can log in", "passwords are hashed"],
        "verification": "run the auth tests",
    })
    assert r["path"] is not None
    assert r["path"] == "tasks/auth-login/task.md"
    body = _created(root, "auth-login")
    assert "wait_human_start: true" in body
    assert "wait_human_merge: false" in body
    assert "dependencies: []" in body
    assert "- [ ] user can log in" in body
    assert "# Task: Add Login" in body
    assert _head(root) == before, "a tool must never commit"
    tmp.cleanup()
    print("ok: create_task writes a spec with the required flags and never commits")


def test_create_defaults_the_wait_flags_to_false() -> None:
    root, tmp = _repo()
    r = tasktool.create(root, root, "main", {"name": "x"})
    assert r["path"] is not None
    body = _created(root, "x")
    assert "wait_human_start: false" in body
    assert "wait_human_merge: false" in body
    assert r["frontMatter"] == {"wait_human_start": False,
                                "wait_human_merge": False,
                                "dependencies": []}
    tmp.cleanup()
    print("ok: create_task defaults the wait flags to false - the AI decides them from the conversation")


def test_create_rejects_a_non_boolean_wait_flag() -> None:
    root, tmp = _repo()
    try:
        tasktool.create(root, root, "main",
                        {"name": "x", "wait_human_start": "true"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "boolean" in str(e)
    tmp.cleanup()
    print("ok: create_task still validates a wait flag's type when passed")


def test_create_refuses_an_existing_draft() -> None:
    root, tmp = _repo()
    tasktool.create(root, root, "main", {
        "name": "x", "wait_human_start": False, "wait_human_merge": False})
    try:
        tasktool.create(root, root, "main", {
            "name": "x", "wait_human_start": False, "wait_human_merge": False})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "exists" in str(e)
    tmp.cleanup()
    print("ok: create_task refuses to clobber an existing draft")


def test_create_refuses_a_committed_slug() -> None:
    root, tmp = _repo()
    _commit_task(root, "auth-login")
    try:
        tasktool.create(root, root, "main", {
            "name": "auth-login", "wait_human_start": False,
            "wait_human_merge": False})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "committed" in str(e)
    tmp.cleanup()
    print("ok: create_task refuses to overwrite a committed task")


# --- update ---------------------------------------------------------------

def test_update_is_partial_and_keeps_other_fields() -> None:
    root, tmp = _repo()
    tasktool.create(root, root, "main", {
        "name": "auth-login", "title": "Auth", "wait_human_start": True,
        "wait_human_merge": False,
        "requirements": ["one", "two"], "context": "why", "priority": "low"})
    r = tasktool.update(root, root, "main", {"name": "auth-login", "priority": "high"})
    assert r["path"] is not None
    body = _created(root, "auth-login")
    assert "- **Priority:** High" in body
    assert "- **Priority:** Low" not in body
    assert "wait_human_start: true" in body, "untouched fields must survive"
    assert "- [ ] one" in body
    assert "- [ ] two" in body
    assert "why" in body
    tmp.cleanup()
    print("ok: update_task changes only the fields that were passed")


def test_update_refuses_a_committed_task() -> None:
    root, tmp = _repo()
    _commit_task(root, "auth-login")
    try:
        tasktool.update(root, root, "main", {"name": "auth-login", "title": "X"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "committed" in str(e)
    tmp.cleanup()
    print("ok: update_task refuses a committed spec - commit is a human act")


def test_update_requires_at_least_one_field() -> None:
    root, tmp = _repo()
    tasktool.create(root, root, "main", {
        "name": "x", "wait_human_start": False, "wait_human_merge": False})
    try:
        tasktool.update(root, root, "main", {"name": "x"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "nothing to change" in str(e)
    tmp.cleanup()
    print("ok: update_task with no fields is refused")


# --- archive --------------------------------------------------------------

def test_archive_moves_declared_task_to_archive_uncommitted() -> None:
    root, tmp = _repo()
    _commit_task(root, "dark-mode")
    before = _head(root)
    r = tasktool.archive(root, root, "main", {"name": "dark-mode"})
    assert r["path"] is not None
    assert r["path"] == "archive/dark-mode/task.md"
    assert not (root / "tasks" / "dark-mode" / "task.md").exists()
    assert (root / "archive" / "dark-mode" / "task.md").exists()
    assert _head(root) == before, "archive must not commit"
    tmp.cleanup()
    print("ok: archive_task moves a declared task and stays uncommitted")


def test_archive_refuses_a_claimed_task() -> None:
    root, tmp = _repo()
    _commit_task(root, "dark-mode")
    _git(root, "branch", "dark-mode")
    try:
        tasktool.archive(root, root, "main", {"name": "dark-mode"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "claimed" in str(e)
    tmp.cleanup()
    print("ok: archive_task refuses claimed work - the daemon owns it")


def test_archive_refuses_an_uncommitted_draft() -> None:
    root, tmp = _repo()
    tasktool.create(root, root, "main", {
        "name": "x", "wait_human_start": False, "wait_human_merge": False})
    try:
        tasktool.archive(root, root, "main", {"name": "x"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "commit it first" in str(e)
    tmp.cleanup()
    print("ok: archive_task wants a committed task, not a draft")


# --- delete ---------------------------------------------------------------

def test_delete_removes_an_uncommitted_draft() -> None:
    root, tmp = _repo()
    tasktool.create(root, root, "main", {
        "name": "drafty", "wait_human_start": False, "wait_human_merge": False})
    r = tasktool.delete(root, root, "main", {"name": "drafty"})
    assert r["path"] is not None
    assert not (root / "tasks" / "drafty" / "task.md").exists()
    tmp.cleanup()
    print("ok: delete_task removes an uncommitted draft outright")


def test_delete_refuses_while_an_open_task_depends_on_it() -> None:
    root, tmp = _repo()
    _commit_task(root, "feature")
    _commit_task(root, "feature-slice", dependencies=["feature"])
    try:
        tasktool.delete(root, root, "main", {"name": "feature"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "feature-slice" in str(e)
    tmp.cleanup()
    print("ok: delete_task refuses while an open task depends on it")


def test_delete_of_a_committed_leaf_is_a_staged_rm() -> None:
    root, tmp = _repo()
    _commit_task(root, "obsolete")
    before = _head(root)
    r = tasktool.delete(root, root, "main", {"name": "obsolete"})
    assert r["path"] is not None
    assert not (root / "tasks" / "obsolete" / "task.md").exists()
    staged = _git(root, "diff", "--cached", "--name-only")
    assert "tasks/obsolete/task.md" in staged
    assert _head(root) == before, "delete must not commit"
    tmp.cleanup()
    print("ok: delete_task stages the removal of a committed task, never commits")


def test_delete_refuses_a_claimed_task() -> None:
    root, tmp = _repo()
    _commit_task(root, "busy")
    _git(root, "branch", "busy")
    try:
        tasktool.delete(root, root, "main", {"name": "busy"})
        assert False, "expected ToolError"
    except ToolError as e:
        assert "claimed" in str(e)
    tmp.cleanup()
    print("ok: delete_task refuses claimed work")


def test_all_ops_report_json() -> None:
    root, tmp = _repo()
    import io
    import contextlib
    from orch.tasktool import main as tt_main
    buf = io.StringIO()
    args = ['create', '--json',
            json.dumps({"name": "z", "wait_human_start": False,
                        "wait_human_merge": False}),
            '--dir', str(root)]
    with contextlib.redirect_stdout(buf):
        rc = tt_main(args)
    assert rc == 0, rc
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True and payload["path"] == "tasks/z/task.md"
    tmp.cleanup()
    print("ok: the tasktool CLI answers in JSON")


def test_orch_tasktool_subcommand_routes_to_the_backend() -> None:
    root, tmp = _repo()
    import io
    import contextlib
    from orch import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["tasktool", "create", "--json",
                       json.dumps({"name": "route-test"}),
                       "--dir", str(root)])
    assert rc == 0, rc
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert payload["path"] == "tasks/route-test/task.md"
    assert "wait_human_start: false" in _created(root, "route-test")
    tmp.cleanup()
    print("ok: `bin/orch tasktool <op> ...` routes to the backend")


def main() -> int:
    import os

    saved = os.environ.get("XDG_STATE_HOME")
    stmp = tempfile.TemporaryDirectory()
    os.environ["XDG_STATE_HOME"] = stmp.name
    try:
        tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
        for t in tests:
            t()
        print(f"\n{len(tests)} tasktool tests passed")
    finally:
        stmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = saved
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
