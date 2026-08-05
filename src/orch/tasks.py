from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, gate

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
        path.write_text(template(shown))
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
