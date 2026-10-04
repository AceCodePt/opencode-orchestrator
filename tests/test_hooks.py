from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import hooks  # noqa: E402


def _env(tmp: tempfile.TemporaryDirectory) -> tuple[Path, Path]:
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    state_home = Path(tmp.name) / "state"
    os.environ["XDG_STATE_HOME"] = str(state_home)
    return Path(tmp.name), state_home


def _restore(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _write_hook(hooks_dir: Path, name: str, body: str, executable: bool = True) -> Path:
    p = hooks_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    if executable:
        p.chmod(0o755)
    return p


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_run_fires_detached_with_env() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        marker = cfg / "marker"
        _write_hook(cfg / "orch" / "hooks", "alert", (
            "#!/usr/bin/env bash\n"
            f"echo \"$ORCH_EVENT:$ORCH_ACTION:$PROJECT_NAME:$TASK_NAME\" "
            f"> {marker}\n"
        ))
        pid = hooks.run("alert", {"ORCH_EVENT": "alert", "ORCH_ACTION": "ALERT",
                                  "PROJECT_NAME": "lab", "TASK_NAME": "task-a"})
        assert pid is not None, "an executable hook must fire"
        assert _wait_for(marker)
        assert marker.read_text().strip() == "alert:ALERT:lab:task-a"
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: a global hook runs detached with the env contract")


def test_run_is_async_and_does_not_wait() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        marker = cfg / "done"
        _write_hook(cfg / "orch" / "hooks", "merged", (
            "#!/usr/bin/env bash\n"
            "sleep 2\n"
            f"touch {marker}\n"
        ))
        start = time.monotonic()
        pid = hooks.run("merged", {})
        elapsed = time.monotonic() - start
        assert pid is not None
        assert elapsed < 1.0, "fire-and-forget must not block the caller"
        assert _wait_for(marker, timeout=5.0)
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: the hook is fired asynchronously")


def test_run_noop_without_executable_hook() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        assert hooks.run("alert", {}) is None, "no hook, no fire"
        _write_hook(cfg / "orch" / "hooks", "alert", "#!/usr/bin/env bash\necho x\n",
                    executable=False)
        assert hooks.run("alert", {}) is None, "non-executable, no fire"
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: a missing or non-executable hook is a no-op")


def test_run_raises_on_bad_interpreter() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        _write_hook(cfg / "orch" / "hooks", "alert",
                    "#!/no/such/interpreter\necho x\n")
        try:
            hooks.run("alert", {})
        except OSError:
            pass
        else:
            raise AssertionError("a bad interpreter must raise OSError")
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: a hook that cannot spawn raises, and the caller logs it fail-open")


def test_install_examples_is_idempotent() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        from orch import opencode_events
        created, skipped = hooks.install_examples()
        assert created, "the first init writes the examples"
        assert skipped == []
        hooks_dir = cfg / "orch" / "hooks"
        assert all((hooks_dir / f"{e}.example").exists()
                   for e in hooks.GLOBAL_HOOKS)
        assert all((hooks_dir / f"{t}.example").exists()
                   for t in hooks.TASK_HOOKS)
        assert (hooks_dir / "session-idle.example").exists(), \
            "the session-idle verification template must be installed"
        for e in opencode_events.OPENCODE_EVENTS:
            name = opencode_events.hook_name(e)
            if name == "session-idle":
                continue
            assert (hooks_dir / f"{name}.example").exists(), e
        n_files = (len(hooks.GLOBAL_HOOKS) + len(hooks.TASK_HOOKS)
                   + len(opencode_events.OPENCODE_EVENTS))
        created2, skipped2 = hooks.install_examples()
        assert created2 == [] and skipped2
        assert len(skipped2) == n_files
        # examples are inert: not executable, and not a fired event name
        assert hooks.run("alert", {}) is None
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: install_examples is idempotent and the examples are inert")


def test_write_project_examples_matches_global_excluding_global_hooks() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        from orch import opencode_events
        hooks.install_examples()
        target = Path(tmp.name) / "proj"
        (target / ".orchestration").mkdir(parents=True)
        created, skipped = hooks.write_project_examples(target)
        assert created and skipped == []
        pdir = target / ".orchestration" / "hooks"
        for g in hooks.GLOBAL_HOOKS:
            assert not (pdir / f"{g}.example").exists(), \
                "the machine-level GLOBAL_HOOKS advisory templates are not copied"
        for name, _body in hooks.project_example_bodies():
            p = pdir / name
            assert p.exists(), name
            assert not os.access(p, os.X_OK), f"{name} must be non-executable"
        created2, skipped2 = hooks.write_project_examples(target)
        assert created2 == [] and skipped2, \
            "writing the project examples is idempotent"
        assert len(skipped2) == len(hooks.project_example_bodies())
        # the global dir and the project dir carry the same session-idle body
        g_verify = (cfg / "orch" / "hooks" / "session-idle.example").read_text()
        p_verify = (pdir / "session-idle.example").read_text()
        assert g_verify == p_verify, "the copies must not drift"
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: write_project_examples matches the global set minus GLOBAL_HOOKS")


def test_opencode_event_examples_are_reference_templates() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        from orch import opencode_events
        hooks.install_examples()
        hooks_dir = cfg / "orch" / "hooks"
        p = hooks_dir / "permission-asked.example"
        body = p.read_text()
        assert "permission.asked" in body, "the example documents its event type"
        assert "SSE consumer" in body, "an event example must describe its firing"
        assert "stdin" in body, "an event example must document the payload channel"
        verify = (hooks_dir / "session-idle.example").read_text()
        assert "no verification configured" in verify and "commit" in verify, \
            "session-idle.example must be the verification template, not an event one"
        pre = (hooks_dir / "pre-task-create.example").read_text()
        assert "pre-task-create" in pre and "TASK_NAME" in pre, \
            "task-lifecycle templates must be installed with the fired env contract"
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: opencode event examples are reference templates, wired but inert")


def _git_repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"],
                   check=True)
    (root / ".orchestration").mkdir(parents=True)
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "main"}\n')
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"],
                   check=True)
    return root, tmp


def _commit(root: Path, msg: str = "c") -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg],
                   check=True)


def test_project_catalog_matches_examples_one_to_one() -> None:
    from orch import opencode_events
    catalog = hooks.project_catalog()
    names = [n for n, _k, _w in catalog]
    assert len(names) == len(set(names)), "one catalog entry per hook name"
    bodies = {fname[: -len(".example")] for fname, _b in hooks.project_example_bodies()}
    assert set(names) == bodies, f"catalog and examples disagree: {set(names) ^ bodies}"
    kinds = {n: k for n, k, _w in catalog}
    assert kinds["session-idle"] == hooks.KIND_VERIFY
    assert kinds["pre-task-create"] == hooks.KIND_LIFECYCLE
    assert kinds["permission-asked"] == hooks.KIND_EVENT
    assert all(n in kinds for n in hooks.TASK_HOOKS)
    for event in opencode_events.OPENCODE_EVENTS:
        name = opencode_events.hook_name(event)
        if name != "session-idle":
            assert name in kinds, event
    assert hooks.GLOBAL_HOOKS.keys().isdisjoint(names), \
        "machine-level GLOBAL_HOOKS are not project hooks"
    print("ok: project_catalog covers exactly the project example set")


def test_project_hook_status_transitions() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        hooks.install_examples()
        root, rtmp = _git_repo()
        try:
            hooks.write_project_examples(root)
            # available: only the inert example exists
            assert hooks.project_hook_status(root, "main", "pre-task-create") \
                == hooks.STATUS_AVAILABLE
            # inactive: the real file exists but is not committed
            ok, _ = hooks.create_project_hook(root, "pre-task-create")
            assert ok
            assert hooks.project_hook_status(root, "main", "pre-task-create") \
                == hooks.STATUS_INACTIVE
            # active: committed on the base branch
            _commit(root)
            assert hooks.project_hook_status(root, "main", "pre-task-create") \
                == hooks.STATUS_ACTIVE
            # missing: no real hook and no example anywhere
            (root / ".orchestration" / "hooks" / "post-task-cleanup.example").unlink()
            (cfg / "orch" / "hooks" / "post-task-cleanup.example").unlink()
            assert hooks.project_hook_status(root, "main", "post-task-cleanup") \
                == hooks.STATUS_MISSING
        finally:
            rtmp.cleanup()
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: hook status moves available -> inactive -> active, else missing")


def test_create_project_hook_uses_example_and_guards() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        hooks.install_examples()
        root, rtmp = _git_repo()
        try:
            # the project's own example wins, so a customized reference is used
            pdir = root / ".orchestration" / "hooks"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "pre-task-create.example").write_text(
                "#!/usr/bin/env bash\n# custom project example\n")
            ok, _ = hooks.create_project_hook(root, "pre-task-create")
            assert ok
            real = pdir / "pre-task-create"
            assert real.read_text() == "#!/usr/bin/env bash\n# custom project example\n"
            assert os.access(real, os.X_OK), "the created hook is executable"
            # no clobber without force, and unknown names are refused
            ok2, msg2 = hooks.create_project_hook(root, "pre-task-create")
            assert not ok2 and "force" in msg2
            ok3, _ = hooks.create_project_hook(root, "pre-task-create", force=True)
            assert ok3
            ok4, msg4 = hooks.create_project_hook(root, "not-a-hook")
            assert not ok4 and "not a project hook" in msg4
            # a machine-level example is the fallback when the project has none
            ok5, _ = hooks.create_project_hook(root, "session-idle")
            assert ok5
            assert (pdir / "session-idle").exists()
        finally:
            rtmp.cleanup()
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: create uses the example, guards against clobber, refuses unknown")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} hooks tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
