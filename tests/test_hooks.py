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
        n_events = len(opencode_events.OPENCODE_EVENTS)
        created, skipped = hooks.install_examples()
        assert created, "the first init writes the examples"
        assert skipped == []
        assert all((cfg / "orch" / "hooks" / f"{e}.example").exists()
                   for e in hooks.GLOBAL_HOOKS)
        assert all(
            (cfg / "orch" / "hooks" / f"{opencode_events.hook_name(e)}.example").exists()
            for e in opencode_events.OPENCODE_EVENTS
        )
        created2, skipped2 = hooks.install_examples()
        assert created2 == [] and skipped2
        assert len(skipped2) == len(hooks.GLOBAL_HOOKS) + n_events
        # examples are inert: not executable, and not a fired event name
        assert hooks.run("alert", {}) is None
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: install_examples is idempotent and the examples are inert")


def test_opencode_event_examples_are_reference_templates() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg, _state = _env(tmp)
        from orch import opencode_events
        hooks.install_examples()
        p = cfg / "orch" / "hooks" / "permission-asked.example"
        body = p.read_text()
        assert "permission.asked" in body, "the example documents its event type"
        assert "SSE consumer" in body, "an event example must describe its firing"
        assert "stdin" in body, "an event example must document the payload channel"
        assert hooks.run("permission-asked", {}) is None, "examples stay inert"
    finally:
        tmp.cleanup()
        _restore(saved)
    print("ok: opencode event examples are reference templates, wired but inert")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} hooks tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
