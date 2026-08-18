from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, project, state  # noqa: E402

_REAL_RUN = project._run
_REAL_INIT = project.init_mod.run
_REAL_SCAFFOLD = project.init_mod.scaffold


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


def _capture_clone(captured: dict):
    def clone(url, name=None, path=None, base=None, trust=False, version="0",
              roots=None, allow_unsupervised=False, pick_branch=None,
              ask_trust=None):
        captured.update(url=url, name=name, path=path, base=base, trust=trust,
                        version=version,
                        allow_unsupervised=allow_unsupervised,
                        pick_branch=pick_branch, ask_trust=ask_trust)
        return _Result()
    return clone


def _clone_args(**kw) -> argparse.Namespace:
    a = argparse.Namespace(url=None, flag_name=None, path=None, base=None,
                           trust=False, allow_unsupervised=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _make_source(root: Path, name: str = "src", branch: str = "main",
                 with_orch: bool = False) -> str:
    """Build a real local git repo to clone from; returns its path."""
    src = root / name
    src.mkdir()
    (src / "README.md").write_text("# source\n")
    if with_orch:
        orch = src / ".orchestration"
        orch.mkdir()
        (orch / "config.json").write_text('{\n  "baseBranch": "main"\n}\n')
    for args in (
        ["git", "init", "-q", "-b", branch, str(src)],
        ["git", "-C", str(src), "config", "user.name", "tester"],
        ["git", "-C", str(src), "config", "user.email", "t@test"],
        ["git", "-C", str(src), "add", "-A"],
        ["git", "-C", str(src), "commit", "-q", "-m", "initial commit"],
    ):
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"{args} failed: {p.stderr}")
    return str(src)


def _set_state_env(tmp: Path) -> tuple[str | None, str | None]:
    import os as _os
    old = _os.environ.get("XDG_STATE_HOME")
    old_cfg = _os.environ.get("XDG_CONFIG_HOME")
    _os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    _os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
    return old, old_cfg


def _restore_state_env(old: str | None, old_cfg: str | None) -> None:
    import os as _os
    if old is None:
        _os.environ.pop("XDG_STATE_HOME", None)
    else:
        _os.environ["XDG_STATE_HOME"] = old
    if old_cfg is None:
        _os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        _os.environ["XDG_CONFIG_HOME"] = old_cfg


def _fake_run(calls: list) -> None:
    def run(args: list, cwd: Path | None = None, timeout: float = 120.0) -> tuple[int, str]:
        calls.append((args, cwd))
        return 0, ""
    project._run = run


def _fake_init(calls: list) -> None:
    def run(_start, force=False, version="0"):
        calls.append(("init", str(_start)))
        return project.init_mod.InitResult(["config.json"], [], [])

    def scaffold(_target, version="0", force=False):
        calls.append(("scaffold", str(_target)))
        return (["config.json"], [])
    project.init_mod.run = run
    project.init_mod.scaffold = scaffold


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
        target = Path(tmp) / "demo" / "main"
        args = [a for a, _c in calls]
        assert any(a[:2] == ["git", "init"] and "--bare" in a for a in args), \
            "a bare store is created"
        assert not any("clone" in a for a in args), \
            "no throwaway repo is cloned"
        assert any("--work-tree" in a and a[-2:] == ["add", "-A"] for a in args), \
            "the scaffold is staged into the store"
        assert any("commit-tree" in a for a in args), \
            "the scaffold is committed straight into the store"
        assert any("worktree" in a and "add" in a and str(target) in a
                   for a in args), \
            "the base worktree is added from the store"
        scaffolds = [e for e in calls if e[0] == "scaffold"]
        assert len(scaffolds) == 2, \
            "scaffolded into the staging dir and again into the born worktree"
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
        assert any("--work-tree" in a and a[-2:] == ["add", "-A"] for a, _ in calls), \
            "the scaffold is staged into the store"
        commit = next(a for a, _ in calls if a[:2] == ["git", "-c"])
        assert "user.name=orch" in commit and "user.email=orch@localhost" in commit
        gh = next(a for a, _ in calls if a[:2] == ["gh", "repo"])
        assert gh == ["gh", "repo", "create", "demo", "--private",
                      "--source", str(Path(tmp) / "demo" / "demo.git"),
                      "--remote", "origin", "--push", "--description", "a demo"]
        wt_idx = next(i for i, entry in enumerate(calls)
                      if isinstance(entry[0], list) and "worktree" in entry[0]
                      and "add" in entry[0])
        gh_idx = next(i for i, entry in enumerate(calls)
                      if isinstance(entry[0], list) and entry[0][:2] == ["gh", "repo"])
        assert wt_idx < gh_idx, "the base worktree must exist before the gh push"
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


def test_create_produces_a_bare_store_and_base_worktree() -> None:
    import os as _os
    orig_run = project._run
    orig_init = project.init_mod.run
    orig_scaffold = project.init_mod.scaffold
    project._run, project.init_mod.run = _REAL_RUN, _REAL_INIT
    project.init_mod.scaffold = _REAL_SCAFFOLD
    try:
        with tempfile.TemporaryDirectory() as tmp:
            old = _os.environ.get("XDG_STATE_HOME")
            old_cfg = _os.environ.get("XDG_CONFIG_HOME")
            _os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
            _os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            try:
                r = project.create("demo", path=tmp, gh=False,
                                   allow_unsupervised=True)
                assert r.ok, r.problems
                project_dir = Path(tmp) / "demo"
                store = project_dir / "demo.git"
                target = project_dir / "main"

                assert sorted(p.name for p in project_dir.iterdir()) == \
                    ["demo.git", "main"], \
                    "the project folder holds the bare store and the main worktree"
                rc, out = project._run(["git", "-C", str(store), "rev-parse",
                                        "--is-bare-repository"])
                assert rc == 0 and out == "true", f"store is not bare: {out!r}"
                rc, common = project._run(["git", "-C", str(target), "rev-parse",
                                           "--path-format=absolute",
                                           "--git-common-dir"])
                assert rc == 0 and Path(common) == store, common
                rc, branch = project._run(["git", "-C", str(target), "branch",
                                           "--show-current"])
                assert rc == 0 and branch == "main", branch
                rc, wt = project._run(["git", "-C", str(target), "worktree", "list"])
                assert rc == 0 and f"{target} " in wt and "[main]" in wt, wt
                rc, log = project._run(["git", "-C", str(target), "log",
                                        "--oneline"])
                assert rc == 0
                lines = [l for l in log.splitlines() if l.strip()]
                assert len(lines) == 1 and "scaffold" in lines[0], log
                rc, st = project._run(["git", "-C", str(target), "status",
                                       "--porcelain"])
                assert rc == 0 and st == "", f"worktree must be clean: {st!r}"
                assert (target / ".orchestration" / "hooks"
                        / "session-idle.sample").is_file(), \
                    "the born worktree must hold the gitignored hook templates"
                assert str(store) in state.discover_projects([tmp]), \
                    "discovery must map the bare store to the base worktree"
                assert state.is_trusted(store), \
                    "the bare store must be trusted"
            finally:
                if old is None:
                    _os.environ.pop("XDG_STATE_HOME", None)
                else:
                    _os.environ["XDG_STATE_HOME"] = old
                if old_cfg is None:
                    _os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    _os.environ["XDG_CONFIG_HOME"] = old_cfg
    finally:
        project._run = orig_run
        project.init_mod.run = orig_init
        project.init_mod.scaffold = orig_scaffold
    print("ok: create makes a bare store with a base worktree on main")


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
        assert "orch attach" in out
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


def test_name_from_url() -> None:
    assert project.name_from_url("https://github.com/a/b.git") == "b"
    assert project.name_from_url("git@github.com:a/b.git") == "b"
    assert project.name_from_url("https://github.com/a/b") == "b"
    assert project.name_from_url("/x/y/b/") == "b"
    assert project.name_from_url("https://github.com/a/") == "a"
    print("ok: the project name is the url's repo name without .git")


def test_cmd_project_clone_derives_name_and_calls_clone() -> None:
    captured: dict = {}
    orig = cli.project_mod.clone
    orig_stdin = cli.sys.stdin
    cli.project_mod.clone = _capture_clone(captured)
    cli.sys.stdin = _FakeTTY([], tty=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.cmd_project_clone(_clone_args(
                url="https://github.com/a/b.git", path=tmp,
                allow_unsupervised=True))
        assert rc == 0, rc
        assert captured["url"] == "https://github.com/a/b.git"
        assert captured["name"] == "b"
        assert captured["path"] == tmp
        assert captured["trust"] is False
        assert captured["pick_branch"] is None, \
            "non-interactive must not prompt for a branch"
        assert captured["ask_trust"] is None, \
            "non-interactive must not prompt for trust"
    finally:
        cli.project_mod.clone = orig
        cli.sys.stdin = orig_stdin
    print("ok: `project clone` derives the name from the url non-interactively")


def test_cmd_project_clone_noninteractive_missing_url_errors() -> None:
    called: list = []
    orig = cli.project_mod.clone
    orig_stdin = cli.sys.stdin
    cli.project_mod.clone = lambda *a, **k: called.append(a)
    cli.sys.stdin = _FakeTTY([], tty=False)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_project_clone(_clone_args())
        assert rc == 1, rc
        assert called == [], "clone must not run without a url"
    finally:
        cli.project_mod.clone = orig
        cli.sys.stdin = orig_stdin
    print("ok: without a terminal and without a url, clone refuses clearly")


def test_cmd_project_clone_prompts_for_base_branch() -> None:
    captured: dict = {}
    orig = cli.project_mod.clone
    orig_stdin = cli.sys.stdin
    cli.project_mod.clone = _capture_clone(captured)
    cli.sys.stdin = _FakeTTY(["2", ""], tty=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_project_clone(_clone_args(
                    url="https://github.com/a/b", path=tmp,
                    allow_unsupervised=True))
            assert rc == 0, rc
            pb = captured["pick_branch"]
            assert pb is not None, "interactive clone must offer a branch pick"
            assert pb(["master", "develop"]) == "develop"
            assert pb(["only"]) == "only"
    finally:
        cli.project_mod.clone = orig
        cli.sys.stdin = orig_stdin
    print("ok: interactive clone lets the user pick the base branch")


def test_cmd_project_clone_prompts_for_trust() -> None:
    captured: dict = {}
    orig = cli.project_mod.clone
    orig_stdin = cli.sys.stdin
    cli.project_mod.clone = _capture_clone(captured)
    cli.sys.stdin = _FakeTTY(["y", "y"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_project_clone(_clone_args(
                    url="https://github.com/a/b", path=tmp))
        assert rc == 0, rc
        at = captured["ask_trust"]
        assert at is not None, "interactive clone must offer a trust prompt"
        assert at() is True
    finally:
        cli.project_mod.clone = orig
        cli.sys.stdin = orig_stdin
    print("ok: interactive clone asks before trusting shipped orchestration")


def test_clone_no_main_noninteractive_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp), branch="master")
        r = project.clone(src, name="demo", path=tmp,
                          allow_unsupervised=True)
        assert not r.ok
        assert "--base" in r.problems[0], r.problems
    print("ok: a clone without main refuses non-interactively without --base")


def test_clone_no_main_picks_branch() -> None:
    picked: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp), branch="master")
        old, old_cfg = _set_state_env(Path(tmp))

        def pick(branches: list[str]) -> str | None:
            picked.append(branches)
            return "master"

        try:
            r = project.clone(src, name="demo", path=tmp, pick_branch=pick,
                              allow_unsupervised=True)
            assert r.ok, r.problems
            assert picked, "the branch pick is consulted when main is absent"
            target = Path(tmp) / "demo" / "master"
            rc, branch = project._run(["git", "-C", str(target),
                                       "branch", "--show-current"])
            assert rc == 0 and branch == "master", branch
            cfg = json.loads((target / ".orchestration" / "config.json")
                             .read_text())
            assert cfg["baseBranch"] == "master", cfg
            rc, log = project._run(["git", "-C", str(target), "log",
                                    "--oneline"])
            lines = [l for l in log.splitlines() if l.strip()]
            assert lines[0].endswith("orch init: orchestration on master"), log
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: a no-main clone picks the branch as the base and records it")


def test_clone_produces_a_bare_store_and_base_worktree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp))
        old, old_cfg = _set_state_env(Path(tmp))
        try:
            r = project.clone(src, name="demo", path=tmp,
                              allow_unsupervised=True)
            assert r.ok, r.problems
            project_dir = Path(tmp) / "demo"
            store = project_dir / "demo.git"
            target = project_dir / "main"

            assert sorted(p.name for p in project_dir.iterdir()) == \
                ["demo.git", "main"], \
                "the project folder holds the bare store and the main worktree"
            rc, out = project._run(["git", "-C", str(store), "rev-parse",
                                    "--is-bare-repository"])
            assert rc == 0 and out == "true", f"store is not bare: {out!r}"
            rc, branch = project._run(["git", "-C", str(target), "branch",
                                       "--show-current"])
            assert rc == 0 and branch == "main", branch
            rc, log = project._run(["git", "-C", str(target), "log",
                                    "--oneline"])
            lines = [l for l in log.splitlines() if l.strip()]
            assert lines[0].endswith("orch init: orchestration on main"), log
            assert "initial commit" in lines[1], \
                "the clone's history is preserved under the init commit"
            rc, st = project._run(["git", "-C", str(target), "status",
                                   "--porcelain"])
            assert rc == 0 and st == "", f"worktree must be clean: {st!r}"
            assert (target / "README.md").read_text() == "# source\n", \
                "the repo's own content is preserved"
            assert (target / ".orchestration" / "hooks"
                    / "session-idle.sample").is_file(), \
                "the born worktree must hold the gitignored hook templates"
            rc, origin = project._run(["git", "-C", str(store), "remote",
                                       "get-url", "origin"])
            assert rc == 0 and origin == src, \
                "the bare store keeps origin pointing at the cloned url"
            assert str(store) in state.discover_projects([tmp]), \
                "discovery must map the bare store to the base worktree"
            assert state.is_trusted(store), \
                "a freshly initialized clone is trusted like project create"
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: clone makes a bare store with an initialized base worktree")


def test_clone_existing_orch_is_not_trusted_by_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp), with_orch=True)
        old, old_cfg = _set_state_env(Path(tmp))
        try:
            r = project.clone(src, name="demo", path=tmp,
                              allow_unsupervised=True)
            assert r.ok, r.problems
            store = Path(tmp) / "demo" / "demo.git"
            assert not state.is_trusted(store), \
                "third-party orchestration must not be trusted by default"
            rc, log = project._run(["git", "-C", str(store), "log",
                                    "--oneline"])
            lines = [l for l in log.splitlines() if l.strip()]
            assert len(lines) == 1, \
                "no commit is added when the shipped base matches, " + log
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: a clone that already ships orchestration stays untrusted")


def test_clone_existing_orch_trust_flag_trusts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp), with_orch=True)
        old, old_cfg = _set_state_env(Path(tmp))
        try:
            r = project.clone(src, name="demo", path=tmp, trust=True,
                              allow_unsupervised=True)
            assert r.ok, r.problems
            store = Path(tmp) / "demo" / "demo.git"
            assert state.is_trusted(store), \
                "--trust opts a shipped-orchestration clone in"
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: --trust opts into trusting a clone that ships orchestration")


def test_clone_existing_orch_ask_trust_controls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp), with_orch=True)
        old, old_cfg = _set_state_env(Path(tmp))
        asked: list[bool] = []
        try:
            def ask() -> bool:
                asked.append(True)
                return True
            r = project.clone(src, name="demo", path=tmp,
                              allow_unsupervised=True, ask_trust=ask)
            assert r.ok, r.problems
            assert asked, "the trust prompt is consulted when no flag is given"
            assert state.is_trusted(Path(tmp) / "demo" / "demo.git")
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: ask_trust decides trust when --trust is absent")


def test_clone_existing_orch_aligns_base_branch_when_chosen_differs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp), branch="master", with_orch=True)
        old, old_cfg = _set_state_env(Path(tmp))
        try:
            r = project.clone(src, name="demo", path=tmp, base="master",
                              allow_unsupervised=True)
            assert r.ok, r.problems
            store = Path(tmp) / "demo" / "demo.git"
            rc, log = project._run(["git", "-C", str(store), "log",
                                    "--oneline"])
            lines = [l for l in log.splitlines() if l.strip()]
            assert "align" in lines[0], log
            rc, cfg = project._run(["git", "-C", str(store), "show",
                                    "master:.orchestration/config.json"])
            assert rc == 0 and json.loads(cfg)["baseBranch"] == "master", cfg
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: a base branch different from the shipped config is aligned")


def test_clone_name_flag_overrides_url_derivation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_source(Path(tmp))
        old, old_cfg = _set_state_env(Path(tmp))
        try:
            r = project.clone(src, name="renamed", path=tmp,
                              allow_unsupervised=True)
            assert r.ok, r.problems
            assert (Path(tmp) / "renamed" / "renamed.git").is_dir(), \
                "--name overrides the url-derived name"
        finally:
            _restore_state_env(old, old_cfg)
    print("ok: --name overrides the url-derived project name")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} project tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
