from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, project  # noqa: E402


def _args(**kw) -> object:
    class A:
        pass
    a = A()
    a.path = None
    a.no_gh = False
    a.public = False
    a.description = None
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _fake_run(calls: list) -> None:
    def run(args: list, cwd: Path | None = None, timeout: float = 120.0) -> tuple[int, str]:
        calls.append((args, cwd))
        return 0, ""
    project._run = run


def _fake_init(calls: list) -> None:
    def run(_start, force=False, version="0", force_hooks=False):
        calls.append(("init", str(_start)))
        return project.init_mod.InitResult(["config.json"], [], [])
    project.init_mod.run = run


def test_gh_ready_missing_gh_is_an_error() -> None:
    orig = project.shutil.which
    project.shutil.which = lambda _name: None
    try:
        ok, err = project.gh_ready()
        assert not ok and err and "--no-gh" in err
    finally:
        project.shutil.which = orig
    print("ok: gh missing is an abort, not a warning")


def test_gh_ready_unauthenticated_is_an_error() -> None:
    orig_which = project.shutil.which
    orig_run = project._run
    project.shutil.which = lambda _name: "/usr/bin/gh"
    project._run = lambda *a, **k: (1, "not logged in")
    try:
        ok, err = project.gh_ready()
        assert not ok and err and "auth" in err
    finally:
        project.shutil.which = orig_which
        project._run = orig_run
    print("ok: gh present but logged out is an abort, not a warning")


def test_create_aborts_when_gh_is_not_ready() -> None:
    calls: list = []
    _fake_run(calls)
    orig_ready = project.gh_ready
    project.gh_ready = lambda: (False, "gh is not installed. Install it")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = project.create("demo", path=tmp, gh=True)
            assert not r.ok
            assert r.problems and "gh is not installed" in r.problems[0]
            assert calls == [], "nothing runs when gh aborts first"
    finally:
        project.gh_ready = orig_ready
    print("ok: create aborts before touching anything when gh is unavailable")


def test_create_no_gh_skips_gh_entirely() -> None:
    calls: list = []
    _fake_run(calls)
    _fake_init(calls)
    with tempfile.TemporaryDirectory() as tmp:
        r = project.create("demo", path=tmp, gh=False)
        assert r.ok, r.problems
        gh = [a for a, _c in calls if isinstance(a, list) and a[:2] == ["gh", "repo"]]
        assert gh == [], f"no-gh must not call gh: {gh}"
        target = Path(tmp) / "demo"
        assert any(entry == ("init", str(target)) for entry in calls), \
            "orch init must run inside the new project"
        assert (target / "README.md").is_file()
        assert (target / ".gitignore").is_file()
    print("ok: --no-gh creates a local project with no GitHub call")


def test_create_gh_private_happy_path_order() -> None:
    calls: list = []
    _fake_run(calls)
    _fake_init(calls)
    with tempfile.TemporaryDirectory() as tmp:
        r = project.create("demo", path=tmp, gh=True, description="a demo")
        assert r.ok, r.problems
        kinds = [a[0] for a, _c in calls]
        assert any(a[:2] == ["git", "init"] for a, _ in calls)
        assert kinds[0] == "gh", "gh auth status is checked before anything runs"
        assert any(a == ["git", "add", "-A"] for a, _ in calls)
        commit = next(a for a, _ in calls if a[:2] == ["git", "-c"])
        assert "user.name=orch" in commit and "user.email=orch@localhost" in commit
        gh = next(a for a, _ in calls if a[:2] == ["gh", "repo"])
        assert gh == ["gh", "repo", "create", "demo", "--private",
                      "--source", str(Path(tmp) / "demo"),
                      "--remote", "origin", "--push", "--description", "a demo"]
        init_idx = next(i for i, entry in enumerate(calls) if entry[0] == "init")
        gh_idx = next(i for i, entry in enumerate(calls)
                      if isinstance(entry[0], list) and entry[0][:2] == ["gh", "repo"])
        assert init_idx < gh_idx, "init must run before the gh push"
    print("ok: gh path runs git init -> orch init -> commit -> gh push")


def test_create_gh_public_flag() -> None:
    calls: list = []
    _fake_run(calls)
    _fake_init(calls)
    with tempfile.TemporaryDirectory() as tmp:
        r = project.create("demo", path=tmp, gh=True, public=True)
        assert r.ok, r.problems
        gh = next(a for a, _ in calls if a[:2] == ["gh", "repo"])
        assert "--public" in gh and "--private" not in gh
    print("ok: --public flips the repo visibility")


def test_create_fails_when_gh_push_fails() -> None:
    calls: list = []
    orig = project._run
    orig_ready = project.gh_ready
    _fake_init(calls)
    project.gh_ready = lambda: (True, None)

    def run(args, cwd=None, timeout=120.0):
        calls.append((args, cwd))
        if args[:2] == ["gh", "repo"]:
            return 1, "repository already exists"
        return 0, ""
    project._run = run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = project.create("demo", path=tmp, gh=True)
            assert not r.ok
            assert any("repository already exists" in p for p in r.problems)
    finally:
        project._run = orig
        project.gh_ready = orig_ready
    print("ok: a failed gh repo create fails the create")


def test_create_refuses_bad_names_and_existing_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = project.create("a/b", path=tmp, gh=False)
        assert not r.ok and "usable" in r.problems[0]
        r = project.create("", path=tmp, gh=False)
        assert not r.ok
        target = Path(tmp) / "demo"
        target.mkdir()
        r = project.create("demo", path=tmp, gh=False)
        assert not r.ok and "already exists" in r.problems[0]
    print("ok: create refuses names that would escape or collide")


def test_cmd_project_create_prints_next_steps() -> None:
    class R:
        ok = True
        steps = ["git init -b main /x/demo", "committed the base branch"]
        problems = []
    orig = cli.project_mod.create
    cli.project_mod.create = lambda *a, **k: R()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args(name="demo"))
        out = buf.getvalue()
        assert rc == 0, rc
        assert "git init" in out and "next:" in out
        assert "task draft" in out
    finally:
        cli.project_mod.create = orig
    print("ok: `project create` prints the project steps and next actions")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} project tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
