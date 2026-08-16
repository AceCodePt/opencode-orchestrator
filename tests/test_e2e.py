from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import daemon, dispatch, server, state  # noqa: E402

PASSING_HOOK = """#!/usr/bin/env bash
set -euo pipefail
cd "${WORKTREE_PATH:-$(git rev-parse --show-toplevel)}" || exit 1
# the project's real check: the agent's committed answer must be exactly 42
grep -q "^answer: 42$" answer.txt
# on a pass, commit anything the agent left uncommitted (idempotent)
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.name=orch -c user.email=orch@localhost \\
      commit -q -m "orch: session-idle verification passed"
fi
exit 0
"""


def _have_opencode() -> bool:
    return shutil.which("opencode") is not None


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _project(tmp: Path) -> Path:
    root = tmp / "roots" / "proj"
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(root))
    _git("-C", str(root), "config", "user.email", "t@t")
    _git("-C", str(root), "config", "user.name", "t")
    h = root / ".orchestration" / "hooks"
    h.mkdir(parents=True)
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "main", "verify": {"retryCap": 3}}\n')
    (root / "seed").write_text("x")
    _git("-C", str(root), "add", "-A")
    _git("-C", str(root), "commit", "-q", "-m", "seed")
    d = root / "tasks" / "done-commit"
    d.mkdir(parents=True)
    (d / "task.md").write_text(
        "# Done by commit\n\n## Verification\n\n`answer.txt` contains `answer: 42`.\n")
    _git("-C", str(root), "add", "-A")
    _git("-C", str(root), "commit", "-q", "-m", "task")
    return root


def _install_hook(root: Path, body: str) -> None:
    p = root / ".orchestration" / "hooks" / "session-idle"
    p.write_text(body)
    p.chmod(0o755)
    _git("-C", str(root), "add", "-A")
    _git("-C", str(root), "commit", "-q", "-m", "hook")


def _claim_and_commit(root: Path, name: str, files: dict[str, str]) -> Path:
    """The agent's act under the new definition: configure its identity, write
    the work, and commit it to the branch. A plain `git commit` relying on the
    per-worktree identity dispatch would have set up."""
    wt = root.parent / name
    _git("-C", str(root), "worktree", "add", "-b", name, str(wt), "main")
    dispatch.configure_identity(wt)
    for rel, content in files.items():
        (wt / rel).write_text(content)
    _git("-C", str(wt), "add", "-A")
    _git("-C", str(wt), "commit", "-q", "-m", f"done: {name}")
    return wt


def _run_step(root: Path, name: str) -> list[str]:
    _top, common, _err = dispatch.project_of(root)
    assert common is not None, _err
    sup = daemon.Supervisor(project=str(common), roots=[str(root.parents[0])])
    # the session settled a while ago: skip the ~30s settle budget in one pass
    sup.mem.settled_at[(str(common), name)] = int(time.time()) - 31
    return sup.step()


def _restore(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_an_agent_commit_is_the_done_signal_and_the_loop_merges_it() -> None:
    if not _have_opencode():
        print("skip: opencode not on PATH (no live server)")
        return
    tmp = tempfile.TemporaryDirectory()
    saved = {k: os.environ.get(k) for k in ("XDG_STATE_HOME",)}
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        t = Path(tmp.name)
        root = _project(t)
        _install_hook(root, PASSING_HOOK)
        _top, common, _err = dispatch.project_of(root)
        assert common is not None, _err
        state.trust_project(common)
        wt = _claim_and_commit(root, "done-commit", {"answer.txt": "answer: 42"})
        assert _git("-C", str(wt), "config", "user.name").stdout.strip() == "orch", \
            "dispatch's per-worktree identity must let the agent commit"
        # the agent committed, so the branch is clean + diffing vs base
        assert not state._dirty(wt)
        assert state._diff(common, "main", "done-commit")

        srv = server.start()
        try:
            log = _run_step(root, "done-commit")
            joined = "\n".join(log)
        finally:
            server.stop()

        assert "verify passed" in joined, joined
        assert "merged" in joined, joined
        assert "archived" in joined, joined
        # the agent's own commit, not a hook commit, is what the loop consumed
        assert (root / "answer.txt").read_text() == "answer: 42"
        assert not wt.exists(), "the worktree must be torn down"
        assert _git("-C", str(root), "show-ref", "--verify", "-q",
                    "refs/heads/done-commit").returncode != 0, \
            "the task branch must be deleted after merge"
        archived = _git("-C", str(root), "ls-tree", "-r", "--name-only",
                        "main", "--", "archive").stdout
        assert "archive/done-commit/task.md" in archived, archived
    finally:
        _restore(saved)
        tmp.cleanup()
    print("ok: an agent commit is the done signal; the daemon verifies, merges, archives")


def test_a_bad_commit_lands_on_needs_review_and_preserves_the_work() -> None:
    if not _have_opencode():
        print("skip: opencode not on PATH (no live server)")
        return
    tmp = tempfile.TemporaryDirectory()
    saved = {k: os.environ.get(k) for k in ("XDG_STATE_HOME",)}
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        t = Path(tmp.name)
        root = _project(t)
        _install_hook(root, PASSING_HOOK)
        _top, common, _err = dispatch.project_of(root)
        assert common is not None, _err
        state.trust_project(common)
        wt = _claim_and_commit(root, "done-commit", {"answer.txt": "answer: 99"})

        srv = server.start()
        try:
            log = _run_step(root, "done-commit")
            joined = "\n".join(log)
        finally:
            server.stop()

        assert "needs-review" in joined, joined
        assert "merged" not in joined, joined
        # the committed work is preserved, unmerged, for a human to review
        assert wt.exists(), "the worktree must survive a failed verification"
        assert (wt / "answer.txt").read_text() == "answer: 99"
        assert _git("-C", str(root), "show-ref", "--verify", "-q",
                    "refs/heads/done-commit").returncode == 0, \
            "the branch must survive a failed verification"
    finally:
        _restore(saved)
        tmp.cleanup()
    print("ok: a bad agent commit is caught by the hook and lands on needs-review")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} e2e tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
