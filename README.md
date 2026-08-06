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
`opencode serve` hosts every worktree.

```bash
bin/orch init                    # scaffold .orchestration/ (config + gate template + hook samples)
bin/orch gate                    # can this project's gate be trusted
bin/orch doctor                  # verify the contracts that fail silently

bin/orch server start|status|stop|restart
bin/orch task add <name...>      # scaffold tasks/<name>/task.md (no TASKS.md)
bin/orch task add <name> --assist "<intent>"  # draft the spec with an opencode agent
bin/orch task import list.txt    # bulk-create specs from a list (or `-` for stdin)
bin/orch task start <name>       # claim (branch+worktree), run hooks, dispatch
bin/orch task start <name> --dry-run
bin/orch task list               # render the backlog + status from the scan
bin/orch task answer <name> allow|deny  # answer a blocked permission (or a question's text)
bin/orch task reset <name>       # delete branch+worktree -> back to todo (a human act)
bin/orch task archive <name>     # close a reviewed needs-review task
bin/orch task merge <name>       # re-run a merge after a conflict

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

`doctor` exits non-zero when the machinery itself is wrong, so it composes in a shell.

## Two things worth knowing before dispatching

**Commit the gate and the task file — on the base branch.** A worktree is created
*from* the base branch, so anything uncommitted, or committed on some other branch, is
invisible to the agent — and the daemon judges by the *committed* gate, never the
working-tree copy. An uncommitted task file gives the agent no instructions; an
uncommitted gate counts as no gate. `orch task start` refuses both, and `orch gate`
explains which one is wrong.

**The dispatch process exiting is not the task finishing.** Completion is derived from
the worktree state by the daemon, after the session settles. Watch `orch task list`.

**The daemon can dispatch `todo` tasks itself (opt-in).** Add a `scheduler` block to
`.orchestration/config.json` and the daemon fills free slots with the next unclaimed
declared task, in name order:

```json
"scheduler": { "enabled": true, "maxConcurrent": 1 }
```

The model is opencode's choice, not orch's — the scheduler just spawns the agent.
Without it, dispatching stays a human act (`orch task start`). The scheduler never
dispatches ungated work, and it never dispatches a task you didn't opt in: a task
needs a human by default (`hitl: true` in the task.md front matter); only `hitl: false`
— `orch task add --auto` — lets the machine pick it.

## What works, and what does not

Working and exercised against live agents: `init`, `gate`, `doctor`, `server`,
`task add`/`start`/`list`/`answer`/`reset`/`archive`/`merge`,
and the daemon — including the full loop of settle → gate → commit-on-pass → merge →
archive, the failure loop of gate-fail → inject → bounded retry → `needs-review`, the
no-op path of clean-identical → remove + archive, and the **scheduler** (auto-dispatch
of `todo` tasks, opt-in per project). A slow agent that spends minutes inside a tool is
held, not torn down: the daemon treats a live detached `opencode run` process in the
worktree as the agent working and only settles once it exits.

`orch daemon` supervises: it settles tasks, runs the committed gate itself, retriggers
failing agents up to `retryCap`, merges verified work, archives done work, restarts a
dead or unresponsive shared server, and flags blocked permissions. A **merge conflict**
is left in progress and the task reads `needs-review (merge-conflict)`. Who resolves it
follows the `hitl` flag: a `hitl: true` task needs a human in the base worktree
(`git add`/`git commit`); a `hitl: false` task gets an agent dispatched into the base
worktree to resolve and commit on its own (up to `retryCap` tries, then it escalates to
a human). Either way, once the merge lands the daemon archives the task. The policy is
a pure function of a snapshot, so those cases are tested rather than hoped for.

**Stopped, finished and done are different things, and all three are derived.** Stopped
means the session is idle. Finished means the gate loop settled — a passing gate commits
the work, a failing one drives the agent back until the retry cap is spent. Done means
a clean, committed, diffing branch passed the daemon's own gate and was merged. A task
with a dirty tree at settle time is `needs-review`, because a passing gate would have
committed it.

Not built yet: routing `needs-review` onwards or unblocking a parent agent, and a
machine-wide concurrency budget (per-project `scheduler.maxConcurrent` exists today).

When an agent needs you, `orch` *exposes* it — `orch task list` shows blocked and
`needs-review` tasks from the scan, `orch task answer` lets you reply to a blocked
permission or question directly, and the daemon journal logs every `alert:` and
`needs-review`. Notify add-ons integrate **with** `orch`, not the other way around:
the phone add-on in `~/stuff/phone-approval` (`notify-phone` → Termux) polls
`orch task list --json` or tails the journal, and only it knows about the phone.

Tests: `for t in tests/test_*.py; do python3 "$t"; done`.

## Layout

- `bin/` — dispatcher
- `src/orch/` — state (scan), dispatch, daemon, gate, merge, server, doctor, CLI
- `docs/` — architecture and decisions
- `tasks/` — one folder per task, each with a `task.md`
