# opencode-orchestrator

Tooling that dispatches opencode agents into git worktrees, verifies their work with a
project-owned verification hook, and merges it — unattended, with no database and no plugin.

**Read `docs/architecture.md` first.** This README is only an entry point.

## What this is

`orch` is a **globally installed tool** — one CLI and one daemon per machine. Projects
keep a small tracked `.orchestration/` directory holding only what that project knows
(the verification hook and config), and nothing is installed into the project's
`.opencode/`.

The state is the repo. Tasks are derived from git — `tasks/<name>/task.md` declares a
task, a branch named `<name>` claims it, the worktree decides whether it is done.
Runtime facts come from `/proc` and the shared opencode service. There is no SQLite, no registry,
no generated backlog file: nothing can drift because there is nothing stored.

## The loop

```
task.md committed (base)  →  branch exists (claimed)  →  agent dispatched
     →  agent commits its work to the branch  →  session settles
     →  daemon runs the session-idle verification hook
          → pass: the hook commits any stragglers  →  merge + archive + teardown
          → fail:  daemon injects the failure back, bounded by retryCap
          → empty: branch committed nothing since its claim  →  held for review
     →  needs-review: dirty work, an empty branch, retries exhausted, a missing
        worktree, or a turn the user interrupted in the TUI (held, never auto-merged)
```

**Done is a git state, and git is the only state.** The working agent declares the task
done by committing its work to the branch — a committed, clean, diffing branch is the sign
of done, and nothing is stored anywhere else to say so. The agent decides when it believes
the work is done and commits; the daemon decides whether the task is *accepted*: it runs the
committed `session-idle` verification hook (which, on a pass, also idempotently commits any
files the agent left uncommitted) and merges only a branch that passed its own verification
run. A dirty branch at settle time is simply work the agent did not commit.

## The execution flow, start to finish

Walking one task through the whole loop, from a raw idea to archived work:

1. **Intake.** An opencode session in the base worktree — usually `orch attach`'s
   primary agent — turns the idea into `tasks/<name>/task.md` by calling the
   machine-global `create_task` custom tool. When the spec is written with
   `wait_human_start: false` it is committed straight to the base branch, so the
   daemon sees it and the rest happens unattended; with `wait_human_start: true`
   it stays an uncommitted draft until you review it.
2. **Claim and dispatch.** Either the scheduler (opt-in per project) picks the task
   because its `dependencies:` are all archived, or you run `orch task start <name>`.
   Dispatch refuses if `refs/heads/<name>` already exists — the branch is the claim,
   atomically, so two dispatches can never race. A worktree is created
   (`git worktree add -b <name> <base>`), the `pre-task-create`/`post-task-create`
   hooks run, and a session is opened on the shared opencode service bound to that worktree,
   fed the task prompt via `prompt`. No `opencode run` subprocess is spawned:
   the agent *is* the session.
3. **The agent works.** It reads the spec, edits the worktree, and commits when it
   believes it is done. If it needs a permission it *blocks* on a pending ask rather
   than auto-answering — surfaced to you through `orch permissions` /
   `orch task answer`. The daemon watches every session and classifies it each cycle:
   **busy** (working), **blocked** (waiting on a human), **interrupted** (a turn the
   user stopped in the TUI), or **idle** (nothing running).
4. **Settle.** Once the session is idle past the settle budget, the daemon derives the
   outcome from the worktree — it does not ask the agent. An interrupted turn with work,
   a missing worktree, or a dirty tree that keeps failing verification lands on
   `needs-review` for you. So does a claimed branch whose tip is still base's tip: it
   committed nothing since its claim — the shape a turn-1 crash, a quota stop or a
   provider error leaves — so the worktree and branch are kept and the spec stays in
   `tasks/`, never harvested as an empty run. Everything else is handed to the
   committed `session-idle` verification hook.
5. **Verify and merge.** The daemon runs the verification hook committed on the base
   branch (never the agent's working-tree copy). On a pass the hook also idempotently
   commits any files the agent left uncommitted, and the daemon merges the branch
   `--no-ff` into base, then removes the worktree and branch and archives the spec. On
   a fail the failure is injected back into the agent's session via `prompt` and
   it retries, bounded by `retryCap`; once the cap is spent the task is `needs-review`.
   A project with no committed hook accepts work implicitly — absence of a hook is the
   opt-in.
6. **Conflict.** A merge that conflicts is left **in progress** (the scan reads
   `MERGE_HEAD` and shows `needs-review (merge-conflict)`); it is never silently
   retried. Who resolves it follows `wait_human_start`: a human-started task is resolved
   by you in the base worktree (`git add`/`git commit`); a machine-started task gets an
   agent dispatched into the base worktree — a server session titled
   `<name>-merge-conflict` — to resolve and commit on its own, up to `retryCap` tries
   before it escalates to you. The daemon detects an in-flight resolver by that session,
   so it never spawns duplicates. Either way, once the merge lands the branch tip is
   base's tip, so the daemon can no longer tell the resolved conflict from an empty
   run: it holds the task for you to review and archive (`orch task archive`).
7. **Archive.** `tasks/<name>/task.md` is `git mv`'d to `archive/<name>/task.md` (a
   reused slug whose earlier generation still occupies that path is archived to
   `archive/<name>/<utc-ts>/task.md` instead) and
   committed. Archiving is what unblocks any task that listed `<name>` in its
   `dependencies:` — the scheduler's frontier is exactly the set of declared tasks
   whose every dependency is archived.

Because every step is derived from git and the live server — never from stored verdicts —
the loop is restart-safe: a daemon restart resets only the in-memory timers, and a task
still lands where its worktree says it should. One pass handles every claimed task in every
trusted project; the scheduler fills free slots with the next eligible `todo`.

## Usage

Stdlib-only Python, no dependency install step. `orch install` copies the
whole tree (minus `.git`) to one canonical path (`$XDG_DATA_HOME/orch`,
default `~/.local/share/orch`) and puts a real launcher on `PATH` at
`~/.local/bin/orch`, so `orch` resolves machine-wide even when no checkout is
present. Agents run **headless**: one shared opencode background service hosts
every worktree.

```bash
orch init                     # scaffold .orchestration/ (config + docs + ignore rules + inert hook .example references) and install the machine-level hook templates; takes the project's trust
orch project create <name>    # new project in a supervised dir: git init + orch init + a private gh repo, pushed (or --name/--dir; asks when omitted; refuses outside the supervised roots)
orch project clone <url>      # clone an existing repo into orch's layout (bare store + base worktree); initializes it if it has no .orchestration, otherwise trusts only with --trust
orch project trust [--dir]    # let the daemon verify/merge/dispatch this project (created projects are trusted automatically)
orch project untrust [--dir]  # stop the daemon acting on it (worktrees are left alone)
orch project list             # every discovered project and its trust status
orch project base <branch>    # repoint an existing project's base branch to <branch> (must already exist)
orch verify                 # can this project's verification hook be trusted
orch verify --run           # run the committed verification hook here and report
orch doctor                  # verify the contracts that fail silently

orch server start|status|stop|restart
orch task add <name...>      # scaffold tasks/<name>/task.md (no TASKS.md) and trust the project
orch task start <name>       # claim (branch+worktree), run hooks, dispatch
orch task start <name> --dry-run
orch task list               # newest 10 todo/running tasks from the scan
orch task list --status all --limit 0   # the whole scan, incl. needs-review/ready/archived
orch task list --global      # every project: what is waiting on you (blocked, stuck, unmerged)
orch task answer <name> allow|deny  # answer a blocked permission (or a question's text)
orch task reset <name>       # delete branch+worktree -> back to todo (a human act)
orch task archive <name>     # close a reviewed needs-review task
orch task merge <name>       # re-run a merge after a conflict
                             # (from the task's own worktree, omit <name> to merge that task)

orch attach                  # attach the interactive TUI to the primary agent, or to a task's session from its worktree (created on first use)

orch permissions             # approve/deny pending permission asks interactively (auto-picks the project you're in; --global picks a project first, then a task)
orch permissions --task <name> allow|deny [--reason "..."]   # scriptable one-shot for one task (a deny explains itself to the agent)
orch permissions -C <dir>    # non-interactive: list the project's pending asks (a flag makes it non-interactive)
orch permissions --json      # list every pending permission ask, machine-readable

orch install                 # copy orch to ~/.local/share/orch, write a ~/.local/bin/orch launcher, then the systemd user unit, enabled, with linger
orch uninstall                # remove the launcher and the install dir, plus config-level traces; keep projects alone
orch daemon status            # is anything actually supervising
orch daemon run --once --dry-run # decide and print, change nothing
orch daemon run --no-sse         # skip the /api/event stream consumer
orch version                  # show the orch version
```

`orch daemon` is one supervisor per machine covering every discovered project. It scans
 `$HOME`, `/mnt`, and orch's dev root `/tmp/opencode` (env `ORCH_ROOTS` overrides) for `.orchestration/` dirs, settles tasks, runs
 verification hooks, retriggers failing agents, merges verified branches, and archives done work.
Without it, nothing is ever settled or verified, so nothing reaches done — and
everything else still looks healthy, which is why `doctor` fails when it is missing.

**The daemon only *acts* on trusted projects.** Discovery is wide, but authority is
trust-bound: a project the machine owner has not approved (`orch project trust`) is never
verified, merged, scheduled or resolved — it only raises an `alert` saying so. The marker
lives in `~/.local/state/orch/trusted.json`, outside the repo, so a project cannot
approve itself. A repo you merely clone can never auto-run its verification hook. `orch init`,
`orch project create`, and `orch task add` all trust the project
they scaffold work into (each is the owner acting); `doctor` fails on an untrusted
project.

Requires opencode **2.0 or newer** — see Prerequisites.

`doctor` exits non-zero when the machinery itself is wrong, so it composes in a shell.

## Prerequisites

- **Linux.** orch reads `/proc` for runtime facts and drives detached processes, and
  assumes a POSIX shell for hooks.
- **Python 3.10+** — stdlib only; `orch install` makes the tool global (a copy at
  `~/.local/share/orch` plus a `~/.local/bin/orch` launcher), but `bin/orch` stays a
  plain script that works straight from a checkout.
- **git** — worktrees, branches, and the bare repo *are* the state store.
- **opencode 2.0 or newer** on `PATH` — agents run headless against the one
  shared opencode background service (`opencode serve --service`); `doctor`
  checks the live version against the floor.

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

## The primary agent (`orch attach`)

Separate from tasks, every project can have **one primary agent**: a persistent
conversation attached to the project's base worktree on the shared opencode
service, found live by its `orch:agent` session title. `orch attach` creates it
on first use, then `exec`s opencode's own TUI pointed straight at that session
(`opencode <base> --session <id>`) — so the conversation remembers everything,
and you type in the same interface you always use instead of a CLI-side shim.
Compaction, streaming, and every other conversation feature are opencode's own;
orch only resolves the project's service and session for you.

`orch attach` is context-sensitive: run from a **claimed task worktree** and it
attaches to that task's session instead (creating it on first use, exactly like
the primary agent) — the task's session is titled by the task slug and bound to
the task worktree, so you can drive the task's agent and answer its asks in the
same TUI. Run from the base worktree, or anywhere outside a task worktree, and it
attaches to the primary agent as above. Running it somewhere that is not a
project worktree at all is refused.

The primary agent works in the base worktree: its edits stay uncommitted for you
to review and commit, and the daemon never verifies, merges, or tears it down — it
supervises only task worktrees. Whether it is idle, busy, or blocked shows up in
`orch project list` (and a blocked primary agent is a `primary-agent@<base>`
owner in `orch permissions`, answerable there or by attaching and answering in
the TUI).

## Permission approval (`orch permissions`)

When an agent needs a permission to run a tool, it **blocks** on a pending ask
instead of auto-answering it. `orch permissions` is the surface for those asks:
approve, deny, skip, or quit, one at a time. It is **allow/deny only** — question
asks are out of scope — and every answer is one-time: nothing is persisted, so the
next ask is a fresh decision (matching the security model).

**Interactive by default** — no flags. Run it inside a project and it auto-picks
that project (no project prompt), then shows a numbered picker of every owner
with pending asks: claimed tasks, plus a `primary-agent@<base>` row when the
project's primary-agent conversation is blocked on a permission. Pick one and the
walk begins, one ask per block:

```
permissions for auth-migrate
  bash: npm run build
  [a]llow  [d]eny  [s]kip  [q]uit:
```

Each block shows what the agent is actually trying to run (the command, file,
or URL from the ask's metadata), not the permission glob — so you can decide
before you approve. `a` approves once, `d` denies, `s` leaves the ask pending
and moves on, `q` stops
the walk. **A denial asks you for a reason and sends it back to the agent** along
with a nudge to adjust and continue — so a denied request becomes a
conversation (the agent responds, tries a different approach, and can hit a new
ask for you to decide again) instead of the session going idle and the task
being held as needs-review by the daemon. An empty reason falls back to a generic
denial. After the last ask it rescans and reports what is still pending — exit 1
if anything is, 0 if all were answered. `--global` (or running outside a project)
lists every supervised project with its pending count first (`*` marks the one
you're in), then the same picker.

**Flags make it non-interactive** — for scripts and the notification flow:
- `orch permissions --task <name> allow|deny [--reason "..."]` — answer one
  task's first pending ask, no terminal needed; a deny's `--reason` explains it
  to the agent so it keeps working.
- `orch permissions -C <dir>` — list that project's pending asks, one per line.
- `orch permissions --json [--global]` — the same, machine-readable.

On a non-terminal stdin with no flags, `orch permissions` refuses to run — a
permission is a deliberate human act, so it must be interactive or explicitly
flagged.

## Creating a project from scratch

`orch project create <name>` (or `--name <name>`, or neither: it asks) is the
from-zero path: it creates a project folder (`<name>/`) holding the **bare**
repo as the fully-tracked store (`<name>.git`) and, adjacent to it, the base
worktree `main/` checked out from it on `main` — born already initialized: the
project is scaffolded (`orch init`) and committed straight into the store before
the worktree is checked out. Task worktrees land in the same project folder.
By default it also creates a **private** GitHub repo and pushes —
`gh` must be installed and authenticated or the command aborts (`--no-gh` creates
locally only, `--public` flips visibility, `--dir <parent>` chooses the parent,
prompting for it too when omitted). On a terminal every omitted flag is asked:
name, parent directory, whether to create a GitHub repo, visibility, description,
and — if the parent is outside a supervised root — whether to create it anyway.
The parent must otherwise be inside a supervised root
(`$HOME`, `/mnt`, `/tmp/opencode`, or `ORCH_ROOTS`) — a project the daemon cannot discover would
never settle, so create refuses and says so; `--allow-unsupervised` creates one
anyway, and `ORCH_ROOTS=/` supervises the whole filesystem. Then
intake is a conversation: `orch attach` (or any opencode session in the
base worktree) turns an idea — or a whole pasted spec — into `tasks/<name>/task.md`
specs via the machine's `create_task` custom tool, for you to review and commit. Both
commands leave the project on the
base worktree with the fail-closed verification hook; copy the machine-level template
`~/.config/orch/hooks/session-idle.example` to `hooks/session-idle` and edit it to real checks
before dispatching, and keep the
project under `$HOME`/`/mnt`
(`ORCH_ROOTS`) so the daemon supervises it.

## Cloning an existing repository

`orch project clone <url>` brings an **existing** repo into orch's layout — the
same on-disk shape `project create` produces: a bare store (`<name>.git`) with a
base worktree checked out beside it, so the daemon can supervise it. The project
name is taken from the URL's repo name (`--name` overrides), `--dir` chooses the
parent (prompted when omitted), and the bare store keeps `origin` → `<url>` and
**tracks it at setup**: the store gets a fetch refspec, fetches origin, and points
the base branch at its upstream, so the remote is tracked by default. orch never
fetches inside the daemon loop; refresh is a manual `git fetch`.

The base branch follows the rule the rest of orch uses everywhere: `main` if the
clone has one; otherwise you are **told and asked to pick** one of its branches
(`--base <branch>` makes it non-interactive). Whatever is chosen becomes
`baseBranch` in `.orchestration/config.json`.

Two kinds of repo are cloned, handled differently:

- **Never initialized** (no `.orchestration/` in the clone): `orch init` runs in
  the base worktree and the scaffolding is committed to the base branch. The
  project is **trusted immediately** — you created the orchestration, exactly as
  `project create` trusts its own scaffold.
- **Already orchestrated** (`.orchestration/` is committed in the clone): the
  clone's orchestration is left untouched. Because that code is third-party, the
  project is **not trusted by default** — it is discovered and only alerts until
  you `orch project trust` it, or clone with `--trust`. `--trust` is the explicit
  opt-in for repos that already ship orchestration; interactively you are asked.

As everywhere, only the verification hook committed on the base branch ever runs,
and a trusted clone's hook runs under the filtered environment — review a cloned
repo's `.orchestration/hooks/session-idle` before trusting it.

## Repointing the base branch

`orch project base <branch>` makes an **existing** project behave as if it had been
cloned with `--base <branch>`: it repoints the project's `baseBranch` so task
worktrees branch from `<branch>` and the verification hook, config, and task specs
are read from it. It is the "make an existing main project behave as if cloned with
`--base preview`" change — a single per-project base branch repoint, not
multi-environment support.

The branch **must already exist** in the project — `orch project base` never creates
one. And it is **non-destructive**: the old base branch is not removed; it simply
stays in place as an ordinary checkout. Only the project's notion of which branch is
the base moves.

## Task intake: the opencode plugin

Intake is not a CLI command — it is four opencode **custom tools**, registered by
one orch-owned **plugin** installed once per machine (`orch install`, or ensured
by `orch server start`) into `~/.config/opencode/plugins/orch.ts`. OpenCode 2
registers custom tools through a plugin, so this replaces the V1 `tools/`
directory. Every opencode session — the shared service's headless sessions and
any local TUI in any project — can call them. There is no `task draft`; you draft
in the conversation itself.

| Tool | What it does |
| --- | --- |
| `create_task` | Write `tasks/<name>/task.md` from params. `wait_human_start`/`wait_human_merge` are passed as the conversation decided (default `false` — scheduler-eligible, auto-merge). **Commits to base when `wait_human_start` is `false`, so the daemon sees it and the loop runs on its own; with `wait_human_start: true` it stays an uncommitted draft for your review.** Pass `commit` to override. Refuses to overwrite an existing task. |
| `update_task` | Amend a spec (committed or draft); only the fields you pass change. Commits the amendment unless the spec's `wait_human_start` is `true`. Refuses a **claimed** task and archived tasks. |
| `archive_task` | `git mv` a declared, unclaimed task to `archive/`, unblocking anything that depends on it. Commits the move by default (`commit: false` leaves it staged). A slug is freely reusable: if `archive/<name>/task.md` is already occupied by an earlier generation, this one lands at `archive/<name>/<utc-ts>/task.md`. |
| `delete_task` | Remove a task. Commits the removal by default (`commit: false` leaves it staged). Refuses claimed tasks and tasks other open tasks depend on. |

The tools are thin bridges to a Python backend (`orch tasktool` — a hidden
dispatcher subcommand), so slugs, front matter, dependencies, and refusals are
validated by the same code the rest of orch uses. **The tools commit their changes
to the base branch by default** — the human-in-the-loop gates are the wait flags
(`wait_human_start` starts a task, `wait_human_merge` gates the merge), and a task
whose human gate is on stays a draft you review before it is committed. A tool also
never
trusts a project; trust comes only from `orch init`/`project create`/`task add`/
`project trust`. Because the tools are machine-global, they are callable from any
opencode session — the safety rails (refuse off-base, refuse claimed, refuse a
delete that would strand an open dependent) are enforced by the tool itself, not by
per-project config.

`create_task`'s own description carries the decomposition contract, so no skill is
needed: decide one task or several, anchor a multi-part effort with a spec task and
one vertical slice task per deliverable, wire the slices with `dependencies:` edges
the scheduler honors, and give every task a runnable Verification.

## Two things worth knowing before dispatching

**Commit the verification hook — on the base branch.** A worktree is created
*from* the base branch, and the daemon judges by the *committed* hook, never the
working-tree copy. An uncommitted hook counts as no verification (absence of a hook is the implicit
unverified state — tasks run without checks). The task spec itself is committed by
`create_task` by default (or left a draft for you when a human gate is on), so the
agent always has its instructions. The task-lifecycle hooks (`pre-task-create` and friends)
follow the same rule: only the copy committed on the base branch runs, so commit them
before dispatching.

**The dispatch process exiting is not the task finishing.** The agent declares done by
committing its work to the branch; completion is then derived from the git state by the
daemon — the committed branch is verified and merged, nothing is stored anywhere else.
Watch `orch task list`.

**The daemon dispatches `todo` tasks itself (on by default).** `orch init` and
`orch project create` scaffold `.orchestration/config.json` with it enabled; a cloned
project starts with it off. The daemon fills free slots with the next unclaimed declared
task whose `dependencies:` are satisfied, in name order; opt out with
`orch project scheduler off`:

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
SSE consumer reads the shared service's `GET /api/event` stream and fires a copied
executable hook for each event, with the event's JSON on stdin and
`ORCH_EVENT=<dashed-name>` set — a session-scoped event also fires its project
hook in the owning worktree.

They are **advisory and fire-and-forget**: each hook is spawned detached (a slow
hook never blocks a daemon pass) and a non-zero exit — or a missing hook — changes
nothing. Hook output goes to `~/.local/state/orch/hooks/<event>.log`.

| Event | Fired when |
| --- | --- |
| `alert` | a task is blocked on a permission/question, or a project is untrusted |
| `needs-review` | a task needs a human (dirty work, an empty branch, retries exhausted, conflict, missing worktree) |
| `merged` | verified work merged into base (daemon verification pass or `orch task merge`) |
| `removed` | a diff-less branch cleaned up after a pass, or by `orch task archive` |
| `dispatched` | a task agent dispatched (`orch task start` or the scheduler) |
| `dispatch-failed` | a dispatch failed |
| `resolve-failed` | automatic conflict resolution failed |
| `restart-server` | the shared opencode service restarted |
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
  (`$HOME`, `/mnt`, `/tmp/opencode`, `ORCH_ROOTS`) stays wide so you can see everything, but nothing
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
- **The control plane is loopback-only and the logs are private.** The shared
  opencode service binds `127.0.0.1` and writes its registration and password to
  `~/.local/state/opencode/service.json` (`0600`); orch's state and session logs
  live in `~/.local/state/orch` created `0700` with `0600` files, so other users
  cannot read conversations or the port.

Same-user compromise is out of scope: anything that already runs as you can read your
files and drive the agents. If you need containment against hostile agents, run them in
an OS-level sandbox — orch does not provide one.

## What works, and what does not

Working and exercised against live agents: `init`, `verify`, `doctor`, `server`,
`project create`/`trust`/`untrust`/`list`, `ask`, and `task add`/`start`/`list`/
`answer`/`reset`/`archive`/`merge`,
and the daemon — including the full loop of settle → verify → commit-on-pass → merge →
archive, the failure loop of verify-fail → inject → bounded retry → `needs-review`, the
hold path of an empty branch (committed nothing) → `needs-review`, and the **scheduler** (auto-dispatch
of `todo` tasks, on by default for `init`/`create` and off for clones, trust-bound). Task intake via the machine's
`create_task`/`update_task`/`archive_task`/`delete_task` custom tools is exercised
against live agents (an agent calling the tools writes, amends, moves, and removes
`tasks/` specs through them). A slow agent that spends minutes
inside a tool is
held, not torn down: the daemon treats a busy session (opencode reports it
working for the whole agent loop) as the agent working and only settles once it
goes idle.

The security controls are covered by unit tests: the **trust boundary** (untrusted projects
only alert), the **env filter** (agents and verification hooks run without the owner's secrets), and
the **`.orchestration` deny** installed by `orch install`.

`orch daemon` supervises: it settles tasks, runs the committed verification hook itself, retriggers
failing agents up to `retryCap`, merges verified work, archives done work, restarts a
dead or unresponsive shared opencode service, flags blocked permissions, and consumes the
shared service's `/api/event` SSE stream to fire per-event hooks. A **merge conflict**
is left in progress and the task reads `needs-review (merge-conflict)`. Who resolves it
follows `wait_human_start`: a task a human had to start needs the human to resolve it
in the base worktree (`git add`/`git commit`); a machine-started task gets an agent
dispatched into the base
worktree to resolve and commit on its own (up to `retryCap` tries, then it escalates to
a human). Either way, once the merge lands the daemon archives the task. The policy is
a pure function of a snapshot, so those cases are tested rather than hoped for.

**Stopped, finished and done are different things, and all three are derived from git.**
Stopped means the session is idle. The agent declares done by committing its work — a
committed, clean, diffing branch is the signal, and nothing is stored elsewhere to record
it. Finished means the verification loop settled — the daemon's run of the hook passes (the
hook also commits any files the agent left uncommitted), a failing one drives the agent back
until the retry cap is spent. Done means a branch the agent committed passed the daemon's
own verification and was merged. A task with a dirty tree at settle time is the work the
agent did not commit — the daemon still verifies it, and the hook commits the stragglers on
a pass.

Not built yet: routing `needs-review` onwards or unblocking a parent agent.

The per-event hooks are live: the daemon's SSE consumer reads the server's
`GET /api/event` stream and fires a copied, executable hook per event type in
`src/orch/opencode_events.py` (templated machine-level as `~/.config/orch/hooks/*.example`),
with the event JSON on stdin.

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

The opencode-event hook templates are covered by unit tests too: `tests/test_opencode_events.py`
sanity-checks the catalog — well-formed event names, unique dots-to-dashes hook names, and no
collisions with the lifecycle or global hooks. The SSE consumer itself is covered by
`tests/test_sse.py`: stream parsing, global + project hook firing, session routing, and the
session-idle verification hook never firing from the stream.

## Layout

- `bin/` — dispatcher (`orch`; `orch tasktool` is a hidden backend subcommand)
- `src/orch/` — state (scan), dispatch, daemon, verify, merge, server, doctor, CLI,
  `tasktool.py` (the custom-tool backend), `custom_tools.py` (installs the global
  plugin), `tools/orch.ts` (the V2 plugin that registers the four task tools),
  `opencode_events.py` (the server-event catalog), and `sse.py` (the daemon's
  `/api/event` stream consumer that fires per-event hooks)
- `docs/` — architecture and decisions
- `tasks/` — one folder per task, each with a `task.md`
