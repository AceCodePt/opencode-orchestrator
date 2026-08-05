# `.orchestration/` — this project's own orchestration

This directory is `orch` dogfooding itself, and it is deliberately **inside the
worktree and tracked**.

That is the whole correction. `rdk-template`'s equivalent lives in
`/home/sagi/stuff/rdk-template/`, the parent directory next to the bare repo, which
belongs to no git repository — so it has no history, no backup, and no way to
differ per branch. A bare repo has no working tree, so anything placed beside it is
outside version control by construction.

Tracked-in-a-worktree buys three things:

1. **History.** You can see when a hook changed and why.
2. **Per-branch behaviour.** A task branch carries its own hooks, so changing how
   checks run is an ordinary reviewable change on a branch.
3. **It travels.** Clone the repo and the orchestration comes with it.

## What is here, and what is deliberately not

- `config.json` — which files matter for the diff gate, the retry cap, the base
  branch.
- `hooks/session-idle` — what "verify" means for this project, and the thing that
  commits a passing task's work.

There is no `post-task-create` here, and that is a signal rather than an omission.
This project has no database to create and no `.env` to rewrite, so it needs
almost no hooks.

There is no `.opencode/` router plugin any more. The daemon runs the gate itself
and injects failures itself; nothing is installed into the project's `.opencode/`.

## The contract

Hooks are named after **what happened**, not what to do. Two dispatchers fire them
from this one directory:

- The **CLI** fires task-lifecycle events: `pre-task-create`, `post-task-create`,
  `pre-task-cleanup`, `post-task-cleanup`, `pre-branch-delete`.
- The **daemon** fires agent-lifecycle events: `session-idle`, when a task's
  session settles.

Both pass `TASK_NAME`, `BRANCH_NAME`, `WORKTREE_PATH`, `WORKTREE_NAME`.

`session-idle` is the gate. Exit 0 passes **and commits the work**; non-zero fails
and the output is fed back to the agent by the daemon, which retries up to
`gate.retryCap` before marking the task needs-review. A **missing** hook is
`no-gate-configured` — never a pass. Absence of checks must never be reported as
success.

Note one asymmetry: `pre-task-create` necessarily runs from the *dispatching*
worktree because the new one does not exist yet, while `post-task-create` runs from
the new worktree's own checkout. So a task branch can change its own post-create
behaviour but not its pre-create behaviour.
