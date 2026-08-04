# Why these files are here (and why they should leave)

These are `rdk-template`/reeeally's actual hooks. They are **examples only** and a
temporary safety copy.

They do not belong in this repo. By the criterion in `docs/architecture.md`, they
are project knowledge — they hardcode `reeeally_` database names,
`.env.local.{shared,web,public}`, a `reeeally-postgres` container, and
`$PROJECT_ROOT/scripts/db-create.sh`. A shared tool cannot own any of that.

They are here because they were living in `/home/sagi/stuff/rdk-template/hooks/`,
a directory belonging to no git repository, so they had no history and no backup.
Copying them here bought a backup, not a home.

Their real destination is `rdk-template`'s own tracked `.orchestration/hooks/`.
See `tasks/migrate-rdk-template/task.md`. When that lands, delete this directory —
leaving two copies is the failure this whole repo exists to end.

Read them as a reference for what the hook contract has to support in practice:

- `pre-task-create` runs before the worktree exists, so it can only see the
  dispatcher's checkout, and it is allowed to interactively abort.
- `post-task-create` runs inside the new worktree and mutates env files, which is
  why the TUI must launch strictly after it.
- `pre-branch-delete` guards destructive cleanup.
