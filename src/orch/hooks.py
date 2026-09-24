from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from . import server

# Hook templates live here, machine-level, in ~/.config/orch/hooks/*.example:
# every notification hook, the session-idle verification hook, the task-lifecycle
# hooks, and one per opencode server event. They are inert - you copy one into a
# project's .orchestration/hooks/<name>, make it executable, and commit it.
# The project-relevant set (session-idle, the task-lifecycle hooks, and one per
# opencode server event - never the machine-level notification hooks) is also
# scaffolded into a fresh project's .orchestration/hooks/ as the same inert,
# gitignored *.example references, so the hook directory matches the global one.
#
# The executable copies in a project's .orchestration/hooks/ follow the same
# contract - named after what happened, fired with an env-var contract - but
# unlike the machine-level notification hooks (advisory: fired detached and async,
# a non-zero exit never blocks the daemon or the CLI), the project hooks run from
# the committed base-branch copy and can gate the workflow.

GLOBAL_HOOKS = {
    "alert": "a task is blocked on a permission/question (or a project is untrusted)",
    "needs-review": "a task needs a human: dirty work, retries exhausted, "
                    "a conflict, or a missing worktree",
    "restart-server": "the shared opencode service was restarted",
    "merged": "verified work was merged into base and archived",
    "removed": "a no-op branch was removed",
    "dispatched": "a task agent was dispatched",
    "dispatch-failed": "a dispatch failed",
    "resolve-failed": "automatic conflict resolution failed",
    "reset": "a task was reset to todo",
    "archived": "a task was closed and archived",
}

TASK_HOOKS = {
    "pre-task-create": "before the task worktree is created, from the dispatching "
                      "worktree; non-zero refuses the dispatch",
    "post-task-create": "right after the task worktree is created, from the new "
                        "worktree",
    "pre-task-cleanup": "before the task worktree is removed at teardown",
    "post-task-cleanup": "after the worktree and branch are gone",
    "pre-branch-delete": "before the task branch is deleted",
}

SESSION_IDLE_TEMPLATE = """#!/usr/bin/env bash
# session-idle.{ext} - what "verify" means for this project.
#
# Copy this file to .orchestration/hooks/session-idle, make it executable, and
# commit it to enable verification. Only the version committed on the base branch
# runs - edit, then commit, before dispatching.
#
# Contract (full version in .orchestration/README.md): the agent declares a task
# done by committing its work to the branch. This hook verifies that committed
# work; exit 0 passes and commits anything the agent left uncommitted, non-zero
# fails and everything written to stdout/stderr is fed back to the agent as its
# next instructions.

set -euo pipefail
cd "${WORKTREE_PATH:-$(git rev-parse --show-toplevel)}" || exit 1

# Put this project's real checks above the commit block, e.g. `pnpm check && pnpm test`.
# `set -e` aborts on the first failing command, and the output becomes the agent's
# next instructions, so keep it useful and bounded.
echo "no verification configured yet - edit .orchestration/hooks/session-idle" >&2
exit 1

# Verification passed: commit the work (idempotent - the agent usually committed
# already, so an empty tree has nothing to commit, and it still passes so the
# daemon can re-run it safely).
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.name=orch -c user.email=orch@localhost \\
      commit -q -m "orch: session-idle verification passed"
fi
exit 0
"""

TASK_HOOK_TEMPLATE = """#!/usr/bin/env bash
# {name}.{ext} - {when}.
# Copy this file to .orchestration/hooks/{name}, make it executable, and commit
# it to enable. Fired with TASK_NAME  BRANCH_NAME  WORKTREE_PATH  WORKTREE_NAME
# BASE_BRANCH. Exit 0 lets the event proceed; non-zero aborts it and the failure
# is logged.

set -uo pipefail
echo "{name} ({ext}): no-op"
exit 0
"""

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

def event_hook_template(name: str, ext: str, event: str, props: str) -> str:
    """One reference template for an opencode server event, installed machine-level
    as ~/.config/orch/hooks/{name}.example (and shared with nothing else - the
    template is the single source). The daemon's SSE consumer fires the real hook
    when the server emits the event, with the event's JSON on stdin."""
    props_line = f"# Properties: {props}" if props else \
        "# Properties: the event JSON is on stdin (V2 puts them under `data`)"
    return f"""#!/usr/bin/env bash
# {name}.{ext} - opencode server event "{event}".
# The daemon's SSE consumer fires this when the server emits "{event}": copy this
# file to .orchestration/hooks/{name}, make it executable, and commit it. The
# event arrives as JSON on stdin, with ORCH_EVENT={name} (plus session/project env
# when one applies).
# Fire-and-forget: a non-zero exit is logged to ~/.local/state/orch, never
# blocking the stream.
{props_line}

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
        cwd: Path | None = None, payload: str | None = None) -> int | None:
    """Fire a global notification hook, detached and fail-open. Returns the
    pid when one was fired, None when there is no executable hook. Raises
    OSError if the hook exists but cannot be spawned (bad interpreter, ...).
    `payload`, when given, is written to the hook's stdin."""
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
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
        if payload is not None and proc.stdin is not None:
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.close()
    finally:
        if handle is not None:
            handle.close()
    return proc.pid


def _example_bodies() -> list[tuple[str, str]]:
    """(fname, body) pairs for every inert `.example` template, from one source:
    the global hooks, the session-idle verification hook, the task-lifecycle
    hooks, and one per opencode server event. The machine-level install and the
    project-level scaffold both write from this list, so the copies cannot
    drift."""
    from . import opencode_events

    bodies: list[tuple[str, str]] = []
    for event, when in GLOBAL_HOOKS.items():
        bodies.append((f"{event}.example",
                       EXAMPLE_TEMPLATE.format(event=event, when=when)))
    bodies.append(("session-idle.example",
                   SESSION_IDLE_TEMPLATE.replace("{ext}", "example")))
    for name, when in TASK_HOOKS.items():
        bodies.append((f"{name}.example",
                       TASK_HOOK_TEMPLATE.format(name=name, when=when,
                                                 ext="example")))
    for event, props in opencode_events.OPENCODE_EVENTS.items():
        name = opencode_events.hook_name(event)
        if name == "session-idle":
            continue
        bodies.append((f"{name}.example",
                       event_hook_template(name, "example", event, props)))
    return bodies


def project_example_bodies() -> list[tuple[str, str]]:
    """The project-relevant `.example` set (session-idle + the task-lifecycle
    hooks + one per opencode server event), excluding the machine-level
    GLOBAL_HOOKS advisory templates which are not per-project. Written into a
    project's `.orchestration/hooks/` so a fresh repo's hook directory matches
    the global one, with each sample carrying its env-var contract."""
    global_names = {f"{e}.example" for e in GLOBAL_HOOKS}
    return [(name, body) for name, body in _example_bodies()
            if name not in global_names]


def install_examples(force: bool = False) -> tuple[list[str], list[str]]:
    """Write an inert `.example` template for every global hook, the
    session-idle verification hook, every task-lifecycle hook, and every
    opencode server event. Returns (created, skipped) file names."""
    created: list[str] = []
    skipped: list[str] = []
    d = config_dir()

    def write_example(fname: str, body: str) -> None:
        p = d / fname
        if p.exists() and not force:
            skipped.append(fname)
            return
        p.write_text(body)
        created.append(fname)

    for fname, body in _example_bodies():
        write_example(fname, body)
    return created, skipped


def write_project_examples(target: Path, force: bool = False
                           ) -> tuple[list[str], list[str]]:
    """Write the project-relevant inert `.example` templates (session-idle, the
    task-lifecycle hooks, and one per opencode server event - never the
    machine-level GLOBAL_HOOKS) into `target/.orchestration/hooks/`. Each copy
    is non-executable and stays gitignored (`.orchestration/.gitignore` already
    ignores `*.example`). Returns (created, skipped) file names. Idempotent: an
    existing file is skipped unless `force`."""
    created: list[str] = []
    skipped: list[str] = []
    d = target / ".orchestration" / "hooks"
    d.mkdir(parents=True, exist_ok=True)

    for fname, body in project_example_bodies():
        p = d / fname
        if p.exists() and not force:
            skipped.append(fname)
            continue
        p.write_text(body)
        created.append(fname)
    return created, skipped

