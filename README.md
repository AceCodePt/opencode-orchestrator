# opencode-orchestrator

Tooling that dispatches opencode agents into git worktrees and reports on their
live state.

**Read `docs/architecture.md` first.** It defines the component split, the state
model, and the flows. This README is only an entry point.

## What this is

`orch` is a **globally installed tool** — one CLI and one daemon per machine. It
is not a repo that projects clone, submodule, or vendor. Projects keep a small
tracked `.orchestration/` directory holding only what that project knows, and
`orch init` installs a generic event router into their `.opencode/`.

That shape is chosen for one reason: it is the only option that can be handed to a
machine that is not yours. Dotfiles are personal, an infra repo is one box, and a
copied script diverges.

## Why this repo exists

The dispatcher (`task-runner.sh`) and five lifecycle hooks were living in
`/home/sagi/stuff/rdk-template/` — the *parent* directory containing the bare
repo, which is not a git repository. The component that dispatches every agent had
no history and no backup.

`bin/task-runner.sh` and `examples/hooks-reeeally/` are **copies taken for
safety**, not a design. The generic half is being replaced by `orch`; the
project-specific half belongs in `rdk-template`'s own tracked
`.orchestration/`. Until `tasks/migrate-rdk-template/task.md` lands there are two
divergent copies, which is worse than one unversioned copy.

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
