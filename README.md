# opencode-orchestrator

Tooling that dispatches opencode agents into git worktrees, verifies their work with a
project-owned verification hook, and merges it — unattended, with no database and no plugin.

**Read `docs/architecture.md` first.** This README is only an entry point.

## What this is

`orch` is a **globally installed tool** — one CLI and one daemon per machine. Projects
keep a small tracked `.orchestration/` directory holding only what that project knows
(the verification hook, hook samples, and config), and nothing is installed into the project's
`.opencode/`.

The state is the repo. Tasks are derived from git — `tasks/<name>/task.md` declares a
task, a branch named `<name>` claims it, the worktree decides whether it is done.
Runtime facts come from `/proc` and the shared server. There is no SQLite, no registry,
no generated backlog file: nothing can drift because there is nothing stored.

## The loop

```
task.md committed (base)  →  branch exists (claimed)  →  agent dispatched
     →  session settles  →  daemon runs the session-idle verification hook
          → pass: the hook commits the work  →  merge + archive + teardown
          → fail:  daemon injects the failure back, bounded by retryCap
          → no-op: clean branch identical to base  →  removed + archived
     →  needs-review: dirty work, retries exhausted, or a missing worktree
```

The agent never commits and never decides completion. The `session-idle` hook — the
project's verification hook — verifies and, on a pass, commits the work. A clean, committed,
diffing branch is the sign of done, and only the daemon's own verification run can produce it.

## Usage

Stdlib-only Python, no install step. Agents run **headless**: one shared
`opencode serve` hosts every worktree.

```bash
bin/orch init                    # scaffold .orchestration/ (config + verification hook template + a hook sample per opencode event) and trust the project
bin/orch project create <name>    # new project in a supervised dir: git init + orch init + a private gh repo, pushed (or --name/--dir; asks when omitted; refuses outside the supervised roots)
bin/orch project trust [--dir]    # let the daemon verify/merge/dispatch this project (created projects are trusted automatically)
bin/orch project untrust [--dir]  # stop the daemon acting on it (worktrees are left alone)
bin/orch project list             # every discovered project and its trust status
bin/orch verify                  # can this project's verification hook be trusted
bin/orch verify-run              # run the committed verification hook here and report
bin/orch doctor                  # verify the contracts that fail silently

bin/orch server start|status|stop|restart
bin/orch task add <name...>      # scaffold tasks/<name>/task.md (no TASKS.md) and trust the project
bin/orch task draft "<text>"     # an agent converts free text into tasks/ specs and trusts the project (--file - for stdin)
bin/orch task start <name>       # claim (branch+worktree), run hooks, dispatch
bin/orch task start <name> --dry-run
bin/orch task list               # render the backlog + status from the scan
bin/orch task list --global      # every project: what is waiting on you (blocked, stuck, unmerged)
bin/orch task answer <name> allow|deny  # answer a blocked permission (or a question's text)
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
bin/orch daemon run --no-sse         # skip the /event stream consumer
bin/orch version                  # show the orch version
```

`orch daemon` is one supervisor per machine covering every discovered project. It scans
 `$HOME` and `/mnt` (env `ORCH_ROOTS`) for `.orchestration/` dirs, settles tasks, runs
 verification hooks, retriggers failing agents, merges verified branches, and archives done work.
Without it, nothing is ever settled or verified, so nothing reaches done — and
everything else still looks healthy, which is why `doctor` fails when it is missing.

**The daemon only *acts* on trusted projects.** Discovery is wide, but authority is
trust-bound: a project the machine owner has not approved (`orch project trust`) is never
verified, merged, scheduled or resolved — it only raises an `alert` saying so. The marker
lives in `~/.local/state/orch/trusted.json`, outside the repo, so a project cannot
approve itself. A repo you merely clone can never auto-run its verification hook. `orch init`,
`orch project create`, `orch task add`, and `orch task draft` all trust the project
they scaffold work into (each is the owner acting); `doctor` fails on an untrusted
project.

Requires opencode **1.18.11 or newer** — see Prerequisites.

`doctor` exits non-zero when the machinery itself is wrong, so it composes in a shell.

## Prerequisites

- **Linux.** orch reads `/proc` for runtime facts and drives detached processes, and
  assumes a POSIX shell for hooks.
- **Python 3.10+** — stdlib only, no install step (`bin/orch` is a plain script).
- **git** — worktrees, branches, and the bare repo *are* the state store.
- **opencode 1.18.11 or newer** on `PATH` — agents run headless against one shared
  `opencode serve`; `doctor` checks the live version against the floor.

## Optional technologies

- **systemd user session** (`systemctl --user`, `loginctl`) — only `orch install` /
  `uninstall` use it: the always-on supervisor that survives logout and reboot.
  Without it nothing settles unless you keep `orch daemon run` in the foreground.
- **gh (GitHub CLI), authenticated** — only `orch project create`'s default
  create-and-push-a-private-repo path; `--no-gh` skips it.
- **fd** — faster project discovery; orch falls back to `find` when absent.
- **shellcheck** — without it verification hooks degrade to syntax-only validation and `doctor`
  warns.
- **Provider API keys** — opencode needs a configured provider to run agents; the env
  filter deliberately lets `*_API_KEY` / `*_AUTH_TOKEN` through.

## The primary agent (`orch ask`)

Separate from tasks, every project can have **one primary agent**: a persistent
conversation attached to the project's base worktree on the shared server, found
live by its `orch:agent` session title. `orch ask` creates it on first use and
keeps talking to the same session, so it remembers the conversation. You can ask
it questions, have it change files, fire a message and check later (`--async`),
and compact the conversation when context grows — `--compact` runs **opencode's
own compaction** (`POST /session/{id}/summarize`), not a stand-in.

The primary agent works in the base worktree: its edits stay uncommitted for you
to review and commit, and the daemon never verifies, merges, or tears it down — it
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
base worktree with the fail-closed verification hook; copy `.orchestration/hooks/session-idle.sample`
to `hooks/session-idle` and edit it to real checks before dispatching, and keep the
project under `$HOME`/`/mnt`
(`ORCH_ROOTS`) so the daemon supervises it.

## Two things worth knowing before dispatching

**Commit the verification hook and the task file — on the base branch.** A worktree is created
*from* the base branch, so anything uncommitted, or committed on some other branch, is
invisible to the agent — and the daemon judges by the *committed* hook, never the
working-tree copy. An uncommitted task file gives the agent no instructions; an
uncommitted hook counts as no verification (absence of a hook is the implicit
unverified state — tasks run without checks). The task-lifecycle hooks (`pre-task-create` and friends)
follow the same rule: only the copy committed on the base branch runs, so commit them
before dispatching.

**The dispatch process exiting is not the task finishing.** Completion is derived from
the worktree state by the daemon, after the session settles. Watch `orch task list`.

**The daemon can dispatch `todo` tasks itself (opt-in).** Add a `scheduler` block to
`.orchestration/config.json` and the daemon fills free slots with the next unclaimed
declared task whose `dependencies:` are satisfied, in name order:

```json
"scheduler": { "enabled": true, "maxConcurrent": 1 }
```

**Ordering is the `dependencies:` front matter.** A `tasks/<name>/task.md` declares
the tasks that block it as slugs — `dependencies: [spec, auth-migrate]`. A `todo` task
dispatches only when every listed dependency is archived (or was never declared); an
open declared task always holds its dependents. The frontier — everything grabbable now —
is the all-dependencies-archived set, derived from the repo, never stored. `orch doctor`
fails on a dependency slug that is neither declared nor archived (a typo would block the
task forever).

The model is opencode's choice, not orch's — the scheduler just spawns the agent.
Without it, dispatching stays a human act (`orch task start`). A task opts the human
back into the loop with `wait_human_start: true` in its task.md front matter; the
scheduler only leaves those alone and never dispatches in an untrusted project. A
`wait_human_merge: true` task is verified and committed but not merged — the human
approves it with `orch task merge`.

## Global hooks (machine-level notifications)

Same contract as the project hooks — named after what happened, executable, fired
with an env-var contract — but global: they live in `~/.config/orch/hooks/`, belong
to the machine owner, and no project agent can touch them. `orch init` writes an
inert `*.example` template for every event; copy one to its real name and
`chmod +x`, and the daemon and CLI fire it.

Besides the ten machine events below, `orch init` also scaffolds a
`*.example` per opencode server event (the vocabulary in `src/orch/opencode_events.py`,
dots mapped to dashes — `session.idle` → `session-idle.example`). The daemon's
SSE consumer reads the server's `GET /event` stream and fires a copied
executable hook for each event, with the event's JSON on stdin and
`ORCH_EVENT=<dashed-name>` set — a session-scoped event also fires its project
hook in the owning worktree.

They are **advisory and fire-and-forget**: each hook is spawned detached (a slow
hook never blocks a daemon pass) and a non-zero exit — or a missing hook — changes
nothing. Hook output goes to `~/.local/state/orch/hooks/<event>.log`.

| Event | Fired when |
| --- | --- |
| `alert` | a task is blocked on a permission/question, or a project is untrusted |
| `needs-review` | a task needs a human (dirty work, retries exhausted, conflict, missing worktree) |
| `merged` | verified work merged into base (daemon verification pass or `orch task merge`) |
| `removed` | a no-op branch cleaned up |
| `dispatched` | a task agent dispatched (`orch task start` or the scheduler) |
| `dispatch-failed` | a dispatch failed |
| `resolve-failed` | automatic conflict resolution failed |
| `restart-server` | the shared server restarted |
| `reset` | `orch task reset` |
| `archived` | `orch task archive` |

Every global hook runs with the full environment (they are the machine owner's own
code); project hooks run with the filtered environment (a project can opt into the
full env with `"verify": {"env": "full"}`). All of them get `ORCH_EVENT`,
`ORCH_ACTION`, `PROJECT_NAME`, `PROJECT_DIR`, and — when task-scoped — `TASK_NAME`,
`BRANCH_NAME`, `WORKTREE_PATH`, `WORKTREE_NAME`, `BASE_BRANCH`, plus
`ORCH_REASON`/`ORCH_DETAIL` (and `ORCH_REQUEST_ID`/`ORCH_SESSION_ID`/`ORCH_KIND`
for blocked alerts).

## Security model

This tool's job is to run LLM agents that execute code on your machine, unattended, so
the honest question is what the *controls* are — the threat model is still a lazy agent,
not a hostile one, but the blast radius is bounded:

- **Authority is trust-bound.** The daemon only verifies/merges/dispatches/resolves in
  projects you approved (`orch project trust`); anything else only alerts. Discovery
  (`$HOME`, `/mnt`, `ORCH_ROOTS`) stays wide so you can see everything, but nothing
  auto-runs on a repo you merely cloned. `doctor` flags an untrusted project and warns
  when `ORCH_ROOTS` includes `/`.
- **The verification hook is the committed base copy, and agents cannot edit it.** The daemon runs
  only the `session-idle` hook committed on the base branch; a working-tree edit shows
  up as `diverged`. `orch install` additionally writes machine-level opencode permission
  rules (`~/.config/opencode/opencode.json`) denying `edit`/`write` on
  `.orchestration/**` and on that config itself, so an agent cannot silently rewrite its
  own verification hook through its file tools.
- **Agents, verification hooks, and project hooks get a filtered environment.** Secrets that no
  agent, verification hook, or project hook needs (`SSH_AUTH_SOCK`, cloud credentials,
  `*_PASSWORD`/`*_SECRET`/…, `GH_TOKEN`, `DATABASE_URL`, `KUBECONFIG`, …) are dropped
  before `opencode run`, a verification hook, or a project hook runs — including the task lifecycle
  hooks. Provider keys (`*_API_KEY`, `*_AUTH_TOKEN`) survive — opencode needs them.
  The machine owner relaxes via `~/.config/orch/env.json` (`drop`/`keep`); a project
  can only *restrict* further (`"agentEnv": {"drop": [...]}`) and a verification hook — or a project
  hook — can opt into the full env with `"verify": {"env": "full"}`.
- **Permissions are one-time.** `orch task answer` approves a blocked permission
  once; nothing is persisted to opencode's runtime config.
- **The control plane is loopback-only and the logs are private.** The shared server
  binds `127.0.0.1`; state and session logs live in `~/.local/state/orch` created
  `0700` with `0600` files, so other users cannot read conversations or the server port.

Same-user compromise is out of scope: anything that already runs as you can read your
files and drive the agents. If you need containment against hostile agents, run them in
an OS-level sandbox — orch does not provide one.

## What works, and what does not

Working and exercised against live agents: `init`, `verify`, `doctor`, `server`,
`project create`/`trust`/`untrust`/`list`, `ask`, and `task add`/`draft`/`start`/`list`/
`answer`/`reset`/`archive`/`merge`,
and the daemon — including the full loop of settle → verify → commit-on-pass → merge →
archive, the failure loop of verify-fail → inject → bounded retry → `needs-review`, the
no-op path of clean-identical → remove + archive, and the **scheduler** (auto-dispatch
of `todo` tasks, opt-in per project and trust-bound). A slow agent that spends minutes
inside a tool is
held, not torn down: the daemon treats a live detached `opencode run` process in the
worktree as the agent working and only settles once it exits.

The security controls are covered by unit tests: the **trust boundary** (untrusted projects
only alert), the **env filter** (agents and verification hooks run without the owner's secrets), and
the **`.orchestration` deny** installed by `orch install`.

`orch daemon` supervises: it settles tasks, runs the committed verification hook itself, retriggers
failing agents up to `retryCap`, merges verified work, archives done work, restarts a
dead or unresponsive shared server, flags blocked permissions, and consumes the shared
server's `/event` SSE stream to fire per-event hooks. A **merge conflict**
is left in progress and the task reads `needs-review (merge-conflict)`. Who resolves it
follows `wait_human_start`: a task a human had to start needs the human to resolve it
in the base worktree (`git add`/`git commit`); a machine-started task gets an agent
dispatched into the base
worktree to resolve and commit on its own (up to `retryCap` tries, then it escalates to
a human). Either way, once the merge lands the daemon archives the task. The policy is
a pure function of a snapshot, so those cases are tested rather than hoped for.

**Stopped, finished and done are different things, and all three are derived.** Stopped
means the session is idle. Finished means the verification loop settled — a passing hook commits
the work, a failing one drives the agent back until the retry cap is spent. Done means
a clean, committed, diffing branch passed the daemon's own verification and was merged. A task
with a dirty tree at settle time is `needs-review`, because a passing verification hook would have
committed it.

Not built yet: routing `needs-review` onwards or unblocking a parent agent.

The per-event hooks are live: the daemon's SSE consumer reads the server's
`GET /event` stream and fires a copied, executable hook per event type in
`src/orch/opencode_events.py` (project `hooks/*.sample` and global
`hooks/*.example`), with the event JSON on stdin.

When an agent needs you, `orch` *exposes* it — `orch task list` shows blocked and
`needs-review` tasks from the scan, `orch task list --global` gathers that from
every supervised project into one waiting-on-you report (blocked, merge conflicts,
missing/dirty worktrees, awaiting-merge tasks, and unmerged work),
`orch task answer` lets you reply to a blocked
permission or question directly, and the daemon journal logs every `alert:` and
`needs-review`. Notify add-ons integrate **with** `orch`, not the other way around:
the phone add-on in `~/stuff/phone-approval` (`notify-phone` → Termux) polls
`orch task list --json` or tails the journal, and only it knows about the phone.

Tests: `for t in tests/test_*.py; do python3 "$t"; done`.

The opencode-event hook samples are covered by unit tests too: `tests/test_opencode_events.py`
sanity-checks the catalog — well-formed event names, unique dots-to-dashes hook names, and no
collisions with the lifecycle or global hooks. The SSE consumer itself is covered by
`tests/test_sse.py`: stream parsing, global + project hook firing, session routing, and the
session-idle verification hook never firing from the stream.

## Layout

- `bin/` — dispatcher
- `src/orch/` — state (scan), dispatch, daemon, verify, merge, server, doctor, CLI,
  `opencode_events.py` (the server-event catalog), and `sse.py` (the daemon's
  `/event` stream consumer that fires per-event hooks)
- `docs/` — architecture and decisions
- `tasks/` — one folder per task, each with a `task.md`
