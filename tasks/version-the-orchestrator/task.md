# Task: Version the Orchestrator and Cut `rdk-template` Over to It

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** In Progress — repo created, migration outstanding
- **Dependencies:** None

## Context

`task-runner.sh` and the five lifecycle hooks were living in
`/home/sagi/stuff/rdk-template/`, the parent directory holding the bare repo.
That directory is not a git repository — confirmed, `git rev-parse` fails there
and neither file appears in `git ls-files` for the bare repo or any worktree.

So the component that dispatches every agent had no history, no backup, and no
way to be reused by a second project. Meanwhile the plan is to run this same
workflow for `linkedin-reader` and on the box, which would have meant copying
unversioned shell scripts around by hand.

This repo is the fix. Creating it is done. Migration is not, and matters more:
until `rdk-template` actually consumes these copies, there are two divergent
originals and the tracked ones will rot.

## Requirements

- [x] Create a bare repo plus worktree in the established layout.
- [x] Copy in `bin/task-runner.sh` and `hooks/`, leaving the originals working.
- [ ] Decide how consumers reference this repo: a submodule, a clone on `PATH`,
      or a symlink from each project's parent directory. Write down the choice.
- [ ] Cut `rdk-template` over, then delete the untracked originals so there is
      exactly one copy. Leaving both is the failure mode this task exists to end.
- [ ] Verify `gwadd`'s dependency is intact — it is a zsh function in `~/.zshrc`
      (mirrored in `~/dotfiles/.zshrc`), so the runner is not portable to a
      non-interactive shell or to the box without addressing that.
- [ ] Add `git worktree prune` where stale worktrees currently accumulate;
      `gwadd` only guards the opposite case.
- [ ] Push to a remote so "unbacked-up" stops being true.

## Verification

Dispatch a real task in `rdk-template` using the runner from this repo, end to
end: entry flips to `[-]` and commits, worktree is created, tmux session starts,
the agent comes up with a live port, and `OPENCODE_PARENT_SESSION_ID` reaches the
child. Then confirm no `task-runner.sh` remains outside this repo.

## Prohibited Patterns

- Do **not** leave two copies in place. Divergence is the whole problem.
- Do **not** rewrite the runner's behaviour while moving it. Move first, verify
  it still works, change it in a later task.
