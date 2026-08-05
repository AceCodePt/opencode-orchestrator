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

## Usage today

Stdlib-only Python, no install step. Agents run **headless**: one shared
`opencode serve` hosts every worktree, and the TUI is an optional viewer.

```bash
bin/orch init                    # scaffold .orchestration/ + install the router plugin
bin/orch gate                    # can this project's gates be trusted at all
bin/orch doctor                  # verify the contracts that fail silently

bin/orch server start|status|stop|restart
bin/orch task start <name>       # claim, create the worktree, run hooks, dispatch
bin/orch task start <name> --dry-run
bin/orch task list
bin/orch task reset <name>       # return a task to available (deliberately manual)

bin/orch observe                 # live state, read-only, exits 1 if you are needed
bin/orch observe -w --json
bin/orch attach                  # open a TUI onto the shared server, close it freely
bin/orch events -d 30 -v         # tail SSE, then print a histogram of event types

bin/orch daemon install          # systemd user unit, enabled, with linger
bin/orch daemon status           # is anything actually supervising
bin/orch daemon --once --dry-run # decide and print, change nothing
```

`orch daemon` is one supervisor per machine covering every project, because there is one
shared server, one database and one set of leases per machine. Without it no lease is
renewed and no stopped agent is verified, so nothing reaches `done` — and everything
else still looks healthy, which is why `doctor` fails rather than warns when it is
missing.

Requires opencode **1.18.11 or newer**. That is the box's pin; newer is a bonus, not a
requirement. Minor releases are assumed API-compatible, so every version actually run
against is recorded in the database — a change then shows up as a dated fact instead of
inexplicable behaviour.

`observe` and `doctor` exit non-zero when something needs a human or when the
machinery itself is wrong, so they compose in a shell.

### Two things worth knowing before dispatching

**Commit the gate, the task file and the router.** A worktree is created *from* the
base branch, so anything uncommitted is invisible to the agent. Each of these was
observed failing silently: an uncommitted task file gave an agent no instructions
and it quietly stopped; an uncommitted hook degraded to `no-gate-configured`; a
worktree created before the router was committed ran with no gate at all while
`orch gate` reported fine. `orch task start` now refuses all three, and
`orch gate` explains which one is wrong.

**The dispatch process exiting is not the task finishing.** Measured: a run exited
0 and its session then did 24 more tool calls over 36 seconds, because the router
injected a follow-up prompt. Watch `orch observe`, not the exit code.

## What works, and what does not

Working and exercised against live agents: `init`, `gate`, `doctor`, `server`,
`task start`/`list`/`reset`, `observe`, `attach`, `events`, the claims/leases store,
and the router plugin — including the full loop of event → hook → failure → prompt
injection → agent fix → pass, with a bounded retry cap.

`orch daemon` supervises: it renews leases while an agent is alive, alerts on
blocked permissions keyed by request id, restarts a dead or unresponsive shared
server and escalates the tasks that were inside it, and reclaims a task only on all
three proofs together — expired lease, gone dispatch process, and no session. The
policy is a pure function, so those cases are actually tested rather than hoped for.

**A task reaches `done` only when the daemon has run the gate itself.** Not when the
agent says so, and not when the router says so. The router runs gates too, but it is a
plugin inside the agent's own server, and the agent holds a bash tool — so anything the
router can report, the agent can report. Router verdicts are stored as `source=router`
and cannot promote anything; the daemon re-runs the committed gate out-of-loop once the
session has stopped, and only its `passed` writes `done`. Everything else — a failure, a
timeout, no committed gate, a vanished worktree — becomes `needs-review`.

Tested by playing the agent: wrong answer written, the local gate rewritten to
`exit 0`, and a hand-forged `orch gate-report --outcome passed`. The result was
`needs-review`, flagged `gate-modified`, with the real reason printed.

```
bin/orch gate                    # can this project's gates be trusted
bin/orch gate-run                # run the committed gate here and say why
bin/orch task list               # status plus who reached the last verdict
```

Not built yet: outcome routing, the phone-facing server over the tailnet, and
SSE-driven absence detection (the watchdog is poll-only, so `server.heartbeat` is
still unused).

62 tests: `for t in tests/test_*.py; do python3 "$t"; done`, plus `bun test`

## Layout

- `bin/` — dispatcher
- `hooks/` — task lifecycle hooks (`pre`/`post` create and cleanup, branch delete)
- `tasks/` — one folder per task, each with a `task.md`
- `TASKS.md` — the backlog and the protocol for working it
