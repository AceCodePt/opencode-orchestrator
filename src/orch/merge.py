from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, gate
from .store import DONE, MERGED, NEEDS_REVIEW, Store


@dataclass
class MergeResult:
    ok: bool = True
    steps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def step(self, msg: str) -> None:
        self.steps.append(msg)

    def fail(self, msg: str) -> None:
        self.problems.append(msg)
        self.ok = False


def _git(cwd: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _find_base_worktree(bare: Path, base: str) -> Path | None:
    code, out = _git(bare, "worktree", "list", "--porcelain")
    if code != 0:
        return None
    wt: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            name = ref.rsplit("/", 1)[-1]
            if name == base and wt:
                return Path(wt)
    return None


def _hook_env(task_name: str, branch: str, worktree: Path,
              base: str) -> dict[str, str]:
    return {
        "TASK_NAME": task_name,
        "BRANCH_NAME": branch,
        "WORKTREE_PATH": str(worktree),
        "WORKTREE_NAME": worktree.name,
        "BASE_BRANCH": base,
    }


def merge_and_cleanup(
    store: Store,
    project: str,
    name: str,
) -> MergeResult:
    r = MergeResult()
    t = store.get(project, name)
    if t is None:
        r.fail(f"no task {name} in {project}")
        return r
    if t.status == MERGED:
        r.step(f"{name} already merged")
        return r
    if t.status not in (DONE, NEEDS_REVIEW):
        r.fail(f"{name} is {t.status}, not done or needs-review")
        return r
    if not t.worktree or not t.branch:
        r.fail(f"{name} has no recorded worktree or branch")
        return r

    wt = Path(t.worktree)
    bare = Path(project)
    base = gate.base_branch(wt) if wt.exists() else "main"

    base_wt = _find_base_worktree(bare, base)
    if base_wt is None:
        r.fail(f"no worktree on {base} found in {bare}; cannot merge without one")
        return r
    r.step(f"base worktree: {base_wt} on {base}")

    code, out = _git(wt, "add", "-A")
    code, status = _git(wt, "status", "--porcelain")
    if status:
        code, out = _git(wt, "commit", "-q",
                        "-m", f"work for {name} (committed by orch)")
        if code != 0:
            r.fail(f"could not commit uncommitted work in {wt}: {out[:300]}")
            return r
        r.step(f"committed uncommitted work on {t.branch}")
        code, out = _git(wt, "push", "origin", f"{t.branch}:{t.branch}")
        if code != 0:
            pass

    code, out = _git(base_wt, "merge", "--no-edit", "--no-ff", t.branch)
    if code != 0:
        _git(base_wt, "merge", "--abort")
        store.finish(project, name, NEEDS_REVIEW,
                     f"merge conflict: {out[-200:]}")
        r.fail(f"merge failed: {out[-1500:]}")
        r.fail(f"the branch {t.branch} was NOT merged and the worktree survives")
        r.fail(f"{name} -> needs-review (resolve the conflict and run "
               f"`orch task merge {name}`)")
        return r
    r.step(f"merged {t.branch} into {base}")

    env = _hook_env(name, t.branch, wt, base)
    ok, msg = dispatch.run_hook(wt, "pre-task-cleanup", env)
    r.step(msg)
    if not ok:
        r.fail(f"pre-task-cleanup blocked the cleanup; worktree survives")
        return r

    code, out = _git(bare, "worktree", "remove", str(wt))
    if code != 0:
        r.step(f"worktree remove failed, trying --force: {out[:200]}")
        code, out = _git(bare, "worktree", "remove", "--force", str(wt))
    if code != 0:
        r.fail(f"could not remove worktree {wt}: {out[:300]}")
        return r
    r.step(f"removed worktree {wt}")

    env["WORKTREE_PATH"] = str(wt)
    ok, msg = dispatch.run_hook(base_wt, "pre-branch-delete", env)
    r.step(msg)
    if not ok:
        r.fail(f"pre-branch-delete blocked; branch {t.branch} survives")
        return r

    code, out = _git(base_wt, "branch", "-d", t.branch)
    if code != 0:
        r.fail(f"could not delete branch {t.branch}: {out[:300]}")
        return r
    r.step(f"deleted branch {t.branch}")

    ok, msg = dispatch.run_hook(base_wt, "post-task-cleanup", env)
    r.step(msg)

    store.finish(project, name, MERGED,
                 f"merged {t.branch} into {base}, worktree + branch cleaned up")
    r.step(f"{name} -> merged")
    return r
