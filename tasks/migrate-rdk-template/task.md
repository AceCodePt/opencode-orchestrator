---
wait_human_start: true
wait_human_merge: false
---

# Task: Migrate `rdk-template` to `.orchestration/` and Delete the Untracked Originals

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** `event-hook-router`

## Context

`task-runner.sh` and five lifecycle hooks live in `/home/sagi/stuff/rdk-template/`,
the parent directory holding the bare repo. That directory is not a git repository
— `git rev-parse` fails there and neither file appears in `git ls-files` for the
bare repo or any worktree. The component that dispatches every agent has no
history and no backup.

`examples/hooks-reeeally/` in this repo is a copy taken for safety. Right now there
are therefore **two divergent copies**, which is worse than one unversioned copy.
This task ends that.

The generic half does not migrate — it is replaced. Worktree creation, tmux, and
the TUI launch move into `orch`; only project knowledge moves into
`.orchestration/`.

## Requirements

- [ ] Create `.orchestration/hooks/` in `rdk-template`'s tracked worktree with the
      project-specific hooks: database creation, `.env.local.*` rewriting, the
      postgres container check, cleanup, branch-delete guard.
- [ ] Rename hooks to event names where they are not already.
- [ ] Move `pnpm check && pnpm test` out of `auto-check.ts` into
      `.orchestration/hooks/session-idle`.
- [ ] Replace `task-manager.ts`'s `path.join(worktree, '..', 'task-runner.sh')`
      with a call to `orch`. Reaching outside the worktree is what triggers the
      `external_directory` approval prompts in the first place.
- [ ] Add `.orchestration/` to `branch-firewall.ts`'s `SAFE_FOLDERS`, or decide
      deliberately that agents may not edit it. It is absent today, so agents are
      blocked from it by default.
- [ ] Decide what replaces the firewall's `git worktree` ban. A tool call is not a
      bash command, so the firewall never sees `orch`, which means the tool itself
      is now the authorization boundary.
- [ ] Delete `/home/sagi/stuff/rdk-template/task-runner.sh` and `hooks/`, then
      delete `examples/hooks-reeeally/` from this repo.

## Verification

Dispatch a real task in `rdk-template` through `orch`, end to end: the entry flips
and commits, the worktree appears, the DB and env are correct, the TUI comes up on
a reachable port, and the child's `session-idle` verification hook runs the project's checks.
Then confirm no `task-runner.sh` exists anywhere outside `orch`.

## Prohibited Patterns

- Do **not** leave two copies. Divergence is the problem this exists to end.
- Do **not** change the runner's behaviour during the move. Move, verify, then
  change in a separate task.
- Do **not** migrate the generic parts into `.orchestration/`. If every project
  would write the same thing, it belongs in `orch`.
