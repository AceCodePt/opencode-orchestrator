"""Merge a done task's branch into base, then clean up the worktree and branch.

The lifecycle of a task is::

    todo  ->  running  ->  done  ->  merged
                         ->  needs-review
                         ->  stuck

`done` means the daemon ran the committed gate itself and it passed. `merged`
means the branch was folded into base and the worktree + branch were removed.
Between the two, the work sits in a worktree that is verifiable by hand; after it,
only the history remains.

This is the first operation that *writes to the base branch*. Every prior step
created something new (a worktree, a branch, a lease); merging changes what
`main` points at. The gate established that the branch is good; this is the
mechanical step after that.

If the merge fails — because another task landed first and the branches
conflict — the task stays `done` and the worktree + branch survive. A human
resolves and re-runs `orch task merge <name>`.
"""

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
    """Find the worktree that is on the base branch.

    `git worktree list --porcelain` prints lines of `worktree <path>` / `branch
    refs/heads/<name>`. The bare repo itself has no `branch` line, and a detached
    HEAD shows `detached` instead — both are skipped.
    """
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
    """Merge a done task's branch into base and remove its worktree + branch.

    Idempotent: a task already `merged` returns ok without touching anything.
    """
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

    # The base worktree is where the merge lands. It is the worktree checked out
    # on the base branch — typically `main`. If it is missing (deleted by hand,
    # or the repo was restructured), the merge cannot proceed without creating
    # one, and creating one silently is a side effect beyond this tool's scope.
    base_wt = _find_base_worktree(bare, base)
    if base_wt is None:
        r.fail(f"no worktree on {base} found in {bare}; cannot merge without one")
        return r
    r.step(f"base worktree: {base_wt} on {base}")

    # Commit any uncommitted work in the task worktree before merging. The gate
    # verified the working tree; `git merge` only moves commits, so uncommitted
    # work would silently disappear. Agents are not reliable committers — observed
    # writing correct files and passing the gate without a single `git add`.
    code, out = _git(wt, "add", "-A")
    code, status = _git(wt, "status", "--porcelain")
    if status:
        code, out = _git(wt, "commit", "-q",
                        "-m", f"work for {name} (committed by orch)")
        if code != 0:
            r.fail(f"could not commit uncommitted work in {wt}: {out[:300]}")
            return r
        r.step(f"committed uncommitted work on {t.branch}")
        # Push the branch ref to the bare repo so the merge can see the new commit.
        code, out = _git(wt, "push", "origin", f"{t.branch}:{t.branch}")
        if code != 0:
            # Worktrees linked to a bare repo share the ref namespace, so push is
            # usually a no-op. If it fails, the merge will still work if the ref
            # is visible from the base worktree.
            pass

    # Merge the task branch into base.
    code, out = _git(base_wt, "merge", "--no-edit", "--no-ff", t.branch)
    if code != 0:
        # Abort the merge so the base worktree is not left in a conflicted state.
        _git(base_wt, "merge", "--abort")
        # Move to needs-review so the daemon stops retrying and a human can resolve.
        # A conflict is the normal outcome of two concurrent tasks touching the same
        # file — the work is verified, the merge is what's blocked.
        store.finish(project, name, NEEDS_REVIEW,
                     f"merge conflict: {out[-200:]}")
        r.fail(f"merge failed: {out[-1500:]}")
        r.fail(f"the branch {t.branch} was NOT merged and the worktree survives")
        r.fail(f"{name} -> needs-review (resolve the conflict and run "
               f"`orch task merge {name}`)")
        return r
    r.step(f"merged {t.branch} into {base}")

    # Run pre-task-cleanup from the task worktree before removing it.
    env = _hook_env(name, t.branch, wt, base)
    ok, msg = dispatch.run_hook(wt, "pre-task-cleanup", env)
    r.step(msg)
    if not ok:
        r.fail(f"pre-task-cleanup blocked the cleanup; worktree survives")
        # The merge already landed. The worktree and branch stay so a human can
        # inspect them, but the task is not marked merged.
        return r

    # Remove the worktree.
    code, out = _git(bare, "worktree", "remove", str(wt))
    if code != 0:
        r.step(f"worktree remove failed, trying --force: {out[:200]}")
        code, out = _git(bare, "worktree", "remove", "--force", str(wt))
    if code != 0:
        r.fail(f"could not remove worktree {wt}: {out[:300]}")
        return r
    r.step(f"removed worktree {wt}")

    # Run pre-branch-delete from the base worktree (the task worktree is gone).
    env["WORKTREE_PATH"] = str(wt)
    ok, msg = dispatch.run_hook(base_wt, "pre-branch-delete", env)
    r.step(msg)
    if not ok:
        r.fail(f"pre-branch-delete blocked; branch {t.branch} survives")
        return r

    # Delete the branch.
    code, out = _git(base_wt, "branch", "-d", t.branch)
    if code != 0:
        # -d refuses if the branch is not merged. --force is not appropriate here:
        # if -d says it's not merged, something is wrong. But the worktree is
        # already gone, so losing the branch ref would lose the work. Leave it.
        r.fail(f"could not delete branch {t.branch}: {out[:300]}")
        return r
    r.step(f"deleted branch {t.branch}")

    # Run post-task-cleanup from the base worktree.
    ok, msg = dispatch.run_hook(base_wt, "post-task-cleanup", env)
    r.step(msg)

    store.finish(project, name, MERGED,
                 f"merged {t.branch} into {base}, worktree + branch cleaned up")
    r.step(f"{name} -> merged")
    return r