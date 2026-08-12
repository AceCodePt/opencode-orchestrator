from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, envfilter, state, verify

TEMPLATE = """---
wait_human_start: {wait_human_start}
wait_human_merge: {wait_human_merge}
dependencies: {dependencies}
---

# Task: {title}

## Metadata

- **Complexity:** <Low|Medium|High>
- **Priority:** <Low|Medium|High>
- **Status:** Ready for Handoff

## Context

_Why this task exists: what is broken or missing, and the evidence for it._

## Requirements

- [ ] <requirement>

## Verification

_How to prove this is done: the behaviour to observe, and how it was exercised.
Code that exists is not a completed task._

## Prohibited Patterns

- <what not to do, and why it is tempting>
"""


@dataclass
class AddResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def fail(self, msg: str) -> "AddResult":
        self.notes.append(msg)
        self.ok = False
        return self


def title_from_name(name: str) -> str:
    words = [w for w in dispatch.slug(name).split("-") if w]
    return " ".join(w.capitalize() for w in words)


def task_rel(name: str) -> str:
    return f"tasks/{name}/task.md"


def template(title: str, wait_human_start: bool = False,
             wait_human_merge: bool = False,
             dependencies: list[str] | None = None) -> str:
    deps = dependencies or []
    return TEMPLATE.format(
        title=title,
        wait_human_start="true" if wait_human_start else "false",
        wait_human_merge="true" if wait_human_merge else "false",
        dependencies="[" + ", ".join(deps) + "]")


def off_base_note(root: Path, base: str) -> str | None:
    current = dispatch.current_branch(root)
    if current == base:
        return None

    shown = repr(current) if current else "a detached HEAD"
    on_base = dispatch.worktree_on_branch(root, base)
    where = str(on_base) if on_base else f"<worktree on {base}>"
    return (
        f"{root.name} is on {shown}, not {base}. A task is project-wide and "
        f"`orch task start` reads specs from {base}, so this one is not dispatchable "
        f"until the branch is merged, and is lost if it never is. Add it with "
        f"-C {where} to have it dispatchable now."
    )


def add_many(
    start: Path,
    names: list[str],
    title: str | None = None,
    depends: list[str] | None = None,
) -> AddResult:
    result = AddResult()

    root, common, err = dispatch.project_of(start)
    if err or root is None:
        return result.fail(err or "not inside a git worktree")

    if common is not None:
        state.trust_project(common)
        result.notes.append(
            "trusted this project for the daemon (auto from `task add`); "
            "`orch project untrust` revokes that."
        )

    if len(names) > 1 and title:
        return result.fail(
            "--title describes one task, so it cannot be combined with "
            "several names. Add them one at a time, or edit the files afterwards."
        )

    deps = [dispatch.slug(d) for d in (depends or []) if dispatch.slug(d)]

    base = verify.base_branch(root)
    note = off_base_note(root, base)
    if note:
        result.notes.append(note)

    for raw in names:
        name = dispatch.slug(raw)
        if not name:
            result.skipped.append(f"{raw!r} does not slugify to anything usable")
            result.ok = False
            continue

        shown = title or title_from_name(name)
        path = root / "tasks" / name / "task.md"
        rel = task_rel(name)

        if path.exists():
            result.skipped.append(f"{rel} (already exists)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template(shown, dependencies=deps))
        result.created.append(rel)

    if result.created:
        on = dispatch.current_branch(root)
        result.notes.append(
            f"COMMIT these in {root}"
            f"{f' on {on}' if on else ''} before dispatching. A task worktree is "
            f"created from {base}, so an uncommitted task file gives the agent no "
            f"instructions at all - `orch task start` refuses rather than let it fail "
            f"silently."
        )
        result.notes.append(
            "Fill in Context, Requirements and Verification. A task whose "
            "Verification says nothing cannot be judged done by anything."
        )
    return result


def draft_prompt(text: str) -> str:
    prompt = (
        "You are converting free text into task specifications for an "
        "orchestrator that dispatches one agent per task into its own git "
        "worktree. Write each spec to tasks/{slug}/task.md (create files and "
        "directories as needed). Every spec must follow exactly this template, "
        "with wait_human_start: false, wait_human_merge: false, and a "
        "dependencies: [] list in the front matter:\n\n"
        "---\n"
        "wait_human_start: false\n"
        "wait_human_merge: false\n"
        "dependencies: []\n"
        "---\n\n"
        "# Task: <title>\n\n"
        "## Metadata\n\n"
        "- **Complexity:** <Low|Medium|High>\n"
        "- **Priority:** <Low|Medium|High>\n"
        "- **Status:** Ready for Handoff\n\n"
        "## Context\n\n"
        "<why this exists, from the description>\n\n"
        "## Requirements\n\n"
        "- [ ] <concrete requirement from the description>\n\n"
        "## Verification\n\n"
        "<the observable behaviour proving it is done - make it runnable>\n\n"
        "## Prohibited Patterns\n\n"
        "- <what not to do, and why>\n\n"
        "### Structure the work\n\n"
        "Decide whether the description is ONE task or several. A single effort "
        "gets TWO layers of specs, written in one pass:\n\n"
        "- **The spec task** at tasks/<feature>/task.md — the parent. Its "
        "Requirements are the user stories (As an <actor>, I want <feature>, so "
        "that <benefit>), and it records Implementation Decisions and Out of "
        "Scope. Do not reference file paths or line numbers — they go stale. It "
        "sets wait_human_start: true and is never auto-dispatched; it is the "
        "anchor the slices point at.\n"
        "- **Slice tasks** at tasks/<feature>-<slice>/task.md — vertical tracer "
        "bullets, one per independent deliverable. Each cuts a narrow but "
        "COMPLETE path through every layer (schema, API, UI, tests), is sized to "
        "fit in a single fresh context window, and is demoable or verifiable on "
        "its own.\n\n"
        "Each slice task's front matter lists its blocking edges as "
        "`dependencies: [<slug>, ...]` — the spec task plus any earlier slices "
        "it needs. Render each slug exactly as the task directory name it "
        "belongs to; the project's scheduler only dispatches a slice once every "
        "dependency is archived, so an edge to a slug that never gets written is "
        "a permanently blocked task. Prefer one task unless the work clearly "
        "decomposes into independent deliverables.\n\n"
        "A wide mechanical refactor whose blast radius fans across the codebase "
        "is NOT a vertical slice. Sequence it as expand-contract: first a task "
        "that adds the new form beside the old so nothing breaks, then one "
        "migrate task per batch of call sites (each listing its own "
        "dependencies), then a final contract task that deletes the old form "
        "once no caller remains, blocked by every migrate task.\n\n"
        "Describe WHAT the system should do, not HOW to implement it — the "
        "dispatched agent explores the codebase fresh and makes its own "
        "implementation decisions. Give every task a Verification the project's "
        "verification hook could actually run. Put the description's checkboxes into "
        "Requirements. Do not create TASKS.md, do not touch any file outside "
        "tasks/, do not commit. When done, report the list of files you created "
        "and the dependency edges between them.\n\n"
        "Description:\n"
    )
    return prompt + text


def _task_files(root: Path) -> set[str]:
    base = root / "tasks"
    if not base.is_dir():
        return set()
    return {f"tasks/{d.name}/task.md" for d in base.iterdir()
            if (d / "task.md").is_file()}


def draft_specs(start: Path, text: str, timeout: float = 600.0) -> AddResult:
    """Have a one-shot agent turn free text into one or more task specs. The
    human reviews and commits the result; this never claims or dispatches."""
    from . import server

    result = AddResult()
    root, common, err = dispatch.project_of(start)
    if err or root is None:
        return result.fail(err or "not inside a git worktree")

    if common is not None:
        state.trust_project(common)
        result.notes.append(
            "trusted this project for the daemon (auto from `task draft`); "
            "`orch project untrust` revokes that."
        )

    try:
        srv = server.start()
    except (RuntimeError, TimeoutError, OSError) as e:
        return result.fail(f"could not start the shared server: {e}")

    before = _task_files(root)
    cmd = ["opencode", "run", "--attach", srv.url, "--dir", str(root),
           "--format", "json", "--title", "spec-draft",
           draft_prompt(text)]
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=timeout,
                           env=envfilter.filtered_env(
                               os.environ, repo_drop=envfilter.repo_drops(root)))
    except (OSError, subprocess.TimeoutExpired) as e:
        return result.fail(f"could not run the drafting agent: {e}")
    if p.returncode != 0:
        return result.fail(f"drafting agent exited {p.returncode}: "
                           f"{(p.stderr or p.stdout)[-500:]}")

    created = sorted(_task_files(root) - before)
    if not created:
        result.notes.append(
            "the agent ran but wrote nothing under tasks/ - check the "
            "description and try again"
        )
        result.ok = False
        return result

    result.created = created
    on = dispatch.current_branch(root)
    result.notes.append(
        f"COMMIT these in {root}"
        f"{f' on {on}' if on else ''} before dispatching. A task worktree is "
        f"created from base, so an uncommitted task file gives the agent no "
        f"instructions at all."
    )
    result.notes.append(
        "drafted by an opencode agent - review each spec, then commit"
    )
    return result


def render(r: AddResult) -> str:
    lines: list[str] = []
    for c in r.created:
        lines.append(f"  created  {c}")
    for s in r.skipped:
        lines.append(f"  skipped  {s}")
    if not r.created and not r.skipped:
        lines.append("  nothing written")
    for n in r.notes:
        lines.append(f"\n  ! {n}")
    return "\n".join(lines)

