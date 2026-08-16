from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import daemon, dispatch, harness, observe, server, state  # noqa: E402

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
    """The agent's act under the new definition: write the work and commit it
    to the branch with the machine's git identity (like the merge-resolution
    agent does)."""
    wt = root.parent / name
    _git("-C", str(root), "worktree", "add", "-b", name, str(wt), "main")
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


def _patch_orphan_ask(sid: str, last_turn_aborted: bool) -> tuple[dict, dict]:
    """Simulate the session's message record so the given pending ask's owning
    message is aborted (orphaned) and the last assistant turn is either
    finished or aborted. A real `MessageAbortedError` needs a live model turn,
    so only the message record is faked; the server, session, git and the whole
    daemon loop are real."""
    ask = {"id": "per_fake1", "sessionID": sid, "permission": "external_directory",
           "metadata": {"command": "ls /tmp"},
           "tool": {"messageID": "m_ask", "callID": "c1"}}
    orig = {n: getattr(observe.harness, n)
            for n in ("permissions", "messages", "message_by_id")}

    def permissions(port, directory=None):
        return [ask]

    def messages(port, session_id, limit=None):
        error = {"name": "MessageAbortedError",
                 "data": {"message": "Aborted"}} if last_turn_aborted else None
        return [{"info": {"id": "m_last", "role": "assistant", "error": error}}]

    def message_by_id(port, session_id, message_id):
        return {"info": {"id": message_id, "role": "assistant",
                         "error": {"name": "MessageAbortedError"}}}

    observe.harness.permissions = permissions
    observe.harness.messages = messages
    observe.harness.message_by_id = message_by_id
    return orig, ask


def _restore_patch(orig: dict) -> None:
    for name, fn in orig.items():
        setattr(observe.harness, name, fn)


def _setup_interrupted(root: Path, name: str, answer: str,
                       last_turn_aborted: bool) -> tuple[Path, list, tuple]:
    """Shared setup for the orphaned/interrupted live-server scenarios: trust
    the project, claim + commit the work, create a real session bound to the
    worktree, simulate the aborted-message record, and start the server."""
    _top, common, _err = dispatch.project_of(root)
    assert common is not None, _err
    state.trust_project(common)
    wt = _claim_and_commit(root, name, {"answer.txt": answer})
    srv = server.start()
    sess = harness.create_session(srv.port, str(wt), title=name)
    sid = (sess or {}).get("id")
    assert sid, "the server must return a session id"
    orig_patch, _ask = _patch_orphan_ask(sid, last_turn_aborted)
    cleared: list = []
    orig_reply = daemon.harness.reply_permission
    daemon.harness.reply_permission = lambda port, rid, reply, directory=None, \
        message=None: (cleared.append((rid, reply, directory)) or {})
    return wt, cleared, (orig_patch, orig_reply)


def _teardown_interrupted(cleanups: tuple) -> None:
    orig_patch, orig_reply = cleanups
    _restore_patch(orig_patch)
    daemon.harness.reply_permission = orig_reply
    server.stop()


def test_an_orphaned_ask_never_freezes_a_task_and_is_cleared() -> None:
    if not _have_opencode():
        print("skip: opencode not on PATH (no live server)")
        return
    tmp = tempfile.TemporaryDirectory()
    saved_env = {k: os.environ.get(k) for k in ("XDG_STATE_HOME",)}
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        t = Path(tmp.name)
        root = _project(t)
        _install_hook(root, PASSING_HOOK)
        wt, cleared, cleanups = _setup_interrupted(
            root, "done-commit", "answer: 42", last_turn_aborted=False)
        try:
            log = _run_step(root, "done-commit")
            joined = "\n".join(log)
        finally:
            _teardown_interrupted(cleanups)

        # the orphaned ask was cleared (replied reject) and never blocked
        assert cleared == [("per_fake1", "reject", str(wt))], cleared
        assert "verify passed" in joined, joined
        assert "merged" in joined and "archived" in joined, joined
        assert (root / "answer.txt").read_text() == "answer: 42"
        assert not wt.exists(), "the worktree must be torn down after the merge"
        assert _git("-C", str(root), "show-ref", "--verify", "-q",
                    "refs/heads/done-commit").returncode != 0
    finally:
        _restore(saved_env)
        tmp.cleanup()
    print("ok: an orphaned ask is cleared and the committed work still merges")


def test_an_interrupted_turn_is_held_for_review_not_merged() -> None:
    if not _have_opencode():
        print("skip: opencode not on PATH (no live server)")
        return
    tmp = tempfile.TemporaryDirectory()
    saved_env = {k: os.environ.get(k) for k in ("XDG_STATE_HOME",)}
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        t = Path(tmp.name)
        root = _project(t)
        _install_hook(root, PASSING_HOOK)
        wt, cleared, cleanups = _setup_interrupted(
            root, "done-commit", "answer: 42", last_turn_aborted=True)
        try:
            log = _run_step(root, "done-commit")
            joined = "\n".join(log)
        finally:
            _teardown_interrupted(cleanups)

        # the orphaned ask is still cleared, but the interrupted work is held
        assert cleared == [("per_fake1", "reject", str(wt))], cleared
        assert "needs-review" in joined and "interrupted" in joined, joined
        assert "merged" not in joined, joined
        assert wt.exists(), "interrupted work must survive, held for review"
        assert (wt / "answer.txt").read_text() == "answer: 42"
        assert _git("-C", str(root), "show-ref", "--verify", "-q",
                    "refs/heads/done-commit").returncode == 0
    finally:
        _restore(saved_env)
        tmp.cleanup()
    print("ok: an interrupted turn is held for review, never merged")


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
