from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch
from . import init as init_mod
from . import state

DEFAULT_BASE = "main"

GITIGNORE = """node_modules/
dist/
build/
.DS_Store
*.log
*.sample
*.example
"""


@dataclass
class CreateResult:
    ok: bool
    steps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def step(self, msg: str) -> None:
        self.steps.append(msg)

    def fail(self, msg: str) -> "CreateResult":
        self.problems.append(msg)
        self.ok = False
        return self


def _run(args: list[str], cwd: Path | None = None,
         timeout: float = 120.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def gh_ready() -> tuple[bool, str | None]:
    """gh exists and is authenticated; abort the create if not."""
    if shutil.which("gh") is None:
        return False, ("gh is not installed. Install it (brew install gh), "
                       "or pass --no-gh to create the project locally only.")
    rc, _ = _run(["gh", "auth", "status"])
    if rc != 0:
        return False, ("gh is not authenticated. Run `gh auth login` first, "
                       "or pass --no-gh to create the project locally only.")
    return True, None


def _unsupervised(parent: Path, roots: list[str]) -> str | None:
    for r in roots:
        root = Path(r).expanduser()
        if parent == root or parent.is_relative_to(root):
            return None
    return ("the parent directory is outside every supervised root. "
            "orch only supervises what the daemon discovers, so a project "
            "created here would never be settled. Create inside one of: "
            + ", ".join(repr(r) for r in roots)
            + ". To supervise elsewhere, set ORCH_ROOTS (e.g. ORCH_ROOTS=/ "
              "supervises the whole filesystem), or pass --allow-unsupervised "
              "to create an unsupervised project anyway.")


def trust(start: Path) -> tuple[bool, str]:
    """Mark the current project's repo as trusted by the machine owner, so the
    daemon will verify, merge and dispatch here. The marker lives outside the
    repo - a project cannot trust itself."""
    _top, common, err = dispatch.project_of(start)
    if err or common is None:
        return False, err or "not inside a git worktree"
    if state.is_trusted(common):
        return True, f"already trusted: {common}"
    state.trust_project(common)
    return True, f"trusted {common} - the daemon will verify, merge and dispatch here"


def untrust(start: Path) -> tuple[bool, str]:
    """Revoke the daemon's permission to act on this project. It does not kill
    a running agent or touch any worktree - it just stops settling them."""
    _top, common, err = dispatch.project_of(start)
    if err or common is None:
        return False, err or "not inside a git worktree"
    if not state.is_trusted(common):
        return True, f"not trusted: {common}"
    state.untrust_project(common)
    return True, f"untrusted {common} - the daemon will stop acting on it"


def list_projects() -> list[dict]:
    trusted = state.trusted_projects()
    return [
        {"common": str(common), "trusted": str(common) in trusted,
         "orchDir": str(orch)}
        for common, orch in sorted(state.discover_projects().items())
    ]


def create(name: str, path: str | None = None, gh: bool = True,
           public: bool = False, description: str | None = None,
           base: str = DEFAULT_BASE, version: str = "0",
           roots: list[str] | None = None,
           allow_unsupervised: bool = False) -> CreateResult:
    result = CreateResult(ok=True)

    name = name.strip()
    if not name or "/" in name or name in (".", ".."):
        return result.fail(f"{name!r} is not a usable project name")

    parent = (Path(path).expanduser() if path else Path.cwd()).resolve()
    project = parent / name
    store = project / f"{name}.git"
    target = project / "main"
    if project.exists():
        return result.fail(f"{project} already exists; pick another name or --path")

    if gh:
        ok, err = gh_ready()
        if not ok:
            return result.fail(err or "gh is not available")

    if not allow_unsupervised:
        supervised = _unsupervised(parent, roots if roots is not None
                                   else state.default_roots())
        if supervised:
            return result.fail(supervised)

    def fail(msg: str) -> CreateResult:
        shutil.rmtree(project, ignore_errors=True)
        shutil.rmtree(store, ignore_errors=True)
        return result.fail(msg)

    parent.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    rc, out = _run(["git", "init", "--bare", "-q", "-b", base, str(store)],
                   cwd=parent)
    if rc != 0:
        return fail(out or "git init --bare failed")
    result.step(f"git init --bare -b {base} {project.name}/{store.name}")

    target.mkdir(parents=True, exist_ok=True)
    created, _skipped = init_mod.scaffold(target, version)
    for c in created:
        result.step(f"created {c}")

    (target / "README.md").write_text(f"# {name}\n")
    (target / ".gitignore").write_text(GITIGNORE)

    rc, out = _run(["git", "--git-dir", str(store), "--work-tree", str(target),
                    "add", "-A"], cwd=parent)
    if rc != 0:
        return fail(out or "git add failed")
    rc, out = _run(["git", "--git-dir", str(store), "write-tree"], cwd=parent)
    if rc != 0:
        return fail(out or "git write-tree failed")
    tree = (out.strip().splitlines() or [""])[-1]
    rc, out = _run([
        "git", "-c", "user.name=orch", "-c", "user.email=orch@localhost",
        "--git-dir", str(store), "commit-tree", tree, "-m",
        "scaffold: orch init, base branch"], cwd=parent)
    if rc != 0:
        return fail(out or "git commit-tree failed")
    commit = (out.strip().splitlines() or [""])[-1]
    rc, out = _run(["git", "--git-dir", str(store), "update-ref",
                    f"refs/heads/{base}", commit], cwd=parent)
    if rc != 0:
        return fail(out or "git update-ref failed")
    result.step("committed the base branch")

    shutil.rmtree(target, ignore_errors=True)

    rc, out = _run(["git", "--git-dir", str(store), "worktree", "add",
                    str(target), base], cwd=parent)
    if rc != 0:
        return fail(out or "git worktree add failed")
    result.step(f"git worktree add {target.relative_to(project)} on "
                f"{base} (base worktree)")

    init_mod.scaffold(target, version)

    rc, common = _run(["git", "rev-parse", "--path-format=absolute",
                       "--git-common-dir"], cwd=target)
    if rc == 0 and common:
        state.trust_project(Path(common))
        result.step("trusted this project - the daemon will supervise it")

    if gh:
        vis = "public" if public else "private"
        cmd = ["gh", "repo", "create", name, f"--{vis}", "--source", str(store),
               "--remote", "origin", "--push"]
        if description:
            cmd += ["--description", description]
        rc, out = _run(cmd)
        if rc != 0:
            return fail(out or "gh repo create failed")
        result.step(f"created a {vis} GitHub repo and pushed origin")

    return result


def name_from_url(url: str) -> str:
    """The last path component of a repo url, minus a trailing `.git`."""
    base = (url or "").rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return base.strip()


def _branches(store: Path) -> list[str]:
    rc, out = _run(["git", "-C", str(store), "for-each-ref",
                    "--format=%(refname:short)", "refs/heads/"])
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _committed_file(store: Path, base: str, rel: str) -> str | None:
    rc, out = _run(["git", "-C", str(store), "show", f"{base}:{rel}"])
    return out if rc == 0 else None


def _set_base_branch(worktree: Path, base: str) -> None:
    cfg = worktree / ".orchestration" / "config.json"
    try:
        data = json.loads(cfg.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("baseBranch") == base:
        return
    data["baseBranch"] = base
    cfg.write_text(json.dumps(data, indent=2) + "\n")


def clone(
    url: str,
    name: str | None = None,
    path: str | None = None,
    base: str | None = None,
    trust: bool = False,
    version: str = "0",
    roots: list[str] | None = None,
    allow_unsupervised: bool = False,
    pick_branch: Callable[[list[str]], str | None] | None = None,
    ask_trust: Callable[[], bool] | None = None,
) -> CreateResult:
    """Clone an existing repo into orch's layout: a bare store (`<name>.git`)
    with a base worktree checked out beside it, so the daemon can supervise it.

    Two kinds of repo are handled differently:
    - no `.orchestration/` in the clone: `orch init` scaffolding is committed to
      the base branch and the project is trusted - we created the orchestration,
      exactly like `project create`.
    - `.orchestration/` already committed: the clone's orchestration is left
      as-is and, because that is third-party code, the project is NOT trusted
      unless `trust` is passed (or `ask_trust()` answers yes).
    """
    result = CreateResult(ok=True)

    url = (url or "").strip()
    if not url:
        return result.fail("no repository url given")

    name = (name or "").strip() or name_from_url(url)
    if not name or "/" in name or name in (".", ".."):
        return result.fail(f"{name!r} is not a usable project name")

    parent = (Path(path).expanduser() if path else Path.cwd()).resolve()
    project = parent / name
    store = project / f"{name}.git"
    if project.exists():
        return result.fail(f"{project} already exists; pick another name or --path")

    if not allow_unsupervised:
        supervised = _unsupervised(parent, roots if roots is not None
                                   else state.default_roots())
        if supervised:
            return result.fail(supervised)

    def fail(msg: str) -> CreateResult:
        shutil.rmtree(project, ignore_errors=True)
        shutil.rmtree(store, ignore_errors=True)
        return result.fail(msg)

    parent.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    rc, out = _run(["git", "clone", "--bare", url, str(store)], cwd=parent)
    if rc != 0:
        return fail(out or f"git clone --bare failed for {url}")
    result.step(f"git clone --bare {url} {project.name}/{store.name}")
    result.step("origin points at the cloned url (provenance; orch is local-only)")

    branches = _branches(store)
    if not branches:
        return fail("the clone has no branches to supervise")

    if base is None:
        if DEFAULT_BASE in branches:
            base = DEFAULT_BASE
        elif pick_branch is not None:
            base = pick_branch(branches)
            if base is None:
                return fail("no base branch chosen")
        else:
            return fail(
                f"the clone has no {DEFAULT_BASE!r} branch (branches: "
                + ", ".join(branches)
                + "). Pass --base <branch> to pick the base branch."
            )
    if base not in branches:
        return fail(f"{base!r} is not a branch in {url} (branches: "
                    + ", ".join(branches) + ")")
    result.step(f"base branch: {base}")

    target = project / base
    rc, out = _run(["git", "-C", str(store), "worktree", "add",
                    str(target), base], cwd=parent)
    if rc != 0:
        return fail(out or "git worktree add failed")
    result.step(f"git worktree add {target.relative_to(project)} on "
                f"{base} (base worktree)")

    rc, out = _run(["git", "-C", str(store), "ls-tree", "-d", base, "--",
                    ".orchestration"])
    has_orch = rc == 0 and bool(out.strip())

    def commit(message: str) -> bool:
        rc, out = _run(["git", "-C", str(target), "add", "-A"], cwd=parent)
        if rc != 0:
            return result.fail(out or "git add failed") is None
        rc, out = _run(["git", "-C", str(target), "-c", "user.name=orch",
                        "-c", "user.email=orch@localhost", "commit", "-q",
                        "-m", message], cwd=parent)
        if rc != 0:
            return result.fail(out or "git commit failed") is None
        result.step(message)
        return True

    if has_orch:
        result.step("the clone already ships .orchestration - leaving it as-is")
        committed = _committed_file(store, base, ".orchestration/config.json")
        cfg_base = None
        if committed:
            try:
                cfg_base = json.loads(committed).get("baseBranch")
            except ValueError:
                cfg_base = None
        if cfg_base and cfg_base != base:
            _set_base_branch(target, base)
            if not commit(f"orch: align .orchestration/config.json baseBranch "
                          f"to {base}"):
                return result

        do_trust = trust
        if not do_trust and ask_trust is not None:
            do_trust = ask_trust()
        if do_trust:
            state.trust_project(store)
            result.step("trusted this project - the daemon will verify, merge "
                        "and dispatch here")
        else:
            result.step("NOT trusted: the clone's orchestration is third-party "
                        "code. `orch project trust` (or `orch project clone "
                        "--trust`) opts it in.")
    else:
        created, _skipped = init_mod.scaffold(target, version)
        for c in created:
            result.step(f"created {c}")
        if base != DEFAULT_BASE:
            _set_base_branch(target, base)
        if not commit(f"orch init: orchestration on {base}"):
            return result
        state.trust_project(store)
        result.step("trusted this project - the daemon will verify, merge and "
                    "dispatch here")

    return result


def base(common: Path | str, target_branch: str) -> CreateResult:
    """Repoint an existing project's base branch onto <target_branch>: the
    base worktree becomes the one checked out on <target_branch> and its
    committed .orchestration/config.json records baseBranch=<target_branch>.
    The branch must already exist (parity with clone's --base). The old base
    branch and its worktree are left untouched as an ordinary checkout, and
    trust is not modified."""
    result = CreateResult(ok=True)
    store = Path(common)
    project = store.parent
    branch = (target_branch or "").strip()

    branches = _branches(store)
    if branch not in branches:
        return result.fail(
            f"{branch!r} is not a branch in {store} (branches: "
            + (", ".join(branches) if branches else "none")
            + "). Create the branch in the store first.")
    result.step(f"base branch: {branch}")

    target = project / branch
    if state._base_worktree(store, branch) is None:
        rc, out = _run(["git", "-C", str(store), "worktree", "add",
                        str(target), branch], cwd=project)
        if rc != 0:
            return result.fail(out or "git worktree add failed")
        result.step(f"git worktree add {target.relative_to(project)} on "
                    f"{branch} (base worktree)")
    else:
        result.step(f"worktree {target} already checked out on {branch}")

    _set_base_branch(target, branch)

    def commit(message: str) -> bool:
        rc, out = _run(["git", "-C", str(target), "add", "-A"],
                       cwd=project)
        if rc != 0:
            return result.fail(out or "git add failed") is None
        rc, out = _run(["git", "-C", str(target), "-c", "user.name=orch",
                        "-c", "user.email=orch@localhost", "commit", "-q",
                        "-m", message], cwd=project)
        if rc != 0:
            return result.fail(out or "git commit failed") is None
        result.step(message)
        return True

    rc, st = _run(["git", "-C", str(target), "status", "--porcelain"],
                   cwd=project)
    if rc == 0 and st:
        if not commit(f"orch: base branch is now {branch}"):
            return result
    else:
        result.step(f"baseBranch is already {branch} on {branch} - "
                    "nothing to commit")

    return result
