# opencode-orchestrator

Tooling that dispatches opencode agents into git worktrees, verifies their work with a
project-owned gate, and merges it — unattended, with no database and no plugin.

**Read `docs/architecture.md` first.** This README is only an entry point.

## What this is

`orch` is a **globally installed tool** — one CLI and one daemon per machine. Projects
keep a small tracked `.orchestration/` directory holding only what that project knows
(the gate and config), and nothing is installed into the project's `.opencode/`.

The state is the repo. Tasks are derived from git — `tasks/<name>/task.md` declares a
task, a branch named `<name>` claims it, the worktree decides whether it is done.
Runtime facts come from `/proc` and the shared server. There is no SQLite, no registry,
no generated backlog file: nothing can drift because there is nothing stored.

## The loop

```
task.md committed (base)  →  branch exists (claimed)  →  agent dispatched
     →  session settles  →  daemon runs the session-idle gate
          → pass: the gate commits the work  →  merge + archive + teardown
          → fail:  daemon injects the failure back, bounded by retryCap
          → no-op: clean branch identical to base  →  removed + archived
     →  needs-review: dirty work, retries exhausted, or a missing worktree
```

The agent never commits and never decides completion. The `session-idle` hook — the
project's gate — verifies and, on a pass, commits the work. A clean, committed,
diffing branch is the sign of done, and only the daemon's own gate run can produce it.

## Usage

Stdlib-only Python, no install step. Agents run **headless**: one shared
`opencode serve` hosts every worktree, and the TUI is an optional viewer.

```bash
bin/orch init                    # scaffold .orchestration/ (config + gate template)
bin/orch gate                    # can this project's gate be trusted
bin/orch doctor                  # verify the contracts that fail silently

bin/orch server start|status|stop|restart
bin/orch task add <name...>      # scaffold tasks/<name>/task.md (no TASKS.md)
bin/orch task start <name>       # claim (branch+worktree), run hooks, dispatch
bin/orch task start <name> --dry-run
bin/orch task list               # render the backlog + status from the scan
bin/orch task reset <name>       # delete branch+worktree -> back to todo (a human act)
bin/orch task archive <name>     # close a reviewed needs-review task
bin/orch task merge <name>       # re-run a merge after a conflict

bin/orch observe                 # live state, read-only, exits 1 if you are needed
bin/orch observe -w --json
bin/orch attach                  # open a TUI onto the shared server, close it freely
bin/orch events -d 30 -v         # tail SSE, then print a histogram of event types

bin/orch install                 # systemd user unit, enabled, with linger
bin/orch uninstall                # remove config-level traces, keep projects alone
bin/orch daemon status            # is anything actually supervising
bin/orch daemon run --once --dry-run # decide and print, change nothing
```

`orch daemon` is one supervisor per machine covering every discovered project. It scans
`$HOME` and `/mnt` (env `ORCH_ROOTS`) for `.orchestration/` dirs, settles tasks, runs
gates, retriggers failing agents, merges verified branches, and archives done work.
Without it, nothing is ever settled or verified, so nothing reaches done — and
everything else still looks healthy, which is why `doctor` fails when it is missing.

Requires opencode **1.18.11 or newer**.

`observe` and `doctor` exit non-zero when something needs a human or when the
machinery itself is wrong, so they compose in a shell.

## Two things worth knowing before dispatching

**Commit the gate and the task file — on the base branch.** A worktree is created
*from* the base branch, so anything uncommitted, or committed on some other branch, is
invisible to the agent — and the daemon judges by the *committed* gate, never the
working-tree copy. An uncommitted task file gives the agent no instructions; an
uncommitted gate counts as no gate. `orch task start` refuses both, and `orch gate`
explains which one is wrong.

**The dispatch process exiting is not the task finishing.** Completion is derived from
the worktree state by the daemon, after the session settles. Watch `orch observe`.

**The daemon can dispatch `todo` tasks itself (opt-in).** Add a `scheduler` block to
`.orchestration/config.json` and the daemon fills free slots with the next unclaimed
declared task, in name order:

```json
"scheduler": { "enabled": true, "maxConcurrent": 1, "model": "opencode/…" }
```

Without it, dispatching stays a human act (`orch task start`). The scheduler never
dispatches ungated work.

## What works, and what does not

Working and exercised against live agents: `init`, `gate`, `doctor`, `server`,
`task add`/`start`/`list`/`reset`/`archive`/`merge`, `observe`, `attach`, `events`,
and the daemon — including the full loop of settle → gate → commit-on-pass → merge →
archive, the failure loop of gate-fail → inject → bounded retry → `needs-review`, the
no-op path of clean-identical → remove + archive, and the **scheduler** (auto-dispatch
of `todo` tasks, opt-in per project). A slow agent that spends minutes inside a tool is
held, not torn down: the daemon treats a live `orch:<task>` run process as the agent
working and only settles once it exits.

`orch daemon` supervises: it settles tasks, runs the committed gate itself, retriggers
failing agents up to `retryCap`, merges verified work, archives done work, restarts a
dead or unresponsive shared server, and flags blocked permissions. The policy is a pure
function of a snapshot, so those cases are tested rather than hoped for.

**Stopped, finished and done are different things, and all three are derived.** Stopped
means the session is idle. Finished means the gate loop settled — a passing gate commits
the work, a failing one drives the agent back until the retry cap is spent. Done means
a clean, committed, diffing branch passed the daemon's own gate and was merged. A task
with a dirty tree at settle time is `needs-review`, because a passing gate would have
committed it.

Not built yet: outcome routing and the phone-facing server, and a machine-wide
concurrency budget (per-project `scheduler.maxConcurrent` exists today).

Tests: `for t in tests/test_*.py; do python3 "$t"; done`.

## Layout

- `bin/` — dispatcher
- `src/orch/` — state (scan), dispatch, daemon, gate, merge, server, doctor, CLI
- `docs/` — architecture and decisions
- `tasks/` — one folder per task, each with a `task.md`
