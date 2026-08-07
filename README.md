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
bin/orch init                    # scaffold .orchestration/ (config + gate template + hook samples) and trust the project
bin/orch project create <name>    # new project in a supervised dir: git init + orch init + a private gh repo, pushed (or --name/--dir; asks when omitted; refuses outside the supervised roots)
bin/orch project trust [--dir]    # let the daemon gate/merge/dispatch this project (created projects are trusted automatically)
bin/orch project untrust [--dir]  # stop the daemon acting on it (worktrees are left alone)
bin/orch project list             # every discovered project and its trust status
bin/orch gate                    # can this project's gate be trusted
bin/orch gate-run                # run the committed gate here and report
bin/orch doctor                  # verify the contracts that fail silently

bin/orch server start|status|stop|restart
bin/orch task add <name...>      # scaffold tasks/<name>/task.md (no TASKS.md) and trust the project
bin/orch task draft "<text>"     # an agent converts free text into tasks/ specs and trusts the project (--file - for stdin)
bin/orch task start <name>       # claim (branch+worktree), run hooks, dispatch
bin/orch task start <name> --dry-run
bin/orch task list               # render the backlog + status from the scan
bin/orch task answer <name> allow|deny  # answer a blocked permission (or a question's text)
bin/orch task answer <name> allow --preseed  # also record it as an always-rule in .orchestration/config.json
bin/orch task reset <name>       # delete branch+worktree -> back to todo (a human act)
bin/orch task archive <name>     # close a reviewed needs-review task
bin/orch task merge <name>       # re-run a merge after a conflict

bin/orch ask "<message>"         # converse with the project's primary agent (created on first use)
bin/orch ask --async "<message>" # fire-and-forget: send and return immediately
bin/orch ask --compact [msg]     # run opencode's own compaction, then optionally ask
bin/orch ask --status            # is the primary agent idle, busy, or blocked

bin/orch install                 # systemd user unit, enabled, with linger
bin/orch uninstall                # remove config-level traces, keep projects alone
bin/orch daemon status            # is anything actually supervising
bin/orch daemon run --once --dry-run # decide and print, change nothing
bin/orch version                  # show the orch version
```

`orch daemon` is one supervisor per machine covering every discovered project. It scans
`$HOME` and `/mnt` (env `ORCH_ROOTS`) for `.orchestration/` dirs, settles tasks, runs
gates, retriggers failing agents, merges verified branches, and archives done work.
Without it, nothing is ever settled or verified, so nothing reaches done — and
everything else still looks healthy, which is why `doctor` fails when it is missing.

**The daemon only *acts* on trusted projects.** Discovery is wide, but authority is
gated: a project the machine owner has not approved (`orch project trust`) is never
gated, merged, scheduled or resolved — it only raises an `alert` saying so. The marker
lives in `~/.local/state/orch/trusted.json`, outside the repo, so a project cannot
approve itself. A repo you merely clone can never auto-run its gate. `orch init`,
`orch project create`, `orch task add`, and `orch task draft` all trust the project
they scaffold work into (each is the owner acting); `doctor` fails on an untrusted
project.

Requires opencode **1.18.11 or newer**.

`doctor` exits non-zero when the machinery itself is wrong, so it composes in a shell.

## The primary agent (`orch ask`)

Separate from tasks, every project can have **one primary agent**: a persistent
conversation attached to the project's base worktree on the shared server, found
live by its `orch:agent` session title. `orch ask` creates it on first use and
keeps talking to the same session, so it remembers the conversation. You can ask
it questions, have it change files, fire a message and check later (`--async`),
and compact the conversation when context grows — `--compact` runs **opencode's
own compaction** (`POST /session/{id}/summarize`), not a stand-in.

The primary agent works in the base worktree: its edits stay uncommitted for you
to review and commit, and the daemon never gates, merges, or tears it down — it
supervises only task worktrees. `--status` reports whether it is idle, busy, or
blocked on a permission/question.

## Creating a project from scratch

`orch project create <name>` (or `--name <name>`, or neither: it asks) is the
from-zero path: it `git init`s a repo on `main`, runs `orch init` inside it,
commits the base, and (by default) creates a **private** GitHub repo and pushes —
`gh` must be installed and authenticated or the command aborts (`--no-gh` creates
locally only, `--public` flips visibility, `--dir <parent>` chooses the parent,
prompting for it too when omitted). On a terminal every omitted flag is asked:
name, parent directory, whether to create a GitHub repo, visibility, description,
and — if the parent is outside a supervised root — whether to create it anyway.
The parent must otherwise be inside a supervised root
(`$HOME`, `/mnt`, or `ORCH_ROOTS`) — a project the daemon cannot discover would
never settle, so create refuses and says so; `--allow-unsupervised` creates one
anyway, and `ORCH_ROOTS=/` supervises the whole filesystem. Then
`orch task draft "<free text>"` has a
one-shot agent convert an idea — or a whole pasted spec — into `tasks/<name>/task.md`
specs for you to review and commit. Both commands leave the project on the
base worktree with the fail-closed gate; edit `.orchestration/hooks/session-idle`
to real checks before dispatching, and keep the project under `$HOME`/`/mnt`
(`ORCH_ROOTS`) so the daemon supervises it.

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
— `orch task add --auto` or `orch task draft --auto` — lets the machine pick it.
The scheduler also never dispatches in an untrusted project.

## Security model

This tool's job is to run LLM agents that execute code on your machine, unattended, so
the honest question is what the *controls* are — the threat model is still a lazy agent,
not a hostile one, but the blast radius is bounded:

- **Authority is trust-gated.** The daemon only gates/merges/dispatches/resolves in
  projects you approved (`orch project trust`); anything else only alerts. Discovery
  (`$HOME`, `/mnt`, `ORCH_ROOTS`) stays wide so you can see everything, but nothing
  auto-runs on a repo you merely cloned. `doctor` flags an untrusted project and warns
  when `ORCH_ROOTS` includes `/`.
- **The gate is the committed base copy, and agents cannot edit it.** The daemon runs
  only the `session-idle` hook committed on the base branch; a working-tree edit shows
  up as `diverged`. `orch install` additionally writes machine-level opencode permission
  rules (`~/.config/opencode/opencode.json`) denying `edit`/`write` on
  `.orchestration/**` and on that config itself, so an agent cannot silently rewrite its
  own gate through its file tools.
- **Agents and gates get a filtered environment.** Secrets that no agent or gate needs
  (`SSH_AUTH_SOCK`, cloud credentials, `*_PASSWORD`/`*_SECRET`/…, `GH_TOKEN`,
  `DATABASE_URL`, `KUBECONFIG`, …) are dropped before `opencode run` or a gate runs.
  Provider keys (`*_API_KEY`, `*_AUTH_TOKEN`) survive — opencode needs them. The machine
  owner relaxes via `~/.config/orch/env.json` (`drop`/`keep`); a project can only
  *restrict* further (`"agentEnv": {"drop": [...]}`) and a gate can opt into the full
  env with `"gate": {"env": "full"}`.
- **Permissions are one-time, or orchestration-level.** `orch task answer` approves a
  blocked permission once — nothing is persisted to opencode's runtime config. To make a
  rule stick, `--preseed` records it in `.orchestration/config.json`
  (`permissions.always`) for you to review and commit; only the committed copy applies.
- **The control plane is loopback-only and the logs are private.** The shared server
  binds `127.0.0.1`; state and session logs live in `~/.local/state/orch` created
  `0700` with `0600` files, so other users cannot read conversations or the server port.

Same-user compromise is out of scope: anything that already runs as you can read your
files and drive the agents. If you need containment against hostile agents, run them in
an OS-level sandbox — orch does not provide one.

## What works, and what does not

Working and exercised against live agents: `init`, `gate`, `doctor`, `server`,
`project create`/`trust`/`untrust`/`list`, `ask`, and `task add`/`draft`/`start`/`list`/
`answer`/`reset`/`archive`/`merge`,
and the daemon — including the full loop of settle → gate → commit-on-pass → merge →
archive, the failure loop of gate-fail → inject → bounded retry → `needs-review`, the
no-op path of clean-identical → remove + archive, and the **scheduler** (auto-dispatch
of `todo` tasks, opt-in per project and trust-gated). A slow agent that spends minutes
inside a tool is
held, not torn down: the daemon treats a live detached `opencode run` process in the
worktree as the agent working and only settles once it exits.

The security controls are covered by unit tests: the **trust gate** (untrusted projects
only alert), the **env filter** (agents and gates run without the owner's secrets), the
**`.orchestration` deny** installed by `orch install`, and the **permission preseed**
(`task answer --preseed`).

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
