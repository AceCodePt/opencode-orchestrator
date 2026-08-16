from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_CONFIG = {
    "$comment": (
        "Project knowledge only. Anything here that every project would copy "
        "verbatim belongs in orch instead."
    ),
    "baseBranch": "main",
    "verify": {
        "$comment": (
            "How the acceptance hook (hooks/session-idle) is bounded. "
            "retryCap caps how many times a failing verification re-prompts the "
            "agent before the task lands on needs-review."
        ),
        "retryCap": 3,
    },
    "scheduler": {
        "$comment": (
            "Let the daemon auto-dispatch todo tasks itself (wait_human_start: false tasks only). "
            "maxConcurrent caps how many task agents run in this project at "
            "once. Edit with `orch project scheduler <n>`; only the committed "
            "copy on the base branch applies."
        ),
    },
}

TEMPLATE_GITIGNORE = """# Generated scaffolding and runtime state, not project intent.
# Real hooks (a copy of hooks/*.sample made executable and committed) ARE tracked;
# only the inert samples are ignored.
.state/
hooks/*.sample
"""

TEMPLATE_SESSION_IDLE_SAMPLE = """#!/usr/bin/env bash
# session-idle.sample - what "verify" means for this project.
#
# Copy this file to hooks/session-idle, make it executable, and commit it to
# enable verification. Only the version committed on the base branch runs - edit,
# then commit, before dispatching.
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

TEMPLATE_README = """# `.orchestration/`

This project's orchestration, deliberately **inside the worktree and tracked**.

Never place these beside the bare repo. A bare repo has no working tree, so
anything next to it is outside version control by construction - no history, no
backup, and no ability to differ per branch.

Hooks are named after **what happened**, not what to do. Two dispatchers fire them
from this one directory under one env-var contract (`TASK_NAME`, `BRANCH_NAME`,
`WORKTREE_PATH`, `WORKTREE_NAME`):

- the **CLI** fires task-lifecycle events: `pre-task-create`, `post-task-create`,
  `pre-task-cleanup`, `post-task-cleanup`, `pre-branch-delete`
- the **daemon** fires agent-lifecycle events: `session-idle` - the verification hook, run
  when a task's session settles

The working agent declares a task done by **committing its work to the branch** - git is
the only state that records it. `session-idle` is the verification hook the daemon runs on
that committed branch: exit 0 passes **and commits anything the agent left uncommitted**;
non-zero fails and the output is fed back to the agent by the daemon, which retries up to
`verify.retryCap` before marking the task needs-review. A **missing** hook means
the work is accepted without verification - the hook is the opt-in, and its
absence is the implicit unverified state.

The verification hook that runs is the version **committed on the base branch**, never the
working-tree copy, so edit the hook and commit it before dispatching. To configure
it, copy `hooks/session-idle.sample` to `hooks/session-idle`, make it executable,
and put the project's real checks above the commit block (e.g.
`pnpm check && pnpm test`, `cargo clippy -- -D warnings && cargo test`,
`ruff check . && pytest -q`); the sample exits non-zero on purpose until then.

A project with no database legitimately needs no `post-task-create`. Absent hooks
are fine, and so is a missing `session-idle`: it means the project's tasks are
accepted without verification.

Like the verification hook, the task-lifecycle hooks run the version **committed on the base
branch** — a working-tree edit, or a hook committed only on a task branch, never
runs. Edit a hook and commit it on the base branch before dispatching.

`config.json` holds project policy: `baseBranch`, the `verify` block
(`retryCap`), and — if you want the daemon
to dispatch `todo` tasks itself — a `scheduler` block (`enabled` + `maxConcurrent`,
the per-project cap). Like the verification hook, the **committed copy on the base branch** is the
authority: `orch project scheduler <n>` writes the working-tree copy for you to
review and commit. A machine-wide cap across all projects lives outside the repo in
`~/.config/orch/daemon.json` (`orch daemon budget`).

Reference samples for the hooks live in `hooks/*.sample` — inert until
you copy one to its real name, make it executable, and commit it. The
`session-idle.sample` is the verification hook template: copy it to `hooks/session-idle` for
the daemon to verify anything at all.

The remaining `hooks/*.sample` files are one per opencode server event
(the vocabulary in `src/orch/opencode_events.py`), fired by the daemon's SSE
consumer when the server emits that event — the event's JSON arrives on stdin.
Copy one to its real name, make it executable, and commit it to react to the
event. `session.idle` has no event sample here — that name is the verification
hook, so it keeps the verification template above.
"""

TASK_HOOKS = {
    "pre-task-create": "before the task worktree is created, from the dispatching "
                       "worktree; non-zero refuses the dispatch",
    "post-task-create": "right after the task worktree is created, from the new "
                        "worktree",
    "pre-task-cleanup": "before the task worktree is removed at teardown",
    "post-task-cleanup": "after the worktree and branch are gone",
    "pre-branch-delete": "before the task branch is deleted",
}


TEMPLATE_HOOK_SAMPLE = """#!/usr/bin/env bash
# {name}.sample - {when}.
# Copy this file to hooks/{name}, make it executable, and commit it to enable.
# Fired with TASK_NAME  BRANCH_NAME  WORKTREE_PATH  WORKTREE_NAME  BASE_BRANCH.
# Exit 0 lets the event proceed; non-zero aborts it and the failure is logged.

set -uo pipefail
echo "{name} (sample): no-op"
exit 0
"""


@dataclass
class InitResult:
    created: list[str]
    skipped: list[str]
    notes: list[str]


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=False)
    return p.returncode, (p.stdout or p.stderr).strip()


def resolve_target(start: Path) -> tuple[Path | None, list[str]]:
    notes: list[str] = []

    rc, bare = _git(["rev-parse", "--is-bare-repository"], start)
    if rc == 0 and bare == "true":
        notes.append(
            "this is a bare repository, which has no working tree. Hook definitions "
            "must be tracked inside a worktree - run `orch init` from one, or create "
            "one first with `git worktree add`. Writing beside a bare repo is how "
            "orchestration ends up unversioned."
        )
        return None, notes

    rc, top = _git(["rev-parse", "--show-toplevel"], start)
    if rc != 0 or not top:
        bares = sorted(p for p in start.glob("*.git") if p.is_dir())
        if bares:
            notes.append(
                f"found a bare repo at {bares[0].name} but no worktree here. This "
                "directory is not a git repository, so anything written here would be "
                "untracked. Run `orch init` from inside a worktree."
            )
        else:
            notes.append("not inside a git repository")
        return None, notes

    return Path(top), notes


def scaffold(target: Path, version: str = "0", force: bool = False
             ) -> tuple[list[str], list[str]]:
    """Write the .orchestration scaffolding (and machine-level global-hook
    examples) into `target`, which need not be a git worktree. Returns
    (created, skipped) relative paths. Does no repo resolution and no trust -
    the caller decides what to do with the files (e.g. `project create` commits
    them straight into a bare store)."""
    created: list[str] = []
    skipped: list[str] = []

    from . import hooks
    from . import opencode_events

    orch = target / ".orchestration"
    hooks_dir = orch / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    def write(p: Path, body: str, executable: bool = False) -> None:
        rel = str(p.relative_to(target))
        if p.exists() and not force:
            skipped.append(rel)
            return
        p.write_text(body)
        if executable:
            p.chmod(0o755)
        created.append(rel)

    write(orch / "README.md", TEMPLATE_README)
    write(orch / "config.json", json.dumps(TEMPLATE_CONFIG, indent=2) + "\n")
    write(orch / ".gitignore", TEMPLATE_GITIGNORE)
    write(hooks_dir / "session-idle.sample", TEMPLATE_SESSION_IDLE_SAMPLE)
    for name, when in TASK_HOOKS.items():
        write(hooks_dir / f"{name}.sample", TEMPLATE_HOOK_SAMPLE.format(
            name=name, when=when))
    for event, props in opencode_events.OPENCODE_EVENTS.items():
        name = opencode_events.hook_name(event)
        if name == "session-idle":
            continue
        write(hooks_dir / f"{name}.sample",
              hooks.event_hook_template(name, "sample", event, props,
                                        commit=True))
    write(orch / ".version", version + "\n")

    g_created, g_skipped = hooks.install_examples(force=force)
    for name in g_created:
        created.append(f"global-hooks/{name}")
    for name in g_skipped:
        skipped.append(f"global-hooks/{name}")

    return created, skipped


def run(start: Path, force: bool = False, version: str = "0") -> InitResult:
    target, notes = resolve_target(start)
    if target is None:
        return InitResult([], [], notes)

    from . import hooks
    from . import opencode_events
    created, skipped = scaffold(target, version, force)

    project_hooks = "\n".join(
        f"    - {name} - {when}" for name, when in TASK_HOOKS.items()
    )
    notes.append(
        "project hooks scaffold as hooks/*.sample, inert until copied to the "
        "real name, made executable, and committed:\n"
        "    - session-idle - the verification hook: verify and, on a pass, commit the work. "
        "Nothing is verified until hooks/session-idle exists.\n"
        + project_hooks
    )
    notes.append(
        f"one sample per opencode server event is scaffolded too "
        f"({len(opencode_events.OPENCODE_EVENTS) - 1} in hooks/*.sample; "
        "session-idle is the verification hook above). The daemon's SSE consumer fires the "
        "real hook when the server emits the event, with the event's JSON on "
        "stdin; the vocabulary lives in src/orch/opencode_events.py."
    )

    global_hooks = "\n".join(
        f"    - {event} - {when}" for event, when in hooks.GLOBAL_HOOKS.items()
    )
    notes.append(
        "global notification hooks (machine-level, outside any repo) are "
        "templated as ~/.config/orch/hooks/*.example - copy one to its real "
        "name, chmod +x, and the daemon/CLI fire it asynchronously. Advisory "
        "only: a non-zero exit never blocks anything.\n"
        + global_hooks
    )

    notes.append(
        "the scaffolded session-idle.sample exits non-zero on purpose: an "
        "unconfigured verification hook must not report success. Copy it to hooks/session-idle "
        "and edit it before dispatching real work."
    )
    notes.append(
        "COMMIT the hook before dispatching. The daemon runs the version committed on "
        "the base branch, not the working-tree copy, so an uncommitted verification hook counts as "
        "no verification at all. A dispatched agent was observed rewriting its own verification "
        "hook to make it pass, which is why the committed version is the only authority."
    )

    from . import state
    rc, common = _git(["rev-parse", "--path-format=absolute",
                       "--git-common-dir"], target)
    if rc == 0 and common:
        state.trust_project(Path(common))
        notes.append(
            "trusted this project for the daemon - it will verify, merge and "
            "dispatch here. `orch project untrust` revokes that."
        )
    else:
        notes.append(
            "could not resolve the git common dir, so this project was not "
            "trusted; `orch project trust` opts it in."
        )
    return InitResult(created, skipped, notes)


def render(r: InitResult) -> str:
    lines: list[str] = []
    for c in r.created:
        lines.append(f"  created  {c}")
    for s in r.skipped:
        lines.append(f"  exists   {s} (use --force to overwrite)")
    if not r.created and not r.skipped:
        lines.append("  nothing written")
    for n in r.notes:
        lines.append(f"\n  ! {n}")
    return "\n".join(lines)
