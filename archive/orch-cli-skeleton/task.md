# Task: Build the `orch` CLI Skeleton With `init`, `task start`, `observe`, `doctor`

## Metadata

- **Complexity:** High
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** None — everything else assumes this exists

## Context

`orch` is a globally installed tool, not something a project vendors. It is the
only option that survives being handed to a machine that is not yours: dotfiles
are personal, an infra repo is one box, and a copied script diverges.

See `docs/architecture.md` for the component split. This task builds the shell of
the CLI, not the daemon and not the observer's policy.

**Language is decided:** Python/bash for the CLI and daemon, TypeScript for the
router plugin (forced by the plugin API). All harness HTTP knowledge lives on the
Python side so there is nothing to keep in sync between the two. See
`docs/architecture.md`.

## Requirements

- [ ] `orch init` — scaffold `.orchestration/` (hooks directory, config, version
      marker) and install the router plugin into the project's `.opencode/`.
      Decide and document what happens when the target has only a bare repo and no
      worktree yet: there is no working tree to write into, so `init` must name the
      worktree and branch that receive the scaffold.
- [ ] `orch task start <name…>` — implements Flow A in the architecture doc, in
      that order. The TUI launch belongs here, **not** in project scripts, so that
      `--port` cannot be forgotten by a project.
- [ ] `orch observe` — prints live state for every discovered TUI, standalone,
      with no daemon required.
- [ ] `orch doctor` — verifies the launch line carries `--port`, hooks resolve,
      the version marker matches the CLI, and reports which events the installed
      opencode emits.
- [ ] Copy `gwadd`'s stale-directory guard and add `git worktree prune`. Stale
      worktrees already accumulate and opencode's own list has drifted from
      `git worktree list`.
- [ ] Refuse to dispatch when the daemon is unreachable, with an explicit
      override. Write the lease row either way.
- [ ] Declare a minimum opencode version and degrade rather than lie. Local is
      1.18.13; the box is pinned 1.18.11.

## Verification

Run `orch init` against a scratch bare repo plus worktree, then `orch task start`
on a trivial task. The TUI must come up with a reachable port, `orch observe` must
show it, and `orch doctor` must pass. Then run `orch init` against **this** repo —
if it cannot scaffold itself, the contract is wrong.

## Prohibited Patterns

- Do **not** let the CLI depend on the daemon for dispatch beyond the reachability
  check. Integration is through SQLite, not RPC between your own components.
- Do **not** put the TUI launch, worktree creation, or tmux handling in project
  scripts. Those are identical everywhere and belong here.
- Do **not** hardcode anything a single project knows. If it mentions a database
  name or a package manager, it is in the wrong repo.
