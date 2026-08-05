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
    "gate": {
        "$comment": (
            "Files that matter for the diff gate. Contents are hashed, not mtimes. "
            "Exclude anything whose change should not trigger verification."
        ),
        "include": ["src/**", "bin/**", "**/*.ts", "**/*.sh"],
        "exclude": ["**/*.md", "docs/**", "tasks/**"],
        "retryCap": 3,
    },
}

TEMPLATE_SESSION_IDLE = """#!/usr/bin/env bash
# session-idle - what "verify" means for this project.
#
# Fired by the router plugin when an agent goes idle AND the files declared in
# ../config.json actually changed. Named after the event, not the action.
#
# Provided: WORKTREE_PATH  WORKTREE_NAME  ORCH_EVENT  ORCH_SESSION_ID
#
# Contract: exit 0 = the gate passed. Non-zero = failed, and everything on
# stdout/stderr is fed back to the agent as its next instructions, so keep the
# output useful and bounded.
#
# Absence of this file is reported as `no-gate-configured`, never as a pass.
#
# IMPORTANT: the version that runs is the one COMMITTED on the base branch, not
# the copy in your working tree. Editing this file mid-task changes nothing about
# how the task is judged - you must commit it. That is deliberate: a dispatched
# agent was observed rewriting this very file to make its own gate pass, and an
# agent that can edit its own gate can always pass it.

set -uo pipefail
cd "${WORKTREE_PATH:-$(git rev-parse --show-toplevel)}" || exit 1

# Replace with this project's real verification, e.g.
#   pnpm check && pnpm test
#   cargo clippy -- -D warnings && cargo test
#   ruff check . && pytest -q
echo "no verification configured yet - edit .orchestration/hooks/session-idle" >&2
exit 1
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
- the **router plugin** fires agent-lifecycle events: `session-idle`,
  `session-error`, and anything else the harness emits

Exit 0 passes, non-zero fails and is fed back to the agent, and a **missing** hook
is `no-gate-configured` - which is not a pass.

A project with no database legitimately needs no `post-task-create`. Absent hooks
are fine; a missing `session-idle` is not, because then nothing verifies the work.
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


def run(start: Path, force: bool = False, version: str = "0",
        force_hooks: bool = False) -> InitResult:
    target, notes = resolve_target(start)
    if target is None:
        return InitResult([], [], notes)

    created: list[str] = []
    skipped: list[str] = []

    orch = target / ".orchestration"
    hooks = orch / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)

    def write(p: Path, body: str, executable: bool = False,
              protected: bool = False) -> None:
        rel = str(p.relative_to(target))
        if p.exists() and not force:
            skipped.append(rel)
            return
        if p.exists() and protected and not force_hooks:
            skipped.append(f"{rel} (protected; --force-hooks to overwrite)")
            return
        p.write_text(body)
        if executable:
            p.chmod(0o755)
        created.append(rel)

    write(orch / "README.md", TEMPLATE_README)
    write(orch / "config.json", json.dumps(TEMPLATE_CONFIG, indent=2) + "\n")
    write(hooks / "session-idle", TEMPLATE_SESSION_IDLE, executable=True,
          protected=True)
    write(orch / ".version", version + "\n")

    plugins = target / ".opencode" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parents[2] / "plugin" / "orch-router.ts"
    if src.is_file():
        from .gate import MIN_ROUTER_CONTRACT, router_contract

        have, _ = router_contract(target)
        dest = plugins / "orch-router.ts"
        if have is not None and have < MIN_ROUTER_CONTRACT and not force:
            dest.write_text(src.read_text())
            created.append(f".opencode/plugins/orch-router.ts (contract {have} "
                           f"-> {MIN_ROUTER_CONTRACT})")
            notes.append(
                f"upgraded the router from contract {have} to {MIN_ROUTER_CONTRACT} "
                f"without being asked: the supervisor relies on it reporting when a "
                f"gate starts, and without that a slow gate is read as an agent "
                f"nothing checked. COMMIT it, or dispatched worktrees keep the old one."
            )
        else:
            write(dest, src.read_text())
    else:
        notes.append(f"router plugin template not found at {src}")

    stale = target / ".orchestration" / ".state"
    if stale.exists():
        notes.append(
            f"{stale} is left over from the file-based router state and is no longer "
            f"read. Delete it; fingerprints and retry counters are in the orch "
            f"database now, which is what makes them visible to `orch task list`."
        )

    notes.append(
        "the scaffolded session-idle hook exits non-zero on purpose: an unconfigured "
        "gate must not report success. Edit it before dispatching real work."
    )
    notes.append(
        "COMMIT the hook before dispatching. The router runs the version committed on "
        "the base branch, not the working-tree copy, so an uncommitted gate counts as "
        "no gate at all. A dispatched agent was observed rewriting its own gate to "
        "make it pass, which is why the committed version is the only authority."
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
