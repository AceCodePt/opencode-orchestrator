from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, project  # noqa: E402


def _args(**kw) -> argparse.Namespace:
    a = argparse.Namespace(name=None, flag_name=None, path=None, no_gh=None,
                           public=None, description=None,
                           allow_unsupervised=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class _FakeTTY:
    def __init__(self, replies: list[str], tty: bool = True,
                 raise_on_empty: bool = False):
        self._replies = iter(replies)
        self.tty = tty
        self.raise_on_empty = raise_on_empty

    def isatty(self) -> bool:
        return self.tty

    def readline(self) -> str:
        try:
            return next(self._replies) + "\n"
        except StopIteration:
            if self.raise_on_empty:
                raise AssertionError("unexpected prompt: stdin was read")
            return ""


class _Result:
    ok = True
    steps = ["git init -b main /x/demo", "committed the base branch"]
    problems = []


def _capture_create(captured: dict):
    def create(name, path=None, gh=True, public=False, description=None,
               version="0", roots=None, allow_unsupervised=False):
        captured.update(name=name, path=path, gh=gh, public=public,
                        description=description,
                        allow_unsupervised=allow_unsupervised)
        return _Result()
    return create


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
        r = project.create("demo", path=tmp, gh=False, roots=[tmp])
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
        r = project.create("demo", path=tmp, gh=True, description="a demo",
                           roots=[tmp])
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
        r = project.create("demo", path=tmp, gh=True, public=True, roots=[tmp])
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
            r = project.create("demo", path=tmp, gh=True, roots=[tmp])
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


def test_create_refuses_outside_the_supervised_roots() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = project.create("demo", path=tmp, gh=False)
        assert not r.ok, "creating in /tmp must be refused by default"
        msg = r.problems[0]
        assert "supervised root" in msg and "ORCH_ROOTS" in msg
    print("ok: create refuses a parent outside every supervised root")


def test_create_allow_unsupervised_bypasses_the_roots() -> None:
    calls: list = []
    _fake_run(calls)
    _fake_init(calls)
    with tempfile.TemporaryDirectory() as tmp:
        r = project.create("demo", path=tmp, gh=False,
                           allow_unsupervised=True)
        assert r.ok, r.problems
        assert any(a[0] == "git" for a, _c in calls), \
            "git init must still run when unsupervised is allowed"
    print("ok: --allow-unsupervised creates an unsupervised project anyway")


def test_create_inside_a_root_succeeds() -> None:
    calls: list = []
    _fake_run(calls)
    _fake_init(calls)
    with tempfile.TemporaryDirectory() as root:
        parent = Path(root) / "nested"
        parent.mkdir()
        r = project.create("demo", path=str(parent), gh=False,
                           roots=[root])
        assert r.ok, r.problems
    print("ok: create inside a supervised root proceeds normally")


def test_cmd_project_create_prints_next_steps() -> None:
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = lambda *a, **k: _Result()
    cli.sys.stdin = _FakeTTY([], tty=False)
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
        cli.sys.stdin = orig_stdin
    print("ok: `project create` prints the project steps and next actions")


def test_cmd_project_create_prompts_when_flags_missing() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY(["demo", "/tmp/here"])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args())
        assert rc == 0, rc
        assert captured["name"] == "demo"
        assert captured["path"] == "/tmp/here"
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: missing name and dir are asked for on a terminal")


def test_cmd_project_create_dir_prompt_defaults_to_cwd() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY(["demo", ""])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args())
        assert rc == 0, rc
        assert captured["name"] == "demo"
        assert captured["path"] == str(Path.cwd())
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: an empty dir answer falls back to the current directory")


def test_cmd_project_create_flags_skip_prompts() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY([], tty=True, raise_on_empty=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.cmd_project_create(_args(
                name="demo", path=tmp, no_gh=True, public=True,
                description="skip", allow_unsupervised=True))
        assert rc == 0, rc
        assert captured["name"] == "demo"
        assert captured["path"] == tmp
        assert captured["gh"] is False and captured["public"] is True
        assert captured["allow_unsupervised"] is True
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: explicit flags are used as-is with no prompting")


def test_cmd_project_create_noninteractive_missing_name_errors() -> None:
    called: list = []
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = lambda *a, **k: called.append(a)
    cli.sys.stdin = _FakeTTY([], tty=False)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args())
        assert rc == 1, rc
        assert called == [], "create must not run without a name"
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: without a terminal and without --name, create refuses clearly")


def test_cmd_project_create_gh_prompt_no_skips_github() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY(["demo", "/tmp/here", "n"])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args())
        assert rc == 0, rc
        assert captured["gh"] is False, captured
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: answering n to the GitHub prompt creates locally only")


def test_cmd_project_create_public_and_description_prompts() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY(["demo", "/tmp/here", "y", "y", "a demo repo"])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args())
        assert rc == 0, rc
        assert captured["gh"] is True
        assert captured["public"] is True
        assert captured["description"] == "a demo repo"
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: gh=yes then asks about visibility and description")


def test_cmd_project_create_unsupervised_prompts_for_override() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY(["y"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_project_create(_args(name="demo", path=tmp,
                                                  no_gh=True))
        assert rc == 0, rc
        assert captured["allow_unsupervised"] is True
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: an unsupervised parent asks before creating it anyway")


def test_cmd_project_create_supervised_dir_never_asks_override() -> None:
    captured: dict = {}
    orig = cli.project_mod.create
    orig_stdin = cli.sys.stdin
    cli.project_mod.create = _capture_create(captured)
    cli.sys.stdin = _FakeTTY([], tty=True, raise_on_empty=True)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_create(_args(name="demo",
                                              path=str(Path.home()),
                                              no_gh=True))
        assert rc == 0, rc
        assert captured["allow_unsupervised"] is False
    finally:
        cli.project_mod.create = orig
        cli.sys.stdin = orig_stdin
    print("ok: a supervised parent is created without any override prompt")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} project tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
