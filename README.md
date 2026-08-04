# opencode-orchestrator

Tooling that dispatches opencode agents into git worktrees and reports on their
live state.

## Why this repo exists

The dispatcher (`task-runner.sh`) and the five lifecycle hooks were living in
`/home/sagi/stuff/rdk-template/` — the *parent* directory that contains the bare
repo. That directory is not a git repository, so this machinery was unversioned,
unbacked-up, and impossible to share with a second project. It is now here.

`bin/task-runner.sh` and `hooks/` are **copies**. The originals are still in
place and still authoritative for `rdk-template`. Cutting `rdk-template` over to
consume this repo is a task, not a side effect — see
`tasks/version-the-orchestrator/task.md`.

## The workflow this serves

```
<project>/
  <project>.git/          bare repo
  <project>-preview/      integration worktree: TASKS.md, tasks/, .opencode/
  <project>-main/
  <kebab-task-name>/      ephemeral per-task worktree, one opencode per worktree
```

`bin/task-runner.sh` parses the next `- [ ]` entry out of
`git show HEAD:TASKS.md`, flips it to `- [-]`, commits, creates the worktree via
`gwadd`, and starts a detached tmux session running opencode with
`OPENCODE_PARENT_SESSION_ID` threaded through so the child can report back.

## The problem being solved next

Once several worktrees each have their own opencode TUI, there is no way to ask
"what is every agent doing right now?" A TUI started without `--port` is a black
box: no listening socket, no lock file, and pending approvals exist only in TUI
memory. Verified — the `permission` table in the global SQLite store holds zero
rows.

Started with `--port`, the same TUI becomes a queryable server. Verified against
a live TUI:

| Question                | Endpoint          | Signal                          |
| ----------------------- | ----------------- | ------------------------------- |
| Working or idle?        | `/session/status` | `{}` = idle, populated = busy   |
| Blocked on an approval? | `/permission`     | non-empty = waiting on a human  |
| Blocked on a question?  | `/question`       | non-empty = waiting on a human  |
| Which worktree is this? | `/api/location`   | maps port -> directory          |

`ss -ltnp | grep opencode` is the registry. No registry file is needed.

Caveats found the hard way:

- `/api/session/active` and `/api/permission/request` are **not** equivalent to
  the unprefixed routes. Both returned empty while a session was demonstrably
  busy and blocked. Use `/permission`, `/question`, `/session/status`.
- `/experimental/*` is unstable by name, and opencode's own worktree list has
  already drifted from `git worktree list`.

## Layout

- `bin/` — dispatcher
- `hooks/` — task lifecycle hooks (`pre`/`post` create and cleanup, branch delete)
- `tasks/` — one folder per task, each with a `task.md`
- `TASKS.md` — the backlog and the protocol for working it
