from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import dispatch, envfilter, verify  # noqa: E402


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "seed").write_text("x")
    _commit(root)
    return root, tmp


def _commit(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True)


def _task_file(root: Path, name: str) -> None:
    d = root / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(f"# {name}\n\nDo the thing.\n")


def test_slug() -> None:
    assert dispatch.slug("Silence Watchdog") == "silence-watchdog"
    assert dispatch.slug("  Weird__Name!! ") == "weird-name"
    assert dispatch.slug("already-fine") == "already-fine"
    print("ok: slug")


def test_outside_a_worktree_is_refused() -> None:
    tmp = tempfile.TemporaryDirectory()
    p, err = dispatch.plan(Path(tmp.name), "whatever")
    assert p is None and err and "not inside a git worktree" in err
    tmp.cleanup()
    print("ok: dispatch outside a worktree is refused")


def test_uncommitted_task_file_is_refused_with_the_reason() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    p, err = dispatch.plan(root, "probe")
    assert p is None and err
    assert "not committed" in err and "no instructions" in err, err
    tmp.cleanup()
    print("ok: an uncommitted task file is refused, with the reason")


def test_committed_task_file_plans_fine() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    p, err = dispatch.plan(root, "probe")
    assert err is None and p is not None
    assert p.task == "probe" and p.branch == "probe" and p.base == "main"
    assert "tasks/probe/task.md" in p.prompt
    tmp.cleanup()
    print("ok: a committed task file plans cleanly")


def test_missing_task_file_needs_an_explicit_prompt() -> None:
    root, tmp = _repo()
    p, err = dispatch.plan(root, "ad-hoc")
    assert p is None and err and "no --prompt" in err
    p, err = dispatch.plan(root, "ad-hoc", prompt="just do this")
    assert err is None and p is not None and p.prompt == "just do this"
    tmp.cleanup()
    print("ok: ad-hoc dispatch requires an explicit prompt")


def test_start_creates_a_session_via_the_server_api() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)

    orig_start = dispatch.server.start
    orig_create = dispatch.harness.create_session
    orig_prompt = dispatch.harness.prompt_async
    calls: list = []

    class _Srv:
        pid = 1234
        port = 4099
        url = "http://127.0.0.1:4099"

    dispatch.server.start = lambda: _Srv()

    def create_session(port, directory, title=None):
        calls.append(("create", port, directory, title))
        return {"id": "ses_abc"}

    def prompt_async(port, sid, text, agent=None, model=None):
        calls.append(("prompt", port, sid, text, agent, model))

    dispatch.harness.create_session = create_session
    dispatch.harness.prompt_async = prompt_async
    try:
        r = dispatch.start(root, "probe", model="m/x", agent="builder")
        assert r.ok, r.problems
        assert r.session_id == "ses_abc"
        assert calls[0] == ("create", 4099, str(r.worktree), "probe"), calls
        assert calls[1][0] == "prompt" and calls[1][2] == "ses_abc", calls
        assert calls[1][4] == "builder" and calls[1][5] == "m/x", calls
        assert "tasks/probe/task.md" in calls[1][3], \
            "the prompt must carry the task instructions"
        assert not any("opencode" in s and "run" in s for s in r.steps), \
            "dispatch must not spawn an `opencode run` process"
    finally:
        dispatch.server.start = orig_start
        dispatch.harness.create_session = orig_create
        dispatch.harness.prompt_async = orig_prompt
    tmp.cleanup()
    print("ok: start creates a task session via the server API, no run loop")


def test_start_prompts_for_the_done_commit() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)

    orig_start = dispatch.server.start
    orig_create = dispatch.harness.create_session
    orig_prompt = dispatch.harness.prompt_async

    class _Srv:
        pid = 1234
        port = 4099
        url = "http://127.0.0.1:4099"

    dispatch.server.start = lambda: _Srv()
    dispatch.harness.create_session = lambda port, directory, title=None: {
        "id": "ses_abc"}
    calls: list = []

    def prompt_async(port, sid, text, agent=None, model=None):
        calls.append(text)

    dispatch.harness.prompt_async = prompt_async
    try:
        r = dispatch.start(root, "probe")
        assert r.ok, r.problems
        assert calls, "the task must have been prompted"
        assert "committing your work" in calls[0], calls[0]
        assert "tasks/probe/task.md" in calls[0], calls[0]
    finally:
        dispatch.server.start = orig_start
        dispatch.harness.create_session = orig_create
        dispatch.harness.prompt_async = orig_prompt
    tmp.cleanup()
    print("ok: start prompts for the done-commit")


def test_branch_exists_detects_a_claim() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    assert not dispatch.branch_exists(Path(dispatch.project_of(root)[1] or ""), "probe")
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "probe"],
                   check=True)
    _, common, _ = dispatch.project_of(root)
    assert common and dispatch.branch_exists(common, "probe")
    tmp.cleanup()
    print("ok: an existing branch is a claim")


def test_start_refuses_when_the_branch_already_exists() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "probe"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True)
    r = dispatch.start(root, "probe", dry_run=True)
    assert not r.ok
    assert "already claimed" in " ".join(r.problems)
    tmp.cleanup()
    print("ok: a claimed branch refuses a fresh dispatch")


def test_dry_run_touches_nothing() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    r = dispatch.start(root, "probe", dry_run=True)
    assert r.ok
    assert any("would run" in s for s in r.steps)
    assert r.worktree is not None and not r.worktree.exists(), \
        "a dry run must not create the worktree"
    tmp.cleanup()
    print("ok: dry run creates nothing")


def test_dispatch_proceeds_without_a_verification_hook() -> None:
    root, tmp = _repo()
    _task_file(root, "probe")
    _commit(root)
    r = dispatch.start(root, "probe", dry_run=True)
    assert r.ok, r.problems
    assert any("accepted without verification" in s for s in r.steps), r.steps
    tmp.cleanup()
    print("ok: dispatch proceeds without a verification hook (absence is implicit)")


def test_hook_absence_is_fine_but_uncommitted_hooks_do_not_run() -> None:
    root, tmp = _repo()
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert ok and "no pre-task-create committed" in msg, msg

    h = root / verify.HOOKS_REL / dispatch.HOOK_PRE
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("#!/usr/bin/env bash\nexit 0\n")
    h.chmod(0o755)
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert ok, msg
    assert "not committed" in msg and "not run" in msg, \
        "an uncommitted working-tree hook is a no-op, not a run"

    _commit(root)
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert ok, msg

    h.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 3\n")
    _commit(root)
    ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {})
    assert not ok and "exited 3" in msg and "boom" in msg, msg
    tmp.cleanup()
    print("ok: absent hook fine, uncommitted hooks do not run, failing hooks are not")


def test_hook_gets_a_filtered_environment_by_default() -> None:
    root, tmp = _repo()
    out = Path(tmp.name) / "hook-env"
    h = root / verify.HOOKS_REL / dispatch.HOOK_PRE
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("#!/usr/bin/env bash\necho \"${GH_TOKEN:-MISSING}\" > \"$OUT\"\n")
    h.chmod(0o755)
    _commit(root)

    saved_cfg = os.environ.get("XDG_CONFIG_HOME")
    saved_token = os.environ.get("GH_TOKEN")
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    os.environ["GH_TOKEN"] = "ghp_secret"
    try:
        ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {"OUT": str(out)})
        assert ok, msg
        assert out.read_text().strip() == "MISSING", \
            "a lifecycle hook must not see the owner's secrets by default"
    finally:
        os.environ.pop("GH_TOKEN", None)
        if saved_token is not None:
            os.environ["GH_TOKEN"] = saved_token
        if saved_cfg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved_cfg
    tmp.cleanup()
    print("ok: lifecycle hooks run with a filtered environment")


def test_hook_can_opt_into_the_full_environment() -> None:
    root, tmp = _repo()
    out = Path(tmp.name) / "hook-env"
    cfg = root / ".orchestration" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"verify": {"env": "full"}}))
    h = root / verify.HOOKS_REL / dispatch.HOOK_PRE
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("#!/usr/bin/env bash\necho \"${GH_TOKEN:-MISSING}\" > \"$OUT\"\n")
    h.chmod(0o755)
    _commit(root)

    saved_cfg = os.environ.get("XDG_CONFIG_HOME")
    saved_token = os.environ.get("GH_TOKEN")
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    os.environ["GH_TOKEN"] = "ghp_secret"
    try:
        assert envfilter.verify_env_full(root) is True
        ok, msg = dispatch.run_hook(root, dispatch.HOOK_PRE, {"OUT": str(out)})
        assert ok, msg
        assert out.read_text().strip() == "ghp_secret", \
            "a hook opting into the full env keeps the secret"
    finally:
        os.environ.pop("GH_TOKEN", None)
        if saved_token is not None:
            os.environ["GH_TOKEN"] = saved_token
        if saved_cfg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved_cfg
    tmp.cleanup()
    print("ok: verify.env=full opts a lifecycle hook into the full environment")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} dispatch tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
