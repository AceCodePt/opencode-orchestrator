from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import merge  # noqa: E402
from orch.store import DONE, MERGED, RUNNING, Store  # noqa: E402


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def _bare_repo(tmp: tempfile.TemporaryDirectory) -> Path:
    seed = Path(tmp.name) / "seed"
    _git(Path(tmp.name), "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README").write_text("hello")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")

    bare = Path(tmp.name) / "lab.git"
    _git(Path(tmp.name), "clone", "--bare", str(seed), str(bare))

    main_wt = Path(tmp.name) / "main"
    _git(bare, "worktree", "add", str(main_wt), "main")
    _git(main_wt, "config", "user.email", "t@t")
    _git(main_wt, "config", "user.name", "t")
    (main_wt / ".orchestration" / "hooks").mkdir(parents=True)
    (main_wt / ".orchestration" / "config.json").write_text("{}")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "orchestration setup")
    return bare


def _done_task(bare: Path, store: Store, name: str) -> Path:
    main_wt = bare.parent / "main"
    wt = bare.parent / name
    _git(bare, "worktree", "add", "-b", name, str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    (wt / "task.py").write_text(f"# {name}")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", f"work for {name}")
    store.sync_intent(str(bare), [name])
    store.claim(str(bare), name)
    store.record_launch(str(bare), name, worktree=str(wt), branch=name,
                        port=4096, pid=0, parent_session="ses_1")
    store.finish(str(bare), name, DONE, "gate passed")
    return wt


def test_merge_done_task_folds_branch_and_cleans_up() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    wt = _done_task(bare, st, "task-a")

    r = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert r.ok, r.problems
    assert "merged" in r.steps[-1]

    main_wt = bare.parent / "main"
    log = _git(main_wt, "log", "--oneline")
    assert "work for task-a" in log

    assert not wt.exists(), "worktree should be gone"
    wts = _git(bare, "worktree", "list")
    assert "task-a" not in wts

    branches = _git(main_wt, "branch", "--list")
    assert "task-a" not in branches

    t = st.get(str(bare), "task-a")
    assert t.status == MERGED
    print("ok: done task merged, worktree + branch cleaned up")


def test_merge_non_done_task_refused() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    st.sync_intent(str(bare), ["running-task"])
    st.claim(str(bare), "running-task")
    st.record_launch(str(bare), "running-task", worktree="/tmp/x",
                     branch="running-task", port=4096, pid=0,
                     parent_session="ses_1")

    r = merge.merge_and_cleanup(st, str(bare), "running-task")
    assert not r.ok
    assert "not done" in r.problems[0] or "not done or needs-review" in r.problems[0]
    print("ok: non-done task refused for merge")


def test_merge_nonexistent_task() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    r = merge.merge_and_cleanup(st, str(bare), "ghost")
    assert not r.ok
    print("ok: nonexistent task fails merge")


def test_merge_idempotent() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    _done_task(bare, st, "task-a")

    r1 = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert r1.ok
    r2 = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert r2.ok
    assert "already merged" in r2.steps[0]
    print("ok: merging an already-merged task is a no-op")


def test_merge_conflict_keeps_worktree_and_branch() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    wt = _done_task(bare, st, "task-a")

    main_wt = bare.parent / "main"
    (main_wt / "task.py").write_text("# conflicting")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "conflicting change on main")

    r = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert not r.ok
    assert any("merge failed" in p for p in r.problems)
    assert wt.exists(), "worktree should survive a failed merge"

    log = _git(main_wt, "log", "--oneline")
    assert "work for task-a" not in log, "the branch should not have merged"

    t = st.get(str(bare), "task-a")
    assert t.status == "needs-review", (
        "a merge conflict should move the task to needs-review so the daemon stops "
        "retrying; a human resolves and runs `orch task merge`")
    print("ok: merge conflict keeps worktree + branch, task moves to needs-review")


def test_manual_merge_after_resolving_conflict() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    wt = _done_task(bare, st, "task-a")

    main_wt = bare.parent / "main"
    (main_wt / "task.py").write_text("# conflicting")
    _git(main_wt, "add", "-A")
    _git(main_wt, "commit", "-q", "-m", "conflicting change on main")

    r1 = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert not r1.ok

    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"],
                       capture_output=True, check=True)
    subprocess.run(["git", "-C", str(wt), "rebase", "main"],
                       capture_output=True, timeout=10)
    (wt / "task.py").write_text("# resolved task-a")
    _git(wt, "add", "-A")
    env = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
    subprocess.run(["git", "-C", str(wt), "rebase", "--continue"],
                       capture_output=True, timeout=10, env=env)
    r2 = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert r2.ok, r2.problems
    assert "merged" in r2.steps[-1]

    t = st.get(str(bare), "task-a")
    assert t.status == MERGED
    print("ok: manual merge after resolving conflict works from needs-review")


def test_pre_task_cleanup_hook_blocks() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    wt = _done_task(bare, st, "task-a")

    hook_dir = wt / ".orchestration" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook = hook_dir / "pre-task-cleanup"
    hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    hook.chmod(0o755)

    r = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert not r.ok
    assert any("blocked" in p for p in r.problems)
    assert wt.exists(), "worktree should survive a blocked cleanup"

    main_wt = bare.parent / "main"
    log = _git(main_wt, "log", "--oneline")
    assert "work for task-a" in log, "merge should have landed before the hook"
    print("ok: pre-task-cleanup hook blocks worktree removal, merge survives")


def test_pre_branch_delete_hook_blocks() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    wt = _done_task(bare, st, "task-a")

    main_wt = bare.parent / "main"
    hook = main_wt / ".orchestration" / "hooks" / "pre-branch-delete"
    hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    hook.chmod(0o755)

    r = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert not r.ok
    assert any("branch" in p.lower() for p in r.problems)
    assert not wt.exists(), "worktree was removed before the branch hook ran"

    branches = _git(main_wt, "branch", "--list")
    assert "task-a" in branches, "branch should survive a blocked delete"
    print("ok: pre-branch-delete hook blocks branch deletion, worktree still removed")


def test_post_task_cleanup_hook_runs() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    _done_task(bare, st, "task-a")

    main_wt = bare.parent / "main"
    marker = main_wt / ".orchestration" / "hooks" / "post-task-cleanup"
    marker.write_text("#!/usr/bin/env bash\necho cleanup-done > " + str(main_wt / "marker.txt"))
    marker.chmod(0o755)

    r = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert r.ok, r.problems
    assert (main_wt / "marker.txt").read_text().strip() == "cleanup-done"
    print("ok: post-task-cleanup hook runs after merge + cleanup")


def test_decide_emits_merge_for_done_tasks() -> None:
    from orch import daemon
    from orch.store import Task
    done_task = Task(id=1, project="/tmp/lab.git", name="task-a", status=DONE,
                    lease_until=None, worktree="/tmp/lab/task-a", port=None,
                    pid=None, retries=0, branch="task-a", last_outcome="passed")
    snap = daemon.Snapshot(now=0, server_running=True, server_unresponsive=False,
                          tasks=[], done_tasks=[done_task])
    acts = daemon.decide(snap)
    merges = [a for a in acts if a.kind == daemon.MERGE]
    assert len(merges) == 1
    assert merges[0].task == "task-a"
    print("ok: decide emits MERGE for done tasks")


def test_merge_commits_uncommitted_work_before_merging() -> None:
    tmp = tempfile.TemporaryDirectory()
    bare = _bare_repo(tmp)
    st = Store(":memory:")
    wt = _done_task(bare, st, "task-a")

    (wt / "extra.py").write_text("# extra file")
    _git(wt, "add", "-A")

    r = merge.merge_and_cleanup(st, str(bare), "task-a")
    assert r.ok, r.problems
    assert any("committed" in s for s in r.steps)

    main_wt = bare.parent / "main"
    log = _git(main_wt, "log", "--oneline")
    assert "work for task-a" in log or "committed by orch" in log
    assert (main_wt / "extra.py").exists(), "uncommitted work should have landed"
    print("ok: uncommitted work is committed before merge and lands on base")


def test_decide_does_not_merge_running_tasks() -> None:
    from orch import daemon
    from orch.store import Task
    running = Task(id=1, project="/tmp/lab.git", name="task-a", status=RUNNING,
                  lease_until=999999999, worktree="/tmp/lab/task-a", port=4096,
                  pid=1234, retries=0, branch="task-a")
    snap = daemon.Snapshot(now=0, server_running=True, server_unresponsive=False,
                          tasks=[running], done_tasks=[])
    acts = daemon.decide(snap)
    assert not any(a.kind == daemon.MERGE for a in acts)
    print("ok: decide does not emit MERGE for running tasks")


if __name__ == "__main__":
    import traceback
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception:
                print(f"FAIL: {name}")
                traceback.print_exc()
            else:
                pass
    print()
    tests = [n for n in sorted(globals()) if n.startswith("test_")]
    print(f"{len(tests)} merge tests passed")
