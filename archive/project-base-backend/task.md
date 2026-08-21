---
wait_human_start: false
wait_human_merge: false
dependencies: [project-base-repoint-spec]
---

# Task: Backend: `project.base()` repoint logic in project.py

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff

## Context

The backend core of the `project base` repoint command for existing projects. Must match the behavior of the `--base` clone path (src/orch/project.py clone(): branches check at line ~305-324, worktree add at ~328, _set_base_branch at ~235, and the commit() helper at ~339-349). Slices after this one (CLI, e2e) build on it; this slice must leave the backend complete and unit-tested on its own.

## Requirements

- [ ] A `base(common, target_branch, ...)`-style function in src/orch/project.py repoints an existing project's base branch.
- [ ] The function requires the target branch to already exist in the store (use the existing `_branches` helper); if it does not, it returns a clean failure message and creates no state.
- [ ] If no worktree is yet checked out on the target branch, one is created via `git worktree add <project>/<branch> <branch>` (mirroring clone).
- [ ] `baseBranch` in the new worktree's `.orchestration/config.json` is set via the existing `_set_base_branch` helper.
- [ ] The config change is committed on the NEW branch with the machine git identity (mirroring clone's commit() helper), so `git show <branch>:.orchestration/config.json` carries the new baseBranch immediately.
- [ ] The old base branch/worktree is left untouched as an ordinary checkout.
- [ ] Trust is not modified.
- [ ] Unit tests in tests/test_project.py cover: new base worktree exists on the new branch; committed baseBranch == new branch; old base still exists; `_base_worktree(common, new)` resolves; unknown target branch fails with no worktree created.
- [ ] All tests in tests/test_project.py pass.

## Verification

`python3 tests/test_project.py` passes, including the new tests asserting the new base worktree exists on the new branch, the committed baseBranch equals the new branch, the old base branch still exists, `_base_worktree(common, new)` resolves, and an unknown branch fails cleanly without creating a worktree.

## Prohibited Patterns

- Do NOT auto-create the new branch if it does not exist; refuse cleanly instead (parity with clone).
- Do NOT delete/rewrite the old base branch or worktree; it just ceases to be the base worktree (a normal checkout remains).
- Do NOT touch trust state; trust is preserved unchanged.
- Do NOT add comments unless the surrounding style already uses them.
