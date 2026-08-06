from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, gate

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


def spec(title: str, hitl: bool, context: str) -> str:
    """A spec with the source line already in Context, so an imported task is
    never empty — the rest of the sections await the human or --assist."""
    return (
        f"---\nhitl: {str(hitl).lower()}\n---\n\n"
        f"# Task: {title}\n\n"
        "## Metadata\n\n"
        "- **Complexity:** <Low|Medium|High>\n"
        "- **Priority:** <Low|Medium|High>\n"
        "- **Status:** Ready for Handoff\n"
        "- **Dependencies:** none\n\n"
        "## Context\n\n"
        f"{context}\n\n"
        "## Requirements\n\n"
        "- [ ] <requirement>\n\n"
        "## Verification\n\n"
        "_How to prove this is done: the behaviour to observe, and how it was "
        "exercised. Code that exists is not a completed task._\n\n"
        "## Prohibited Patterns\n\n"
        "- <what not to do, and why it is tempting>\n"
    )


def parse_items(text: str) -> list[str]:
    """One task per line: plain text, or markdown `- ` / `1. ` list items.
    Blank lines and `#` headers are skipped."""
    items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^([-*+]|\d+[.)])\s+", "", line).strip()
        if line:
            items.append(line)
    return items


def _unique_slug(text: str, used: set[str], max_len: int = 60) -> str:
    base = dispatch.slug(text) or "task"
    name = base[:max_len].rstrip("-") or "task"
    n = 1
    while name in used:
        suffix = f"-{n}"
        name = (base[:max_len - len(suffix)].rstrip("-") + suffix) or name
        n += 1
    used.add(name)
    return name


def import_specs(start: Path, items: list[str], auto: bool = False) -> AddResult:
    result = AddResult()
    root, _, err = dispatch.project_of(start)
    if err or root is None:
        return result.fail(err or "not inside a git worktree")

    base = gate.base_branch(root)
    note = off_base_note(root, base)
    if note:
        result.notes.append(note)

    used: set[str] = set()
    for item in items:
        name = _unique_slug(item, used)
        path = root / "tasks" / name / "task.md"
        rel = task_rel(name)
        if path.exists():
            result.skipped.append(f"{rel} (already exists)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec(title=item, hitl=not auto, context=item))
        result.created.append(rel)

    if result.created:
        on = dispatch.current_branch(root)
        result.notes.append(
            f"COMMIT these in {root}"
            f"{f' on {on}' if on else ''} before dispatching. A task worktree is "
            f"created from {base}, so an uncommitted task file gives the agent no "
            f"instructions at all."
        )
        result.notes.append(
            "Each spec's Context holds the source line. Fill in Requirements and "
            "Verification - or run `orch task add <name> --assist` to draft them - "
            "then commit."
        )
    return result


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


def assist_prompt(rel: str, description: str) -> str:
    return (
        "You are drafting a task specification, not doing the task. "
        f"Rewrite the file {rel} (it already exists as a template) to fully "
        "describe the task below. Keep its front matter and its exact sections "
        "(## Metadata, ## Context, ## Requirements, ## Verification, "
        "## Prohibited Patterns). Write concrete, verifiable content: "
        "Requirements as checkboxes, and a Verification section that states the "
        "observable behaviour proving it is done. Do not edit anything else.\n\n"
        f"Task intent: {description}"
    )


@dataclass
class AssistResult:
    ok: bool
    notes: list[str] = field(default_factory=list)


def assist_spec(start: Path, name: str, description: str,
                timeout: int = 600) -> AssistResult:
    """Draft tasks/<name>/task.md with a one-shot opencode agent. The human
    reviews and commits the result; this never claims or dispatches the task."""
    from . import server

    rel = task_rel(name)
    if not (start / rel).exists():
        return AssistResult(False, [f"{rel} does not exist; run `orch task add` "
                                    "first"])
    try:
        srv = server.start()
    except (RuntimeError, TimeoutError, OSError) as e:
        return AssistResult(False, [f"could not start the shared server: {e}"])
    cmd = ["opencode", "run", "--attach", srv.url, "--dir", str(start),
           "--format", "json", "--title", f"spec:{name}",
           assist_prompt(rel, description)]
    try:
        p = subprocess.run(cmd, cwd=str(start), capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return AssistResult(False, [f"could not run the assistant: {e}"])
    if p.returncode != 0:
        return AssistResult(False, [f"assistant exited {p.returncode}: "
                                    f"{(p.stderr or p.stdout)[-500:]}"])
    return AssistResult(True, ["drafted by an opencode agent - review, then "
                               "commit before dispatching"])
