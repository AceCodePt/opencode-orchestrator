from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, gate

TASKS_REL = "TASKS.md"
BACKLOG_HEADING = "## Backlog"

ENTRY_RE = re.compile(r"^- \[[ x\-?]\] ")

TEMPLATE = """# Task: {title}

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

TEMPLATE_TASKS_MD = """# Project Tasks

## Status Legend

- `[ ]` **Todo**: available to be picked up.
- `[-]` **In Progress**: currently being executed.
- `[x]` **Done**: completed and verified.
- `[?]` **Indeterminate**: blocked or needs review.

## Backlog

Later additions go at the top.
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


def template(title: str) -> str:
    return TEMPLATE.format(title=title)


def backlog_entry(name: str, title: str, desc: str | None = None) -> str:
    entry = f"- [ ] [{title}]({task_rel(name)})"
    if desc:
        entry += f" \u2014 {desc}"
    return entry


def has_entry(content: str, name: str) -> bool:
    return f"]({task_rel(name)})" in content


def insert_backlog(content: str, entries: list[str]) -> str:
    if not entries:
        return content
    block = list(entries)
    lines = content.splitlines()

    heading = None
    for i, line in enumerate(lines):
        if line.strip().lower() == BACKLOG_HEADING.lower():
            heading = i
            break

    if heading is None:
        tail = lines + ([""] if lines and lines[-1].strip() else [])
        return "\n".join(tail + [BACKLOG_HEADING, "", *block, ""]) + "\n"

    at = len(lines)
    for i in range(heading + 1, len(lines)):
        stripped = lines[i].strip()
        if ENTRY_RE.match(stripped) or stripped.startswith("**") \
                or stripped.startswith("#"):
            at = i
            break

    while at > heading + 1 and not lines[at - 1].strip():
        at -= 1

    out = lines[:at] + ["", *block] + lines[at:]
    return "\n".join(out) + "\n"


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
    desc: str | None = None,
) -> AddResult:
    result = AddResult()

    root, _, err = dispatch.project_of(start)
    if err or root is None:
        return result.fail(err or "not inside a git worktree")

    if len(names) > 1 and (title or desc):
        return result.fail(
            "--title and --desc describe one task, so they cannot be combined with "
            "several names. Add them one at a time, or edit the files afterwards."
        )

    base = gate.base_branch(root)
    note = off_base_note(root, base)
    if note:
        result.notes.append(note)

    tasks_md = root / TASKS_REL
    existed = tasks_md.is_file()
    content = tasks_md.read_text() if existed else TEMPLATE_TASKS_MD
    if not existed:
        result.notes.append(
            f"{TASKS_REL} did not exist and was created. It is the human-readable "
            f"view of the backlog; `orch` reads intent from tasks/*/task.md."
        )

    entries: list[str] = []
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
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template(shown))
            result.created.append(rel)

        if has_entry(content, name):
            result.skipped.append(f"{TASKS_REL} entry for {name} (already listed)")
        else:
            entries.append(backlog_entry(name, shown, desc))

    if entries or not existed:
        tasks_md.write_text(insert_backlog(content, entries))
        for e in entries:
            result.created.append(f"{TASKS_REL}: {e.strip()}")

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
