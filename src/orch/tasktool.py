from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import dispatch, state, verify

TASKS_REL = "tasks"
ARCHIVE_REL = "archive"

SECTIONS = {
    "Context": "context",
    "Requirements": "requirements",
    "Verification": "verification",
    "Prohibited Patterns": "prohibited_patterns",
}


class ToolError(Exception):
    pass


def _git(cwd: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _slug(name: str) -> str:
    slug = dispatch.slug(name)
    if not slug:
        raise ToolError(f"{name!r} does not slugify to anything usable")
    return slug


def _project(dir_: Path) -> tuple[Path, Path, str]:
    """Resolve the base worktree the tool may write in, or raise.

    The tools write task specs into the working tree of the base branch only.
    A session running anywhere else (a task worktree, a bare repo, a non-orch
    checkout) is refused: the daemon judges by the committed base copy, so
    specs written anywhere else are invisible to it."""
    if not dir_.is_dir():
        raise ToolError(f"{dir_} is not a directory")
    top, common, err = dispatch.project_of(dir_)
    if err or top is None or common is None:
        raise ToolError(err or "not inside a git worktree")
    if not (top / verify.ORCH_DIR).is_dir():
        raise ToolError(
            f"{top} is not an orch project (no {verify.ORCH_DIR}/). "
            f"Run `orch init`; these tools write task specs into the base "
            f"worktree of an orch project only."
        )
    base = verify.base_branch(top)
    branch = dispatch.current_branch(top)
    if branch != base:
        raise ToolError(
            f"refusing: {top} is on {branch or 'a detached HEAD'}, not the base "
            f"branch {base}. Task specs only count committed on {base}; run this "
            f"from the base worktree."
        )
    return top, Path(common), base


def _committed(top: Path, base: str, rel: str) -> bool:
    return verify.show(top, base, rel) is not None


def _claimed(common: Path, name: str) -> bool:
    return dispatch.branch_exists(common, name)


def _open_dependents(common: Path, name: str) -> list[str]:
    proj = state.Project.from_common(common)
    if proj is None:
        return []
    out: list[str] = []
    for t in proj.scan():
        if t.declared and not t.archived and name in t.dependencies:
            out.append(t.name)
    return out


def _commit_flag(args: dict, default: bool = True) -> bool:
    """The `commit` param. A task commits to base by default unless a human is
    in the loop (wait_human_start: true leaves a draft for review), so the
    daemon can see it and run the loop itself. Pass `commit` to override."""
    if "commit" in args and not isinstance(args["commit"], bool):
        raise ToolError("commit must be a boolean - it defaults to 'not "
                        "wait_human_start'; pass false to leave the change "
                        "uncommitted")
    return bool(args.get("commit", default))


def _commit(top: Path, rels: list[str], message: str) -> tuple[str | None, str | None]:
    """Commit exactly the given paths to the current branch as the orch machine
    user. Returns (sha, None) when committed, (None, None) when there was nothing
    to commit, or (None, error) on failure."""
    code, out = _git(top, "add", "-A", "--", *rels)
    if code != 0 and "did not match any files" not in (out or ""):
        return None, out[:300]
    code, out = _git(
        top, "-c", "user.name=orch", "-c", "user.email=orch@localhost",
        "commit", "-q", "-m", message, "--", *rels,
    )
    if code != 0:
        low = (out or "").lower()
        if "nothing to commit" in low or "no changes added" in low \
                or "did not match any file" in low:
            return None, None
        return None, out[:300]
    _rc, sha = _git(top, "rev-parse", "--short", "HEAD")
    return (sha.strip() or "HEAD"), None


# --- spec parse / render ----------------------------------------------------

def _parse_sections(lines: list[str], start: int) -> tuple[dict, int]:
    """Split the body into sections after the title. Returns {sections, i}."""
    sections: dict = {}
    current: str | None = None
    buf: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[len("## "):].strip()
            buf = []
        elif current is not None:
            buf.append(line)
        i += 1
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections, i


def parse_spec(text: str) -> dict:
    spec: dict = {}
    lines = text.splitlines()

    i = 0
    fm: dict = {}
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            line = lines[i]
            if ":" in line:
                key, _, value = line.partition(":")
                fm[key.strip().lower()] = value.strip().strip('"').strip("'")
            i += 1
        if i < len(lines):
            i += 1
    for key in ("wait_human_start", "wait_human_merge"):
        if key in fm:
            spec[key] = str(fm[key]).strip().lower() not in ("false", "no", "0")
    if "dependencies" in fm:
        spec["dependencies"] = state._dependencies_from(fm)

    for j in range(i, len(lines)):
        if lines[j].startswith("# Task:"):
            spec["title"] = lines[j][len("# Task:"):].strip()
            i = j + 1
            break

    sections, _ = _parse_sections(lines, i)
    meta = sections.get("Metadata") or ""
    for mline in meta.splitlines():
        mline = mline.strip()
        for key, field in (("Complexity", "complexity"),
                           ("Priority", "priority"),
                           ("Status", "status")):
            marker = f"- **{key}:**"
            if mline.startswith(marker):
                spec[field] = mline[len(marker):].strip()

    for heading, field in SECTIONS.items():
        body = sections.get(heading)
        if body is None:
            continue
        if field == "requirements":
            spec[field] = [ln.strip()[len("- [ ]"):].strip() for ln in body.splitlines()
                           if ln.strip().startswith("- [ ]")]
        elif field == "prohibited_patterns":
            spec[field] = [ln.strip()[len("-"):].strip() for ln in body.splitlines()
                           if ln.strip().startswith("-")]
        else:
            spec[field] = body
    return spec


def render_spec(spec: dict) -> str:
    out: list[str] = []
    out.append("---")
    out.append(f"wait_human_start: {'true' if spec.get('wait_human_start') else 'false'}")
    out.append(f"wait_human_merge: {'true' if spec.get('wait_human_merge') else 'false'}")
    deps = spec.get("dependencies") or []
    out.append("dependencies: [" + ", ".join(deps) + "]")
    out.append("---")
    out.append("")
    out.append(f"# Task: {spec.get('title') or 'Untitled'}")
    out.append("")
    out.append("## Metadata")
    out.append("")
    out.append(f"- **Complexity:** {spec.get('complexity') or 'Medium'}")
    out.append(f"- **Priority:** {spec.get('priority') or 'Medium'}")
    out.append("- **Status:** " + (spec.get('status') or 'Ready for Handoff'))

    context = spec.get("context")
    if context is not None and str(context).strip():
        out.append("")
        out.append("## Context")
        out.append("")
        out.append(str(context).rstrip())
    if "requirements" in spec and spec["requirements"]:
        out.append("")
        out.append("## Requirements")
        out.append("")
        for req in spec["requirements"]:
            out.append(f"- [ ] {req}")
    verification = spec.get("verification")
    if verification is not None and str(verification).strip():
        out.append("")
        out.append("## Verification")
        out.append("")
        out.append(str(verification).rstrip())
    if "prohibited_patterns" in spec and spec["prohibited_patterns"]:
        out.append("")
        out.append("## Prohibited Patterns")
        out.append("")
        for pat in spec["prohibited_patterns"]:
            out.append(f"- {pat}")
    return "\n".join(out) + "\n"


def _front_matter(spec: dict) -> dict:
    return {
        "wait_human_start": bool(spec.get("wait_human_start")),
        "wait_human_merge": bool(spec.get("wait_human_merge")),
        "dependencies": list(spec.get("dependencies") or []),
    }


# --- operations -------------------------------------------------------------

def create(top: Path, common: Path, base: str, args: dict) -> dict:
    for flag in ("wait_human_start", "wait_human_merge"):
        if flag in args and not isinstance(args[flag], bool):
            raise ToolError(f"{flag} must be a boolean when passed - decide it "
                            f"in the conversation; it defaults to false")
    commit = _commit_flag(args, default=not bool(args.get("wait_human_start", False)))
    name = _slug(str(args.get("name", "")))
    rel = f"{TASKS_REL}/{name}/task.md"
    path = top / rel

    if _committed(top, base, rel):
        raise ToolError(
            f"{rel} is committed on {base} - a task is already declared with "
            f"this slug. Use update_task to amend the draft, or archive_task/"
            f"delete_task if you meant to remove it."
        )
    if path.exists():
        raise ToolError(
            f"{rel} already exists in the working tree. Use update_task to amend "
            f"the draft, or delete_task to remove it first."
        )

    spec: dict = {
        "title": str(args.get("title") or name.title()),
        "wait_human_start": bool(args.get("wait_human_start", False)),
        "wait_human_merge": bool(args.get("wait_human_merge", False)),
        "dependencies": [dispatch.slug(d) for d in (args.get("dependencies") or [])
                         if dispatch.slug(d)],
        "complexity": str(args.get("complexity") or "Medium").title(),
        "priority": str(args.get("priority") or "Medium").title(),
        "status": "Ready for Handoff",
        "context": str(args.get("context") or "").strip(),
        "requirements": [str(r).strip() for r in (args.get("requirements") or [])
                         if str(r).strip()],
        "verification": str(args.get("verification") or "").strip(),
        "prohibited_patterns": [str(p).strip() for p in (args.get("prohibited_patterns") or [])
                                if str(p).strip()],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_spec(spec))
    committed: str | None = None
    if commit:
        committed, err = _commit(top, [rel], f"task: {name} (spec)")
        if err:
            raise ToolError(f"could not commit {rel}: {err}")
    if committed:
        message = (f"created and committed {rel} to {base} (sha {committed}). "
                   f"The daemon can now see it.")
    else:
        message = (f"created {rel} - uncommitted. Commit it on {base} (or pass "
                   f"commit: true) before the daemon can see it.")
    return {
        "path": rel,
        "message": message,
        "committed": bool(committed),
        "commitSha": committed if isinstance(committed, str) else None,
        "frontMatter": _front_matter(spec),
    }


def update(top: Path, common: Path, base: str, args: dict) -> dict:
    name = _slug(str(args.get("name", "")))
    rel = f"{TASKS_REL}/{name}/task.md"
    path = top / rel
    arch_rel = f"{ARCHIVE_REL}/{name}/task.md"

    if (top / arch_rel).exists() or _committed(top, base, arch_rel):
        raise ToolError(f"{name} is already archived at {arch_rel} - it is done")
    if _claimed(common, name):
        raise ToolError(
            f"{name} is claimed (branch {name} exists) - the daemon owns the "
            f"running work; amend its spec only after it settles."
        )
    if not path.exists():
        raise ToolError(f"no spec at {rel}; create_task writes it first")

    known = {"title", "wait_human_start", "wait_human_merge", "dependencies",
             "complexity", "priority", "context", "requirements", "verification",
             "prohibited_patterns"}
    passed = [k for k in args if k in known]
    if not passed:
        raise ToolError("nothing to change: pass at least one task field")
    if "wait_human_start" in args and not isinstance(args["wait_human_start"], bool):
        raise ToolError("wait_human_start must be a boolean")
    if "wait_human_merge" in args and not isinstance(args["wait_human_merge"], bool):
        raise ToolError("wait_human_merge must be a boolean")

    spec = parse_spec(path.read_text())
    for key in passed:
        value = args[key]
        if key == "dependencies":
            value = [dispatch.slug(d) for d in (value or []) if dispatch.slug(d)]
        elif key in ("requirements", "prohibited_patterns"):
            value = [str(v).strip() for v in (value or []) if str(v).strip()]
        elif key in ("complexity", "priority"):
            value = str(value or "").title()
        elif key in ("wait_human_start", "wait_human_merge"):
            value = bool(value)
        else:
            value = str(value).strip()
        spec[key] = value
    commit = _commit_flag(args, default=not bool(spec.get("wait_human_start", False)))

    path.write_text(render_spec(spec))
    committed: str | None = None
    if commit:
        committed, err = _commit(top, [rel], f"task: {name} (update)")
        if err:
            raise ToolError(f"could not commit {rel}: {err}")
    if committed:
        message = (f"updated and committed {rel} on {base} (sha {committed}, "
                   f"fields: {', '.join(passed)})")
    else:
        message = (f"updated {rel} (fields: {', '.join(passed)}) - uncommitted; "
                   f"pass commit: true or commit on {base} to make it visible")
    return {
        "path": rel,
        "message": message,
        "committed": bool(committed),
        "commitSha": committed if isinstance(committed, str) else None,
        "frontMatter": _front_matter(spec),
    }


def archive(top: Path, common: Path, base: str, args: dict) -> dict:
    commit = _commit_flag(args)
    name = _slug(str(args.get("name", "")))
    rel = f"{TASKS_REL}/{name}/task.md"
    dest = f"{ARCHIVE_REL}/{name}/task.md"

    if not _committed(top, base, rel):
        if (top / rel).exists():
            raise ToolError(
                f"{rel} is an uncommitted draft - commit it first, or use "
                f"delete_task to discard it."
            )
        raise ToolError(f"no task named {name} declared on {base}")
    if _claimed(common, name):
        raise ToolError(
            f"{name} is claimed (branch {name} exists) - the daemon owns claimed "
            f"work; archive it with `orch task archive` after it settles."
        )

    (top / dest).parent.mkdir(parents=True, exist_ok=True)
    code, out = _git(top, "mv", rel, dest)
    if code != 0:
        raise ToolError(f"git mv {rel} -> {dest} failed: {out[:300]}")
    committed: str | None = None
    if commit:
        committed, err = _commit(top, [rel, dest], f"task: archive {name}")
        if err:
            raise ToolError(f"could not commit the archive move: {err}")
    if committed:
        message = (f"moved and committed {rel} -> {dest} on {base} (sha "
                   f"{committed}). Archiving unblocks anything that depends on "
                   f"{name}.")
    else:
        message = (f"moved {rel} -> {dest} (staged, uncommitted). Archiving "
                   f"unblocks anything that depends on {name}; commit on {base} "
                   f"or pass commit: true to make it real.")
    return {
        "path": dest,
        "message": message,
        "committed": bool(committed),
        "commitSha": committed if isinstance(committed, str) else None,
        "frontMatter": {},
    }


def delete(top: Path, common: Path, base: str, args: dict) -> dict:
    commit = _commit_flag(args)
    name = _slug(str(args.get("name", "")))
    rel = f"{TASKS_REL}/{name}/task.md"
    arch_rel = f"{ARCHIVE_REL}/{name}/task.md"

    if _claimed(common, name):
        raise ToolError(
            f"{name} is claimed (branch {name} exists) - the daemon owns claimed "
            f"work; `orch task reset` or `orch task archive` handle it."
        )
    dependents = _open_dependents(common, name)
    if dependents:
        raise ToolError(
            f"cannot delete {name}: these open tasks depend on it and would be "
            f"permanently blocked: {', '.join(sorted(dependents))}"
        )

    path = top / rel
    if (top / arch_rel).exists() and not path.exists():
        code, out = _git(top, "rm", "-q", arch_rel)
        if code != 0:
            raise ToolError(f"git rm {arch_rel} failed: {out[:300]}")
        committed: str | None = None
        if commit:
            committed, err = _commit(top, [arch_rel], f"task: delete {name} (archived)")
            if err:
                raise ToolError(f"could not commit the removal: {err}")
        return {
            "path": arch_rel,
            "message": _delete_message("removed archived task", arch_rel,
                                       committed, base, commit),
            "committed": bool(committed),
            "commitSha": committed if isinstance(committed, str) else None,
            "frontMatter": {},
        }

    if path.exists():
        was_committed = _committed(top, base, rel)
        if was_committed:
            code, out = _git(top, "rm", "-q", rel)
            if code != 0:
                raise ToolError(f"git rm {rel} failed: {out[:300]}")
        else:
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
        committed: str | None = None
        if commit:
            committed, err = _commit(top, [rel], f"task: delete {name}")
            if err:
                raise ToolError(f"could not commit the removal: {err}")
        return {
            "path": rel,
            "message": _delete_message(
                "deleted" if was_committed else "deleted uncommitted draft",
                rel, committed, base, commit),
            "committed": bool(committed),
            "commitSha": committed if isinstance(committed, str) else None,
            "frontMatter": {},
        }

    raise ToolError(f"no task named {name} (neither {rel} nor {arch_rel} exists)")


def _delete_message(what: str, rel: str, committed, base: str,
                    commit: bool) -> str:
    if committed:
        return f"{what} {rel} and committed on {base} (sha {committed})"
    if commit:
        return f"{what} {rel} on {base} (nothing to commit)"
    return f"{what} {rel}; commit on {base} or pass commit: true to make it real"


OPS = {"create": create, "update": update, "archive": archive, "delete": delete}


def _run(op: str, args: dict, dir_: Path) -> dict:
    top, common, base = _project(dir_)
    handler = OPS[op]
    result = handler(top, common, base, args)
    out = {"ok": True, "op": op, **result}
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 1 or argv[0] not in OPS:
        print(json.dumps({"ok": False,
                          "error": f"usage: tasktool {('|'.join(OPS))} --json "
                                   f"<args> --dir <worktree>"}))
        return 2
    op = argv[0]
    args: dict = {}
    dir_: Path | None = None
    rest = argv[1:]
    while rest:
        token = rest.pop(0)
        if token == "--json":
            if not rest:
                return _err("--json needs a value")
            raw = rest.pop(0)
            if raw == "-":
                raw = sys.stdin.read()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                return _err(f"--json is not valid JSON: {e}")
            if not isinstance(parsed, dict):
                return _err("--json must be an object")
            args = parsed
        elif token == "--dir":
            if not rest:
                return _err("--dir needs a value")
            dir_ = Path(rest.pop(0))
        else:
            return _err(f"unknown argument {token!r}")
    if dir_ is None:
        return _err("--dir <worktree> is required")
    try:
        out = _run(op, args, dir_)
    except ToolError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001 - a tool must always answer in JSON
        return _err(f"internal error: {type(e).__name__}: {e}")
    print(json.dumps(out, indent=2))
    return 0


def _err(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
