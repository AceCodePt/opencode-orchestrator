---
wait_human_start: false
wait_human_merge: false
dependencies: []
---

# Task: Spec: `orch project base <branch>` repoints an existing project's base branch

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff

## Context

The user wants an existing project whose base branch is `main` to be repointable (e.g. to `preview`/`dev`) with a single command, such that afterward it behaves exactly as if it had been originally created with `orch project clone --base <branch>` — the only difference being the old `main` branch still exists (which a `--base` clone may also have; it just is no longer the base branch). Today there is no command for this: `--base` is only available at clone time. The new command fills the existing-project gap. Decisions confirmed with the user: (1a) leave the old `main` base worktree in place as an ordinary checkout (non-destructive), (2a) require the new branch to already exist (parity with clone, no auto-create).

## Requirements

- [ ] A `bin/orch project base <branch>` command exists that repoints an existing project's base branch from `main` (or whatever it currently is) to `<branch>`.
- [ ] Behavior parity with a fresh `orch project clone --base <branch>`: `baseBranch` in `.orchestration/config.json` committed on the new branch equals `<branch>`; a base worktree is checked out on `<branch>`; tasks branch from it, merges fold into it, and the primary agent + committed-verification-hook authority all resolve to it.
- [ ] The old base branch (e.g. `main`) still exists afterward as a normal branch/worktree; nothing destructive is done to it.
- [ ] The new branch must already exist in the repo; if it does not, the command fails cleanly (non-zero) with a helpful message and leaves no partial state (no worktree created).
- [ ] Trust is preserved unchanged; the command never trusts/untrusts a project and never touches registered state beyond the repo.
- [ ] Each implementation slice ships its own unit tests, and an e2e slice exercises the full loop for both the backend and CLI paths.
- [ ] Docs (README + architecture/decisions) document the command and the 'old base branch stays a normal branch' semantics.

## Verification

Run `for t in tests/test_*.py; do python3 "$t"; done` and see all tests pass, including the new `project base` unit tests and the e2e test that repoints a project's base branch and runs the full loop (dispatch from the new base, verify, merge).

## Prohibited Patterns

- Do NOT auto-create the new branch if it does not exist (breaks clone parity; clone requires it).
- Do NOT delete, rewrite, or otherwise mutate the old base branch or its worktree beyond it ceasing to be the base worktree.
- Do NOT advertise this as 'switching environments' beyond the base branch; orch has one integration branch per project.
- Do NOT change trust state or registered state outside the repo.
- Do NOT add explanatory comments to code unless the surrounding code style already uses them.
