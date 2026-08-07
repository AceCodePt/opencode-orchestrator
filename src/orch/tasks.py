from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, envfilter, gate

TEMPLATE = """---
hitl: {hitl}
---

# Task: {title}

## Metadata

- **Complexity:** <Low|Medium|High>
- **Priority:** <Low|Medium|High>
- **Status:** Ready for Handoff
- **Dependencies:** none

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


def template(title: str, hitl: bool = True) -> str:
    return TEMPLATE.format(title=title, hitl="true" if hitl else "false")


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
    auto: bool = False,
) -> AddResult:
    result = AddResult()

    root, _, err = dispatch.project_of(start)
    if err or root is None:
        return result.fail(err or "not inside a git worktree")

    if len(names) > 1 and title:
        return result.fail(
            "--title describes one task, so it cannot be combined with "
            "several names. Add them one at a time, or edit the files afterwards."
        )

    base = gate.base_branch(root)
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
        path.write_text(template(shown, hitl=not auto))
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


def draft_prompt(text: str, auto: bool) -> str:
    hitl = "false" if auto else "true"
    prompt = (
        "You are converting free text into task specifications for an "
        "orchestrator that dispatches one agent per task into its own git "
        "worktree. Write each spec to tasks/{{slug}}/task.md (create files and "
        "directories as needed). Every spec must follow exactly this template, "
        "with hitl: {hitl} in the front matter:\n\n"
        "---\nhitl: {hitl}\n---\n\n"
        "# Task: <title>\n\n"
        "## Metadata\n\n"
        "- **Complexity:** <Low|Medium|High>\n"
        "- **Priority:** <Low|Medium|High>\n"
        "- **Status:** Ready for Handoff\n"
        "- **Dependencies:** none\n\n"
        "## Context\n\n"
        "<why this exists, from the description>\n\n"
        "## Requirements\n\n"
        "- [ ] <concrete requirement from the description>\n\n"
        "## Verification\n\n"
        "<the observable behaviour proving it is done - make it runnable>\n\n"
        "## Prohibited Patterns\n\n"
        "- <what not to do, and why>\n\n"
        "Decide whether the description is ONE task or several INDEPENDENT "
        "tasks; prefer one unless the work clearly decomposes into independent "
        "deliverables. Put the description's checkboxes into Requirements and "
        "give each task a Verification the project's gate could run. Do not "
        "create TASKS.md, do not touch any file outside tasks/, do not commit. "
        "When done, report the list of files you created.\n\n"
        "Description:\n"
    ).format(hitl=hitl)
    return prompt + text


def _task_files(root: Path) -> set[str]:
    base = root / "tasks"
    if not base.is_dir():
        return set()
    return {f"tasks/{d.name}/task.md" for d in base.iterdir()
            if (d / "task.md").is_file()}


def draft_specs(start: Path, text: str, auto: bool = False,
                timeout: float = 600.0) -> AddResult:
    """Have a one-shot agent turn free text into one or more task specs. The
    human reviews and commits the result; this never claims or dispatches."""
    from . import server

    result = AddResult()
    root, _, err = dispatch.project_of(start)
    if err or root is None:
        return result.fail(err or "not inside a git worktree")

    try:
        srv = server.start()
    except (RuntimeError, TimeoutError, OSError) as e:
        return result.fail(f"could not start the shared server: {e}")

    before = _task_files(root)
    cmd = ["opencode", "run", "--attach", srv.url, "--dir", str(root),
           "--format", "json", "--title", "spec-draft",
           draft_prompt(text, auto)]
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

