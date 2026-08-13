from __future__ import annotations

import shutil
import subprocess
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
