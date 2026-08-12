from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, server, state, verify

TASKS_REL = "tasks"
ARCHIVE_REL = "archive"


@dataclass
class MergeResult:
    ok: bool = True
    steps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    conflicted: bool = False

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


def _hook_env(name: str, branch: str, worktree: Path, base: str) -> dict[str, str]:
    return {
        "TASK_NAME": name,
        "BRANCH_NAME": branch,
        "WORKTREE_PATH": str(worktree),
        "WORKTREE_NAME": worktree.name,
        "BASE_BRANCH": base,
    }


def _kill_dispatch(worktree: Path) -> None:
    pid = state.dispatch_pid(str(worktree))
    if pid is None:
        return
    ticks = server.proc_start_ticks(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if state.dispatch_pid(str(worktree)) is None:
            return
        time.sleep(0.2)
    # Only escalate if it really is still the same process; the pid may have
    # been recycled in the meantime.
    if ticks is None or server.pid_alive(pid, ticks):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _archive(common: Path, base_wt: Path, name: str, base: str) -> list[str]:
    steps: list[str] = []
    src = Path(TASKS_REL) / name / "task.md"
    if not (base_wt / src).exists():
        steps.append(f"nothing to archive: {src} is not on {base}")
        return steps
    dest = Path(ARCHIVE_REL) / name / "task.md"
    (base_wt / dest).parent.mkdir(parents=True, exist_ok=True)
    code, out = _git(base_wt, "mv", str(src), str(dest))
    if code != 0:
        steps.append(f"archive mv failed: {out[:200]}")
        return steps
    code, out = _git(base_wt, "commit", "-q",
                     "-m", f"orch: archive {name}")
    if code != 0:
        steps.append(f"archive commit failed: {out[:200]}")
        return steps
    empty = base_wt / Path(TASKS_REL) / name
    try:
        empty.rmdir()
    except OSError:
        pass
    steps.append(f"archived {TASKS_REL}/{name} -> {ARCHIVE_REL}/{name}")
    return steps


def _remove_worktree_and_branch(
    common: Path, base_wt: Path, name: str, branch: str, worktree: Path,
    base: str, r: MergeResult, force_delete: bool = False,
) -> None:
    _kill_dispatch(worktree)
    env = _hook_env(name, branch, worktree, base)

    ok, msg = dispatch.run_hook(worktree, "pre-task-cleanup", env)
    r.step(msg)
    if not ok:
        r.fail("pre-task-cleanup blocked the cleanup; worktree survives")
        return

    code, out = _git(common, "worktree", "remove", str(worktree))
    if code != 0:
        code, out = _git(common, "worktree", "remove", "--force", str(worktree))
    if code != 0:
        r.fail(f"could not remove worktree {worktree}: {out[:300]}")
        return
    r.step(f"removed worktree {worktree}")

    env["WORKTREE_PATH"] = str(worktree)
    ok, msg = dispatch.run_hook(base_wt, "pre-branch-delete", env)
    r.step(msg)
    if not ok:
        r.fail(f"pre-branch-delete blocked; branch {branch} survives")
        return

    flag = "-D" if force_delete else "-d"
    code, out = _git(base_wt, "branch", flag, branch)
    if code != 0:
        r.fail(f"could not delete branch {branch}: {out[:300]}")
        return
    r.step(f"deleted branch {branch}")

    _git(common, "worktree", "prune")
    dispatch.run_hook(base_wt, "post-task-cleanup", env)


def merge_and_cleanup(
    common: Path,
    name: str,
    branch: str,
    worktree: Path,
    base: str = "main",
) -> MergeResult:
    r = MergeResult()
    wt = Path(worktree)
    base_wt = state._base_worktree(common, base)
    if base_wt is None:
        r.fail(f"no worktree on {base} found in {common}; cannot merge without one")
        return r
    r.step(f"base worktree: {base_wt} on {base}")

    if state._base_merging(common, base):
        r.fail("the base worktree is mid-merge (a conflict awaits resolution); "
               "finish or abort that merge first")
        return r

    if not wt.exists():
        r.fail(f"worktree {wt} no longer exists")
        return r
    if state._dirty(wt):
        r.fail(f"worktree {wt} has uncommitted work; only a committed, diffing "
               f"branch is merged. The verification hook must have passed and committed.")
        return r

    code, out = _git(base_wt, "merge", "--no-edit", "--no-ff", branch)
    if code != 0:
        _rc, head = _git(base_wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if _rc == 0 and head:
            r.conflicted = True
            r.fail(
                "the merge into base conflicts. It is left in progress so the task "
                "shows needs-review (merge-conflict); resolve it in the base "
                "worktree (fix the files, git add, git commit). Once the merge "
                "lands, the daemon archives the task."
            )
        else:
            _git(base_wt, "merge", "--abort")
            r.fail(f"merge failed: {out[-1500:]}")
            r.fail(f"the branch {branch} was NOT merged and the worktree survives. "
                   f"Resolve the conflict, then `orch task merge {name}`.")
        return r
    r.step(f"merged {branch} into {base}")

    _remove_worktree_and_branch(common, base_wt, name, branch, wt, base, r)
    if not r.ok:
        return r

    for s in _archive(common, base_wt, name, base):
        r.step(s)
    r.step(f"{name} -> merged and archived")
    return r


def reset_task(
    common: Path,
    name: str,
    branch: str,
    worktree: Path,
    base: str = "main",
) -> MergeResult:
    """Return a claimed task to `todo`: delete the worktree and branch, keep
    the spec in tasks/. A deliberate human action - the branch may hold work."""
    r = MergeResult()
    wt = Path(worktree)
    base_wt = state._base_worktree(common, base)
    if base_wt is None:
        r.fail(f"no worktree on {base} found in {common}; cannot reset without one")
        return r
    _kill_dispatch(wt)
    code, out = _git(common, "worktree", "remove", "--force", str(wt))
    if code != 0:
        r.fail(f"could not remove worktree {wt}: {out[:300]}")
        return r
    r.step(f"removed worktree {wt}")
    code, out = _git(base_wt, "branch", "-D", branch)
    if code != 0:
        r.fail(f"could not delete branch {branch}: {out[:300]}")
        return r
    r.step(f"deleted branch {branch}")
    _git(common, "worktree", "prune")
    return r


def remove_task(
    common: Path,
    name: str,
    branch: str,
    worktree: Path,
    base: str = "main",
) -> MergeResult:
    r = MergeResult()
    wt = Path(worktree)
    base_wt = state._base_worktree(common, base)
    if base_wt is None:
        r.fail(f"no worktree on {base} found in {common}; cannot clean up without one")
        return r
    r.step(f"base worktree: {base_wt} on {base}")

    if state._base_merging(common, base):
        r.fail("the base worktree is mid-merge (a conflict awaits resolution); "
               "finish or abort that merge first")
        return r

    _remove_worktree_and_branch(common, base_wt, name, branch, wt, base, r,
                                force_delete=True)
    if not r.ok:
        return r

    for s in _archive(common, base_wt, name, base):
        r.step(s)
    r.step(f"{name} -> removed (nothing to merge) and archived")
    return r
