from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from . import server

# Machine-level notification hooks. Same contract as the project hooks in
# .orchestration/hooks/ - named after what happened, executable, fired with an
# env-var contract - but global: they live in ~/.config/orch/hooks/, belong to
# the machine owner, and cannot be edited by any project's agent.
#
# Unlike the project gate, they are advisory: fired detached and async, a
# non-zero exit (or a missing hook) never blocks the daemon or the CLI.

GLOBAL_HOOKS = {
    "alert": "a task is blocked on a permission/question (or a project is untrusted)",
    "needs-review": "a task needs a human: dirty work, retries exhausted, "
                    "a conflict, or a missing worktree",
    "restart-server": "the shared server was restarted",
    "merged": "verified work was merged into base and archived",
    "removed": "a no-op branch was removed",
    "dispatched": "a task agent was dispatched",
    "dispatch-failed": "a dispatch failed",
    "resolve-failed": "automatic conflict resolution failed",
    "reset": "a task was reset to todo",
    "archived": "a task was closed and archived",
}

EXAMPLE_TEMPLATE = """#!/usr/bin/env bash
# {event}.example - {when}.
# Copy this file to hooks/{event} (same directory), make it executable, and the
# daemon and CLI fire it asynchronously. Advisory only: a non-zero exit is logged
# to ~/.local/state/orch/hooks/{event}.log, it never blocks anything.
# Fired with the full environment plus ORCH_EVENT, ORCH_ACTION, PROJECT_NAME,
# PROJECT_DIR, and - when task-scoped - TASK_NAME, BRANCH_NAME, WORKTREE_PATH,
# WORKTREE_NAME, BASE_BRANCH, ORCH_REASON, ORCH_DETAIL.

set -uo pipefail
echo "{event} (example): no-op"
exit 0
"""

def event_hook_template(name: str, ext: str, event: str, props: str,
                        commit: bool) -> str:
    """One reference template for an opencode server event, shared by the
    project hooks/*.sample (`commit=True`) and the global
    ~/.config/orch/hooks/*.example (`commit=False`) so the two cannot drift.
    The daemon's SSE consumer fires the real hook when the server emits the
    event, with the event's JSON on stdin."""
    enable = " and commit it" if commit else ""
    return f"""#!/usr/bin/env bash
# {name}.{ext} - opencode server event "{event}".
# The daemon's SSE consumer fires this when the server emits "{event}": copy this
# file to hooks/{name}, make it executable{enable}. The event arrives as JSON on
# stdin, with ORCH_EVENT={name} (plus session/project env when one applies).
# Fire-and-forget: a non-zero exit is logged to ~/.local/state/orch, never
# blocking the stream.
# Properties: {props}

set -uo pipefail
read -r payload
echo "{name} (opencode event): $payload"
exit 0
"""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "orch" / "hooks"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def hooks_dir() -> Path:
    return config_dir()


def hook_path(event: str) -> Path:
    return config_dir() / event


def _log_file(event: str) -> Path:
    d = server.state_dir() / "hooks"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d / f"{event}.log"


def _neutral_cwd() -> Path:
    d = server.state_dir() / "hooks-cwd"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def has(event: str) -> bool:
    p = hook_path(event)
    return p.exists() and os.access(p, os.X_OK)


def run(event: str, env: Mapping[str, str] | None = None,
        cwd: Path | None = None) -> int | None:
    """Fire a global notification hook, detached and fail-open. Returns the
    pid when one was fired, None when there is no executable hook. Raises
    OSError if the hook exists but cannot be spawned (bad interpreter, ...)."""
    hook = hook_path(event)
    if not hook.exists() or not os.access(hook, os.X_OK):
        return None
    log = _log_file(event)
    handle = None
    try:
        handle = open(log, "ab")
        proc = subprocess.Popen(
            [str(hook)],
            cwd=str(cwd or _neutral_cwd()),
            env={**os.environ, **(dict(env) if env else {})},
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if handle is not None:
            handle.close()
    return proc.pid


def install_examples(force: bool = False) -> tuple[list[str], list[str]]:
    """Write an inert `.example` template for every documented global hook and
    every opencode server event. Returns (created, skipped) file names."""
    from . import opencode_events

    created: list[str] = []
    skipped: list[str] = []
    d = config_dir()
    for event, when in GLOBAL_HOOKS.items():
        name = f"{event}.example"
        p = d / name
        if p.exists() and not force:
            skipped.append(name)
            continue
        p.write_text(EXAMPLE_TEMPLATE.format(event=event, when=when))
        created.append(name)
    for event, props in opencode_events.OPENCODE_EVENTS.items():
        name = opencode_events.hook_name(event)
        fname = f"{name}.example"
        p = d / fname
        if p.exists() and not force:
            skipped.append(fname)
            continue
        p.write_text(event_hook_template(name, "example", event, props,
                                         commit=False))
        created.append(fname)
    return created, skipped
