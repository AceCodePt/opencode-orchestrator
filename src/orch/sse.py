from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from . import envfilter, harness, hooks, opencode_events, server, state, verify

RECONNECT_S = 2.0

# The acceptance hook: the project's committed hooks/session-idle the daemon
# runs on settle. It is never fired from the event stream - it has acceptance
# semantics (commits on pass), not advisory ones.
ACCEPTANCE_EVENT = "session-idle"


def hook_name(event_type: str) -> str:
    return opencode_events.hook_name(event_type)


def payload_of(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False)


def _label(common: Path) -> str:
    p = Path(common)
    return p.parent.name if p.name == ".git" else p.name


def event_env(ev: dict) -> dict[str, str]:
    props = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    env = {"ORCH_EVENT": hook_name(ev.get("type", ""))}
    if ev.get("type"):
        env["ORCH_EVENT_TYPE"] = str(ev["type"])
    if ev.get("id"):
        env["ORCH_EVENT_ID"] = str(ev["id"])
    if props.get("sessionID"):
        env["ORCH_SESSION_ID"] = str(props["sessionID"])
    return env


def _project_env(ev: dict, common: Path, base: str, worktree: Path) -> dict[str, str]:
    env = event_env(ev)
    env["PROJECT_DIR"] = str(common)
    env["PROJECT_NAME"] = _label(common)
    env["BASE_BRANCH"] = base
    env["WORKTREE_PATH"] = str(worktree)
    env["WORKTREE_NAME"] = worktree.name
    return env


def fire_global(ev: dict) -> list[str]:
    name = hook_name(ev.get("type", ""))
    try:
        pid = hooks.run(name, event_env(ev), payload=payload_of(ev))
    except OSError as e:
        return [f"global event hook {name} could not spawn: {e}"]
    if pid is not None:
        return [f"fired global event hook {name} pid={pid}"]
    return []


def _log_file(common: Path, name: str) -> Path:
    d = server.state_dir() / "event-hooks" / _label(common)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d / f"{name}.log"


def fire_project(common: Path, base: str, name: str, ev: dict,
                 worktree: Path) -> list[str]:
    """Run the committed hooks/<name> for an opencode event, detached and
    fail-open, with the event JSON on stdin. Only the committed copy on the
    base branch runs; a working-tree edit is ignored, and the acceptance hook's
    own event (session-idle) is never fired from the stream."""
    if name == ACCEPTANCE_EVENT:
        return []
    base_wt = state._base_worktree(common, base)
    if base_wt is None:
        return []
    rel = f"{verify.HOOKS_REL}/{name}"
    committed = verify.show(base_wt, base, rel)
    if committed is None or not committed.strip():
        return []
    if envfilter.verify_env_full(base_wt):
        base_env = dict(os.environ)
    else:
        base_env = envfilter.filtered_env(
            os.environ, repo_drop=envfilter.repo_drops(base_wt))
    env = {
        **base_env,
        **_project_env(ev, common, base, worktree),
        "ORCH_VERIFIER": "daemon",
    }
    # Materialize the committed hook to a stable path (never deleted: a bash
    # script reads its file incrementally, so removing a temp copy right after
    # spawn breaks the running hook; a fixed path also matches how global hooks
    # live). Overwriting a later fire is fine - the committed copy is the
    # authority, and a short script is read quickly.
    exe = _log_file(common, name).with_suffix("")
    log = exe.with_suffix(".log")
    handle = None
    try:
        exe.write_text(committed)
        exe.chmod(0o755)
        handle = open(log, "ab")
        proc = subprocess.Popen(
            [str(exe)],
            cwd=str(worktree),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(payload_of(ev).encode("utf-8"))
            proc.stdin.close()
        return [f"fired project event hook {name} pid={proc.pid} -> {log}"]
    except OSError as e:
        return [f"project event hook {name} could not spawn: {e}"]
    finally:
        if handle is not None:
            handle.close()


def dispatch(ev: dict, owner: tuple[str, str, str] | None,
             trusted: set[str]) -> list[str]:
    """Route one streamed event to its global hook and, when its session
    belongs to a trusted project's worktree, that project's committed hook."""
    log: list[str] = []
    log.extend(fire_global(ev))
    if owner is not None:
        common, base, worktree = owner
        if common in trusted:
            name = hook_name(ev.get("type", ""))
            log.extend(fire_project(Path(common), base, name, ev, Path(worktree)))
    return log


class EventWatcher(threading.Thread):
    """Consume the shared opencode service's /api/event stream and fire per-event hooks.
    Reconnects on transport failure; stop() tears it down. The `resolver`
    callable maps a session id to (common, base, worktree) or None."""

    def __init__(self, resolver, interval: float = RECONNECT_S) -> None:
        super().__init__(daemon=True)
        self._resolver = resolver
        self._interval = interval
        self._stop_flag = threading.Event()
        self.lines: list[str] = []

    def stop(self) -> None:
        self._stop_flag.set()

    def _dispatch(self, ev: dict) -> None:
        trusted = state.trusted_projects()
        props = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        owner = None
        if props.get("sessionID"):
            owner = self._resolver(str(props["sessionID"]))
        self.lines.extend(dispatch(ev, owner, trusted))

    def run(self) -> None:
        while not self._stop_flag.is_set():
            st = server.status()
            port = (st.get("port") if st.get("running")
                    and not st.get("unresponsive") else None)
            if port is None:
                self._stop_flag.wait(self._interval)
                continue
            try:
                for ev in harness.event_stream(port):
                    if self._stop_flag.is_set():
                        return
                    self._dispatch(ev)
            except (OSError, TimeoutError):
                pass
            self._stop_flag.wait(self._interval)
