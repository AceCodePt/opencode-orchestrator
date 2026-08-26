from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import state  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "seed").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    orch = root / ".orchestration"
    orch.mkdir(parents=True)
    (orch / "config.json").write_text('{"baseBranch": "main"}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "orchestration")
    return root, tmp


def _task(root: Path, name: str) -> None:
    d = root / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(f"# {name}\n\nDo it.\n")
    _git(root, "add", str(d))
    _git(root, "commit", "-q", "-m", f"task {name}")


def _claim(root: Path, name: str) -> Path:
    wt = root.parent / name
    _git(root, "worktree", "add", "-b", name, str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    return wt


def _proj(root: Path) -> state.Project:
    proj = state.Project.from_dir(root)
    assert proj is not None
    return proj


def test_scan_lists_declared_tasks_as_todo() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    _task(root, "b-task")
    proj = _proj(root)
    tasks = proj.scan()
    by = {t.name: t for t in tasks}
    assert set(by) == {"a-task", "b-task"}
    assert by["a-task"].declared and not by["a-task"].claimed
    assert by["a-task"].status == state.TODO
    assert by["a-task"].base == "main"
    tmp.cleanup()
    print("ok: declared tasks scan as todo")


def test_wait_flags_default_false_and_come_from_the_committed_front_matter() -> None:
    root, tmp = _repo()
    (root / "tasks" / "manual").mkdir(parents=True)
    (root / "tasks" / "manual" / "task.md").write_text(
        "---\nwait_human_start: true\n---\n\n# Manual\n\nDo it.\n")
    (root / "tasks" / "auto").mkdir(parents=True)
    (root / "tasks" / "auto" / "task.md").write_text("# Auto\n\nDo it.\n")
    (root / "tasks" / "waiter").mkdir(parents=True)
    (root / "tasks" / "waiter" / "task.md").write_text(
        "---\nwait_human_merge: true\n---\n\n# Waiter\n\nDo it.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tasks")
    by = {t.name: t for t in _proj(root).scan()}
    assert by["manual"].wait_human_start, "wait_human_start: true opts the task out of the scheduler"
    assert not by["auto"].wait_human_start, "no front matter means the machine may pick it"
    assert by["waiter"].wait_human_merge, "wait_human_merge: true awaits a human merge"
    assert not by["auto"].wait_human_merge, "wait_human_merge defaults false"
    (root / "tasks" / "waiter" / "task.md").write_text("# Waiter\n\nDo it.\n")
    assert {t.name: t for t in _proj(root).scan()}["waiter"].wait_human_merge, \
        "an uncommitted working-tree edit must not count - the committed copy rules"
    tmp.cleanup()
    print("ok: wait flags default false and are read from the committed task.md "
          "front matter, never the working tree")


def test_dependencies_default_to_empty_and_parse_from_the_committed_front_matter() -> None:
    root, tmp = _repo()
    (root / "tasks" / "plain").mkdir(parents=True)
    (root / "tasks" / "plain" / "task.md").write_text("# Plain\n\nDo it.\n")
    (root / "tasks" / "linked").mkdir(parents=True)
    (root / "tasks" / "linked" / "task.md").write_text(
        "---\nwait_human_start: true\ndependencies: [spec, auth-migrate]\n---\n\n# Linked\n\nDo it.\n")
    (root / "tasks" / "csv").mkdir(parents=True)
    (root / "tasks" / "csv" / "task.md").write_text(
        "---\nwait_human_start: true\ndependencies: spec, auth-migrate\n---\n\n# Csv\n\nDo it.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tasks")
    by = {t.name: t for t in _proj(root).scan()}
    assert by["plain"].dependencies == []
    assert by["linked"].dependencies == ["spec", "auth-migrate"]
    assert by["csv"].dependencies == ["spec", "auth-migrate"]
    (root / "tasks" / "linked" / "task.md").write_text(
        "---\nwait_human_start: true\ndependencies: [tampered]\n---\n\n# Linked\n\nDo it.\n")
    assert {t.name: t for t in _proj(root).scan()}["linked"].dependencies == \
        ["spec", "auth-migrate"], \
        "an uncommitted working-tree edit must not count - the committed copy rules"
    tmp.cleanup()
    print("ok: dependencies default to empty and parse from the committed front matter")


def test_scan_sees_a_claim_and_worktree() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.claimed and t.branch == "a-task"
    assert t.worktree == str(wt)
    assert t.diff is False and t.dirty is False
    tmp.cleanup()
    print("ok: a branch is a claim, and its worktree is found")


def test_scan_reports_dirty_and_diff() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    (wt / "work.txt").write_text("uncommitted")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.dirty, "uncommitted work must read as dirty"

    (wt / "work.txt").write_text("committed")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "work")
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert not t.dirty and t.diff, "a committed branch differs from base"
    assert t.status == state.MERGE_READY
    tmp.cleanup()
    print("ok: dirty and diff derive from the worktree")


def test_scan_clean_no_diff_is_a_noop() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    _claim(root, "a-task")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert not t.dirty and not t.diff
    assert t.status == state.NOOP
    tmp.cleanup()
    print("ok: a clean branch identical to base is a no-op")


def test_interrupted_status_depends_on_the_work() -> None:
    wt = tempfile.mkdtemp(prefix="orch-status-")
    try:
        held = state.TaskState(project="/p", name="x", base="main", claimed=True,
                               branch="x", worktree=wt, session=state.SESSION_IDLE,
                               interrupted=True, dirty=False, diff=True)
        assert held.status == state.NEEDS_REVIEW
        noop = state.TaskState(project="/p", name="x", base="main", claimed=True,
                               branch="x", worktree=wt, session=state.SESSION_IDLE,
                               interrupted=True, dirty=False, diff=False)
        assert noop.status == state.NOOP
    finally:
        os.rmdir(wt)
    print("ok: interrupted work is held for review, an interrupted no-op still closes")


def test_scan_marks_a_missing_worktree() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(wt)],
                   capture_output=True, check=True)
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.claimed and t.worktree is None
    assert t.status == state.NEEDS_REVIEW
    tmp.cleanup()
    print("ok: a claimed task with a missing worktree needs review")


def test_archived_tasks_are_listed_as_archived() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    (root / "archive" / "a-task").mkdir(parents=True)
    _git(root, "mv", "tasks/a-task/task.md", "archive/a-task/task.md")
    _git(root, "commit", "-q", "-m", "archive")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.archived and t.status == state.ARCHIVED
    tmp.cleanup()
    print("ok: an archived task is not todo")


def test_timestamped_archive_generation_scans_as_archived() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    (root / "archive" / "a-task").mkdir(parents=True)
    _git(root, "mv", "tasks/a-task/task.md", "archive/a-task/task.md")
    _git(root, "commit", "-q", "-m", "archive")
    _task(root, "a-task")
    ts_dir = root / "archive" / "a-task" / "20260826-090412"
    ts_dir.mkdir(parents=True)
    _git(root, "mv", "tasks/a-task/task.md", f"archive/a-task/20260826-090412/task.md")
    _git(root, "commit", "-q", "-m", "archive again")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.archived and t.status == state.ARCHIVED
    tmp.cleanup()
    print("ok: a timestamped archive generation still scans as archived")


def test_reused_slug_with_old_archive_reads_as_declared() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    (root / "archive" / "a-task").mkdir(parents=True)
    _git(root, "mv", "tasks/a-task/task.md", "archive/a-task/task.md")
    _git(root, "commit", "-q", "-m", "archive")
    _task(root, "a-task")
    proj = _proj(root)
    t = next(x for x in proj.scan() if x.name == "a-task")
    assert t.declared and not t.archived, \
        "a re-created slug is the live task, not ARCHIVED history"
    assert t.status == state.TODO
    tmp.cleanup()
    print("ok: a reused slug with an old archive reads as the live declared task")


def test_discover_finds_orchestration_dirs() -> None:
    root, tmp = _repo()
    found = state.discover_projects([str(Path(tmp.name))])
    assert str(root) in " ".join(str(v) for v in found.values())
    tmp.cleanup()
    print("ok: discovery finds a project by its .orchestration dir")


def test_discover_ignores_orchestration_like_folder_names() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        root = Path(tmp.name)
        real = root / "real"
        real.mkdir(parents=True)
        _git(real, "init", "-q", "-b", "main")
        _git(real, "config", "user.email", "t@t")
        _git(real, "config", "user.name", "t")
        (real / "seed").write_text("x")
        _git(real, "add", "-A")
        _git(real, "commit", "-q", "-m", "seed")
        orch = real / ".orchestration"
        orch.mkdir()
        (orch / "config.json").write_text('{"baseBranch": "main"}\n')
        _git(real, "add", "-A")
        _git(real, "commit", "-q", "-m", "orch")

        fake = root / "fake"
        fake.mkdir(parents=True)
        _git(fake, "init", "-q", "-b", "main")
        _git(fake, "config", "user.email", "t@t")
        _git(fake, "config", "user.name", "t")
        (fake / "seed").write_text("x")
        _git(fake, "add", "-A")
        _git(fake, "commit", "-q", "-m", "seed")
        task = fake / "tasks" / "property-enrichment-orchestration"
        task.mkdir(parents=True)
        (task / "task.md").write_text("# x\n")

        found = state.discover_projects([str(root)])
        assert len(found) == 1, \
            f"a folder ending in '-orchestration' is not .orchestration: {found}"
        assert str(real) in str(list(found.values())[0])
    finally:
        tmp.cleanup()
    print("ok: discovery matches a literal .orchestration dir, not '-orchestration'")


def test_fingerprint_tracks_worktree_content() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    fp1 = state.fingerprint(wt)
    (wt / "x").write_text("1")
    fp2 = state.fingerprint(wt)
    assert fp1 != fp2
    (wt / "x").write_text("1")
    assert state.fingerprint(wt) == fp2
    tmp.cleanup()
    print("ok: the fingerprint reflects the worktree state")


def test_fingerprint_tracks_untracked_file_content() -> None:
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    (wt / "out.txt").write_text("forty two")
    fp1 = state.fingerprint(wt)
    (wt / "out.txt").write_text("42")
    fp2 = state.fingerprint(wt)
    assert fp1 != fp2, \
        "an agent fixing an untracked deliverable must register as a change, " \
        "or the retry loop mistakes the fix for a stall"
    assert state.fingerprint(wt) == fp2, "the fingerprint must be deterministic"
    tmp.cleanup()
    print("ok: editing an untracked file's content changes the fingerprint")


def test_trust_round_trip_keyed_by_the_repo() -> None:
    saved = os.environ.get("XDG_STATE_HOME")
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_STATE_HOME"] = tmp.name
    try:
        root, repo_tmp = _repo()
        common = Path(_git(root, "rev-parse", "--path-format=absolute",
                           "--git-common-dir"))
        assert not state.is_trusted(common)
        state.trust_project(common)
        assert state.is_trusted(common)
        assert str(common) in state.trusted_projects()
        state.untrust_project(common)
        assert not state.is_trusted(common)
        repo_tmp.cleanup()
    finally:
        tmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = saved
    print("ok: trust is a machine-level marker keyed by the repo's git dir")


def test_base_branch_comes_from_config() -> None:
    root, tmp = _repo()
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "preview"}\n')
    proj = _proj(root)
    assert proj.base == "preview"
    tmp.cleanup()
    print("ok: base branch read from the project config")


def test_default_roots_include_home_and_mnt() -> None:
    saved = os.environ.get("ORCH_ROOTS")
    os.environ.pop("ORCH_ROOTS", None)
    try:
        roots = state.default_roots()
        assert str(Path.home()) in roots and "/mnt" in roots
        assert state.DEV_ROOT in roots, \
            "orch's own dev/e2e root must be supervised by default"
    finally:
        if saved:
            os.environ["ORCH_ROOTS"] = saved
        else:
            os.environ.pop("ORCH_ROOTS", None)
    print("ok: default roots are $HOME, /mnt and the dev root")


def test_detached_detects_orch_spawned_processes() -> None:
    p = subprocess.Popen(["sleep", "30"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    time.sleep(0.2)
    try:
        assert state._detached(p.pid), "orch starts agents detached (no tty)"
    finally:
        p.kill()
    print("ok: a detached process (orch-spawned) is detected")


def test_detached_excludes_a_pty_process() -> None:
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("sleep", ["sleep", "30"])
    time.sleep(0.2)
    try:
        assert not state._detached(pid), "a human's session has a controlling tty"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        os.close(fd)
    print("ok: a process with a controlling tty is not orchestrated")


def test_scan_marks_a_mid_merge_conflict_as_needs_review() -> None:
    from orch import merge as merge_mod
    root, tmp = _repo()
    _task(root, "a-task")
    wt = _claim(root, "a-task")
    (wt / "task.py").write_text("x = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "work")
    (root / "task.py").write_text("x = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "conflict")
    r = merge_mod.merge_and_cleanup(root, "a-task", "a-task", wt, "main")
    assert r.conflicted, "the merge must be left in progress"
    t = next(x for x in _proj(root).scan() if x.name == "a-task")
    assert t.conflicted, "the scan must derive the conflict from MERGE_HEAD"
    assert t.status == state.NEEDS_REVIEW, t.status
    assert any("merge-conflict" in n for n in t.notes), t.notes
    tmp.cleanup()
    print("ok: a mid-merge conflict is derived by the scan as needs-review")


def test_committed_config_wins_over_a_diverged_working_copy() -> None:
    root, tmp = _repo()
    cfg = (root / ".orchestration" / "config.json")
    cfg.write_text(json.dumps({
        "baseBranch": "main",
        "scheduler": {"enabled": True, "maxConcurrent": 1},
    }) + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "scheduler")
    cfg.write_text(json.dumps({
        "baseBranch": "main",
        "scheduler": {"enabled": True, "maxConcurrent": 99},
    }) + "\n")
    got = state.committed_config(Path(_git(root, "rev-parse",
                                            "--path-format=absolute",
                                            "--git-common-dir")), "main")
    pol = state.scheduler_from_config(got)
    assert pol.enabled and pol.max_concurrent == 1, \
        "the committed copy is the only authority, not the working tree"
    common = Path(_git(root, "rev-parse", "--path-format=absolute",
                       "--git-common-dir"))
    assert common.exists()
    tmp.cleanup()
    print("ok: a diverged working config.json does not change the scheduler")


def test_write_scheduler_policy_preserves_keys_on_base() -> None:
    root, tmp = _repo()
    cfg = root / ".orchestration" / "config.json"
    cfg.write_text(json.dumps({
        "baseBranch": "main",
        "verify": {"retryCap": 5},
        "custom": {"keep": True},
    }) + "\n")
    common = Path(_git(root, "rev-parse", "--path-format=absolute",
                       "--git-common-dir"))
    ok, msg = state.write_scheduler_policy(common, "main", True, 3)
    assert ok, msg
    cfg = json.loads((root / ".orchestration" / "config.json").read_text())
    assert cfg["scheduler"] == {"enabled": True, "maxConcurrent": 3}
    assert cfg["verify"] == {"retryCap": 5}, "unrelated keys survive"
    assert cfg["custom"] == {"keep": True}
    ok, _ = state.write_scheduler_policy(common, "main", False, None)
    assert ok
    cfg = json.loads((root / ".orchestration" / "config.json").read_text())
    assert cfg["scheduler"]["enabled"] is False
    tmp.cleanup()
    print("ok: write_scheduler_policy edits only the scheduler block on base")


def test_retry_cap_comes_from_the_committed_config() -> None:
    root, tmp = _repo()
    cfg = root / ".orchestration" / "config.json"
    cfg.write_text(json.dumps({
        "baseBranch": "main",
        "verify": {"retryCap": 7},
    }) + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "retrycap")
    cfg.write_text(json.dumps({
        "baseBranch": "main",
        "verify": {"retryCap": 1},
    }) + "\n")
    _task(root, "auto")
    wt = _claim(root, "auto")
    (wt / "work.txt").write_text("done")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "claim")
    t = next(x for x in _proj(root).scan() if x.name == "auto")
    assert t.retry_cap == 7, t.retry_cap
    tmp.cleanup()
    print("ok: retryCap is read from the committed copy, not the working tree")


def test_machine_budget_roundtrip() -> None:
    saved = os.environ.get("XDG_CONFIG_HOME")
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    try:
        assert state.machine_budget() is None, "absent means unlimited"
        state.set_machine_budget(4)
        assert state.machine_budget() == 4
        state.set_machine_budget(None)
        assert state.machine_budget() is None
        raw = json.loads(state.machine_config_path().read_text())
        assert "scheduler" not in raw, "off removes the cap entirely"
    finally:
        tmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved
    print("ok: the machine-wide budget round-trips through "
          "~/.config/orch/daemon.json")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} state tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
