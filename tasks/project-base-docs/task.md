---
wait_human_start: false
wait_human_merge: false
dependencies: [project-base-repoint-spec]
---

# Task: Docs: `project base` in README + architecture/decisions

## Metadata

- **Complexity:** Low
- **Priority:** Low
- **Status:** Ready for Handoff

## Context

Document the new `orch project base <branch>` command so it appears alongside the other project subcommands in README.md (usage block ~line 47-52 and the 'Cloning an existing repository'/'project create' sections) and the design notes in docs/architecture.md and docs/decisions.md. The user's mental model is 'make an existing main project behave as if cloned with --base preview, with main still existing'. Independent of the code slices (only needs the spec contract).

## Requirements

- [ ] README.md usage block gains a `bin/orch project base <branch>` line with a one-line description grouped with the other project subcommands.
- [ ] README.md documents the semantics: repoints an existing project's base branch so it behaves as if cloned with `--base <branch>`, requires the branch to already exist, and leaves the old base branch in place as a normal checkout.
- [ ] docs/architecture.md (and/or docs/decisions.md) records the repoint command, its clone-parity behavior, and the non-destructive 'old base branch stays ordinary' decision.

## Verification

`grep -n "project base" README.md` finds the usage line and a prose entry; the architecture/decisions files mention the repoint semantics (requires existing branch, old base stays as ordinary checkout).

## Prohibited Patterns

- Do NOT document branch auto-creation; the branch must already exist.
- Do NOT imply the old base branch is removed; it stays as an ordinary checkout.
- Do NOT overstate this as multi-environment support; it is a single per-project base branch repoint.
