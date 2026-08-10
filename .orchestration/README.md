# `.orchestration/`

This project's orchestration, deliberately **inside the worktree and tracked**.

Never place these beside the bare repo. A bare repo has no working tree, so
anything next to it is outside version control by construction - no history, no
backup, and no ability to differ per branch.

Hooks are named after **what happened**, not what to do. Two dispatchers fire them
from this one directory under one env-var contract (`TASK_NAME`, `BRANCH_NAME`,
`WORKTREE_PATH`, `WORKTREE_NAME`):

- the **CLI** fires task-lifecycle events: `pre-task-create`, `post-task-create`,
  `pre-task-cleanup`, `post-task-cleanup`, `pre-branch-delete`
- the **daemon** fires agent-lifecycle events: `session-idle` - the gate, run
  when a task's session settles

`session-idle` is the gate: exit 0 passes **and commits the work**; non-zero fails
and the output is fed back to the agent by the daemon, which retries up to
`gate.retryCap` before marking the task needs-review. A **missing** hook is
`no-gate-configured` - which is not a pass.

The gate that runs is the version **committed on the base branch**, never the
working-tree copy, so edit the hook and commit it before dispatching. To configure
it, copy `hooks/session-idle.sample` to `hooks/session-idle`, make it executable,
and put the project's real checks above the commit block (e.g.
`pnpm check && pnpm test`, `cargo clippy -- -D warnings && cargo test`,
`ruff check . && pytest -q`); the sample exits non-zero on purpose until then.

A project with no database legitimately needs no `post-task-create`. Absent hooks
are fine; a missing `session-idle` is not, because then nothing verifies the work.

Like the gate, the task-lifecycle hooks run the version **committed on the base
branch** — a working-tree edit, or a hook committed only on a task branch, never
runs. Edit a hook and commit it on the base branch before dispatching.

`config.json` holds project policy: `baseBranch`, the `gate` block
(`include`/`exclude`/`retryCap`), and — if you want the daemon
to dispatch `todo` tasks itself — a `scheduler` block (`enabled` + `maxConcurrent`,
the per-project cap). Like the gate, the **committed copy on the base branch** is the
authority: `orch project scheduler <n>` writes the working-tree copy for you to
review and commit. A machine-wide cap across all projects lives outside the repo in
`~/.config/orch/daemon.json` (`orch daemon budget`).

Reference samples for the hooks live in `hooks/*.sample` — inert until
you copy one to its real name, make it executable, and commit it. The
`session-idle.sample` is the gate: copy it to `hooks/session-idle` for
the daemon to verify anything at all.

The remaining `hooks/*.sample` files are one per opencode server event
(the vocabulary in `src/orch/opencode_events.py`), fired by the daemon's SSE
consumer when the server emits that event — the event's JSON arrives on stdin.
Copy one to its real name, make it executable, and commit it to react to the
event. `session.idle` has no event sample here — that name is the gate, so it
keeps the gate template above.
