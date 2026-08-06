from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import init as init_mod

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
                           capture_output=True, text=True, timeout=timeout)
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


def create(name: str, path: str | None = None, gh: bool = True,
           public: bool = False, description: str | None = None,
           base: str = DEFAULT_BASE, version: str = "0") -> CreateResult:
    result = CreateResult(ok=True)

    name = name.strip()
    if not name or "/" in name or name in (".", ".."):
        return result.fail(f"{name!r} is not a usable project name")

    parent = Path(path).expanduser() if path else Path.cwd()
    target = parent / name
    if target.exists():
        return result.fail(f"{target} already exists; pick another name or --path")

    if gh:
        ok, err = gh_ready()
        if not ok:
            return result.fail(err or "gh is not available")

    parent.mkdir(parents=True, exist_ok=True)
    rc, out = _run(["git", "init", "-b", base, name], cwd=parent)
    if rc != 0:
        return result.fail(out or "git init failed")
    result.step(f"git init -b {base} {target}")
    target.mkdir(parents=True, exist_ok=True)

    r = init_mod.run(target, force=False, version=version)
    for c in r.created:
        result.step(f"created {c}")

    (target / "README.md").write_text(f"# {name}\n")
    (target / ".gitignore").write_text(GITIGNORE)

    rc, out = _run(["git", "add", "-A"], cwd=target)
    if rc != 0:
        return result.fail(out or "git add failed")
    rc, out = _run(["git", "-c", "user.name=orch", "-c", "user.email=orch@localhost",
                    "commit", "-q", "-m", "scaffold: orch init, base branch"], cwd=target)
    if rc != 0:
        return result.fail(out or "initial commit failed")
    result.step("committed the base branch")

    if gh:
        vis = "public" if public else "private"
        cmd = ["gh", "repo", "create", name, f"--{vis}", "--source", str(target),
               "--remote", "origin", "--push"]
        if description:
            cmd += ["--description", description]
        rc, out = _run(cmd)
        if rc != 0:
            return result.fail(out or "gh repo create failed")
        result.step(f"created a {vis} GitHub repo and pushed origin")

    return result
