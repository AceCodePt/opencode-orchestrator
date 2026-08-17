# Architecture

## Vocabulary

| Term | Meaning |
| --- | --- |
| **Harness** | opencode. Supplies agents, sessions, HTTP API, event bus. |
| **Project** | A git repo (bare + worktrees, or plain). Owns its own code, checks, config. |
| **Task** | A unit of work with a spec, dispatched into its own worktree with its own agent. |
| **`orch`** | The global tool: one CLI and one daemon per machine. |
| **`.orchestration/`** | Per-project, tracked, project-specific hooks and config. |

**The repo is the state.** Task management — which tasks exist, their status, their
ordering — is derived from the project's own git repository, never stored anywhere
else. Runtime facts — what is running right now — are read live from `/proc` and
the shared server. There is no database, no cache, no registry, no generated
backlog file. Nothing can drift because there is nothing to drift from.

| Concept | Derived from |
| --- | --- |
| task declared | `git ls-tree <base> -- tasks/` |
| task claimed | `refs/heads/<name>` exists — **the branch is the claim** |
| worktree / branch | `git worktree list` |
| dirty / diff | `git status --porcelain` in the worktree; `rev-list --count base..branch` |
| session state | the shared server: `/session?directory=<worktree>` |
| dispatch alive | a session bound to the worktree that is busy or blocked — task agents are server sessions, not subprocesses |
| dispatch orchestrated | the task has a session on the shared server (every task session is created by `orch task start` or the scheduler via `POST /session`) |
| which projects exist | a live scan of `$HOME`, `/mnt`, and orch's dev root `/tmp/opencode` (env `ORCH_ROOTS` overrides) for `.orchestration/` dirs |
| retries, settle timers | **in-memory in the daemon only** — lost on restart, accepted |

## The task lifecycle

```
todo  ->  running  ->  ready  ->  merged  ->  archived
            |  \->  needs-review
```

- **todo** — `tasks/<name>/task.md` on base, no branch.
- **running** — a branch named `<name>` exists (a live session or dispatch).
- **needs-review** — claimed, but the loop cannot settle it green: uncommitted work,
  a failing verification hook with no session left to retrigger, retries exhausted, a merge left
  in progress, or a missing worktree. Derived from the worktree, never stored.
- **ready** — a clean, committed, diffing branch whose session settled: the
  verification hook passed and committed it, and the daemon merges it on its next pass.
- **merged** — the ready branch was folded into base. The branch and worktree are
  removed and the spec is **archived** (`archive/<name>/task.md`) so the active
  backlog stays light.
- **archived** — the spec has left `tasks/`; git history and the merge commit are the
  record of what was done.

## The completion signal: the git state tells you

The working agent declares done by committing its work to the branch — git is the only
state that records it, and nothing is stored anywhere else. The daemon still judges
*acceptance*: the `session-idle` hook — the project's verification hook — verifies, and on a
pass also commits anything the agent left uncommitted (idempotent), so a passing task always
leaves a clean, committed, diffing branch:

- **Non-zero exit** → failed; the output is fed back to the agent.
- **Zero exit** → the hook commits any remaining work (`git add -A && git commit` only if
  anything is dirty — idempotent), then passes.

So the observable git state of the branch is the whole story when a task settles
(session idle or gone):

| Worktree state | Meaning | Daemon does |
| --- | --- | --- |
| dirty (untracked/staged/modified) | work the agent did not commit; the hook will commit it on a pass | run the hook → retrigger or `needs-review` |
| clean + diff vs base | the agent (or a passed hook) committed the work | run the hook → **merge + archive + teardown** |
| clean + identical to base | nothing was produced | **remove + archive** (no verification needed) |

## The loop lives in the daemon

There is no router plugin. The daemon owns the whole loop — it is the component that
observes, decides, verifies, retriggers, merges, and archives. `decide()` is a pure
function of a snapshot, so every branch above is testable.

### When is an agent "still working"?

Every task agent is a session on the shared server bound to its worktree — created via
`POST /session?directory=<worktree>`, prompted via `prompt_async` — so there is no
dispatch subprocess to inspect. The daemon reads three signals from the server:

- **busy** — the session appears in `/session/status` (opencode reports `busy` for the
  whole agent loop, including long-running tool calls); the agent is working.
- **blocked** — a live pending ask for that worktree in `/permission` or `/question`; the
  agent is waiting on a human.
- **interrupted** — the session's last assistant message was aborted (`MessageAbortedError`,
  i.e. a user stopped the turn in the TUI or it died); the work is held for the human.
- **idle** — neither; the work is settled, and the verification hook plus the worktree
  state decide the outcome.

A human-attached client (`opencode attach`) shares the same sessions and is simply
another viewer; nothing about a controlling terminal distinguishes an orchestrated
agent anymore. A pending ask whose **owning message** was aborted is **orphaned** — the
turn that created it is dead, so it is not a block; the daemon replies `reject` to clear
it from the server and the task settles by its worktree state (an interrupted turn's
work is `needs-review`, never auto-merged).

Per claimed task, each cycle:

1. **Interrupted** with work (`dirty` or `diff`) → `needs-review` (`interrupted`), held for
   the human; an interrupted task with no produced work still closes as a no-op.
2. **Blocked** on a live permission/question → alert (deduped by request id, in-memory).
3. **Busy** session, or an orchestrated run process alive → running; nothing. (A run
   client stuck alive past `MAX_ORCHESTRATED_S` is escaped, so it can never stall a
   task forever.)
4. **Settled** (session idle or gone) past a settle budget (~30s):
   - worktree missing → `needs-review`
   - clean + no diff → `remove` (no-op)
   - otherwise: the committed verification hook decides, or its absence is the
     implicit unverified state:
     - **hook present, pass** → the hook committed → diff → `merge`; no diff → `remove`
     - **hook present, fail** → inject the failure via `prompt_async`; retry++;
       `< cap` wait for the agent to work again, `>= cap` → `needs-review`
       (`retries-exhausted`)
     - **no committed verification hook** → the daemon commits the work itself and
       accepts it without verification — absence is implicit, never a failure
     - **`wait_human_merge`** → the accepted work is left ready for `orch task merge`,
       not merged by the daemon
4. **Fingerprint unchanged** since the last failure for longer than a stall budget →
   `needs-review` (`stalled`) — the agent was told what to fix and changed nothing.

Retries and settle timers are in-memory: a daemon restart resets them, and the task
still lands correctly because `needs-review` is recomputed from the worktree, not
stored.

## The verification hook cannot be edited by the agent

The hook that runs is the version **committed on the base branch**, staged to a
temp dir outside the worktree (`verify.run`). The agent's working-tree copy is never the
judge. A diverging copy is flagged as `hook-modified` but does not overturn the
verdict — the committed hook ran. Improving a hook costs a commit to base, which is
reviewable; editing it mid-task buys nothing. This is the sharpest lesson this
design learned, and it still holds with the router gone: there is simply no plugin
inside the agent's server left to trust or distrust — the daemon runs every verification hook.

The commit-on-pass is inside the hook, so a passing verification run always leaves a
clean, committed branch even when the agent did not commit everything. A dirty worktree at
settle time means work the agent did not commit; the daemon still verifies it and the hook
commits the stragglers on a pass.

## State: one authority per question

| Question | Authority |
| --- | --- |
| What work exists, and what does it mean? | **the base branch** — `tasks/*/task.md` as committed there |
| What is claimed? | **git refs** — `refs/heads/<name>` |
| What orders the work? | **the base branch** — `dependencies:` in each task's front matter |
| Is the work committed and passing? | **the worktree** — clean + diff + the daemon's own verification run |
| What is running right now? | **live** — `/proc`, the shared server |
| How long since… (retries, settle) | **the daemon's memory** — restart-losing by design |

## The repo is the tracker

There is no external issue tracker. The project's own `tasks/` *is* the tracker:
- a spec file declares work, a branch claims it, the daemon verifies and merges it, `archive/` closes it
- ordering is `dependencies:` front matter — a slice dispatches when its dependencies archive, which is exactly the frontier query
- intake is a conversation in opencode: the `create_task` custom tool turns free text
  (with the human deciding the wait flags) into a spec task + slice tasks; the TUI is
  the surface, `orch attach` drops you into it on the project's primary-agent session
  (or, run from inside a claimed task worktree, on that task's session)
- triage is a human reviewing uncommitted drafted specs — approve (commit), ask-for-info (leave uncommitted), or drop

GitHub is **hosting only**: `orch project create` pushes a private repo; nothing reads
or writes issues, labels, or PRs.

## Separation of concerns

```
                          ┌──────────────────────────────┐
    phone / tailnet ──────│  orchestrator daemon         │
                          │  settle · decide · verify    │
                          │  retrigger · merge · archive │
                          └───────┬──────────────┬───────┘
                                  │ facts        │ actions
                          ┌───────▼──────┐       │  inject failure / merge / remove / kill pid
                          │   scan       │       │  approve stays the human's `orch task answer`
                          │  git + /proc │       │
                          └───────┬──────┘       │
                                  │ HTTP         │
   ┌────────────────────────────▼──────────────▼────────────────────┐
   │  ONE `opencode serve` per machine (headless, always a port)    │
   │  sessions per worktree; agents are server sessions, never runs │
   └───────────────────────────────┬────────────────────────────────┘
                                    │ the verification hook
                          ┌──────────▼───────────┐
                          │  .orchestration/     │  project knowledge
                          │  hooks/session-idle  │  verify + commit on pass
                          └──────────────────────┘

   git    ── intent and status: tasks/, archive/, branches, worktrees
   /proc  ── runtime facts: pids, cmdlines
   memory ── the daemon's retries, settle timers, alert dedup (restart-losing)
```

### 1. The scan — sensing only

`state.scan_project` derives every task fact from the repo and the live system. It
writes nothing. Project discovery is a bounded `fd`/`find` walk of `$HOME` and `/mnt`
(env `ORCH_ROOTS`) for `.orchestration/` directories, deduped by `git-common-dir`,
cached in-memory for ~60s.

### 2. The daemon — policy and authority

Consumes the scan, decides, and is the only component permitted to act. Its actions:
restart the shared server, inject a verification failure back into a session, merge a verified
branch, remove a no-op, mark `needs-review`, alert on a blocked permission. It runs
the verification hook itself — there is no verdict it accepts from anywhere, because there is no
plugin left to report one.

**One per machine, under systemd, with linger.** Not one per project: the shared
server is one process, the retries are one set, and project discovery is machine-wide.

A long-lived daemon holds the code it imported at startup. `doctor` therefore fails
when the running supervisor's start time precedes the newest source file — a version
string cannot do this job.

### 3. CLI — per-invocation action

| Command | Does |
| --- | --- |
| `orch init` | scaffold `.orchestration/` (config + the `session-idle` verification hook template). The project is trusted for the daemon as it scaffolds |
| `orch project create <name>` | new project from scratch: a project folder (`<name>/`) holding a **bare** store (`<name>.git`) and, adjacent to it, the base worktree `main/` checked out from it on `main` — born initialized (the scaffold is committed straight into the store before the worktree is checked out; task worktrees live in the same folder) + a private gh repo pushed (`gh` must exist and be authenticated, else abort; `--no-gh` skips; `--name`/`--dir` can replace the args; on a terminal every omitted flag — name, parent, gh, visibility, description, unsupervised-override — is asked). The parent must be inside a supervised root (`$HOME`, `/mnt`, `/tmp/opencode`, or `ORCH_ROOTS`) or create refuses — `--allow-unsupervised` overrides. Created projects are trusted automatically |
| `orch project trust [--dir]` | machine-level approval that lets the daemon verify, merge, dispatch and resolve in this project. The marker lives in `~/.local/state/orch/trusted.json`, keyed by the repo's git common dir — outside the repo, so a project cannot approve itself |
| `orch project untrust [--dir]` | revoke that approval; running agents and worktrees are left alone, the daemon just stops settling them |
| `orch project list` | every discovered project and its trust status (`--json`) |
| `orch project scheduler [<n>\|off]` | show or set the project's auto-dispatch cap (`scheduler.maxConcurrent`). Writes into the base worktree's `.orchestration/config.json` for review and commit; the committed copy is the only one in force |
| `orch daemon budget [<n>\|off]` | show or set the machine-wide auto-dispatch cap in `~/.config/orch/daemon.json`; `0` disables scheduling machine-wide, `off`/absent means per-project caps only |
| `orch task add <name…>` | scaffold `tasks/<name>/task.md` (no TASKS.md is written or generated). The project is trusted for the daemon as it scaffolds |
| `create_task`/`update_task`/`archive_task`/`delete_task` | machine-global opencode custom tools (installed by orch into `~/.config/opencode/tools/`) — intake is a conversation, not a command: any opencode session can create/amend/move/remove `tasks/` specs from params. None ever commits or trusts |
| `orch task start <name…>` | claim (create branch+worktree), run hooks, dispatch |
| `orch task list` | render the scan for this project; `--global` scans every supervised project and reports what is waiting on you (blocked, stuck, and unmerged work) with the exact action to take |
| `orch task answer <name> allow|deny` | reply to a blocked permission (or a question's text). An answer is one-time — nothing is persisted |
| `orch task reset <name>` | delete branch + worktree → back to `todo` (a deliberate human act) |
| `orch task archive <name>` | close a reviewed `needs-review` task (delete + archive the spec) |
| `orch task merge <name>` | re-run a merge after a conflict |
| `orch verify` | inspect whether the committed verification hook is trustworthy |
| `orch doctor` | verify the contracts that fail silently |
| `orch verify --run` | run the committed verification hook here and say why |
| `orch attach` | attach the interactive TUI on the shared server — from inside a claimed task worktree to that task's session (create it on first use), otherwise to the project's primary agent (create the `orch:agent` session on first use) |
| `orch permissions` | approve/deny pending permission asks interactively: run inside a project it auto-picks it and shows a task picker (a `primary-agent@<base>` row covers the base worktree's asks), `--global` (or running outside a project) picks a project first. Flags make it non-interactive: `--task <name> allow\|deny [--reason "..."]` answers one task's ask (a deny carries its reason back to the agent so it keeps working rather than the session idling into a daemon no-op), `-C <dir>` lists the project's asks, `--json` lists them machine-readably. Questions are out of scope — this is allow/deny only, and every answer is one-time (nothing persisted) |

The CLI is what agents call, via a tool. A task is a branch: `task start` refuses
when the branch already exists (the claim), and refuses an uncommitted spec or an
unverified project.

### 4. Project `.orchestration/` — project knowledge

- `hooks/session-idle` — the verification hook: verify, and on success commit the
  work. The daemon fires it on settle; the CLI fires it via `orch verify --run`. `orch init`
  scaffolds
  `hooks/session-idle.sample` — inert until copied to the real name, made
  executable, and committed.
- Lifecycle hooks: `pre-task-create`, `post-task-create`, `pre-task-cleanup`,
  `pre-branch-delete`, `post-task-cleanup`. `orch init` scaffolds git-style
  `hooks/*.sample` references for all five — inert until copied to the real name,
  made executable, and committed. Like the verification hook, they run the version
  **committed on
  the base branch** (never a working-tree edit or a hook committed only on a task
  branch), so an agent cannot change what runs at cleanup by editing a hook.
- One `hooks/*.sample` per opencode server event (the vocabulary in
  `src/orch/opencode_events.py`, the single source of truth). The daemon's SSE
  consumer reads the shared server's `GET /event` stream and fires a copied,
  executable hook for each event, with the event's JSON on stdin and
  `ORCH_EVENT=<dashed-name>` set; a session-scoped event also fires in the
  worktree that owns the session. `session.idle` is the one project-side
  exception: that name is the verification hook, so it keeps the verification sample.
- `config.json` — `baseBranch`, `verify.retryCap`, `scheduler`
  (`enabled`/`maxConcurrent`). Like the verification hook, the **committed copy on
  the base branch**
  is the authority for these values (`orch project scheduler` writes the working-tree
  copy for a human to commit). A machine-wide cap lives outside the repo in
  `~/.config/orch/daemon.json` (`orch daemon budget`).

Two dispatchers fire hooks from one directory under one env-var contract
(`TASK_NAME`, `BRANCH_NAME`, `WORKTREE_PATH`, `WORKTREE_NAME`, `BASE_BRANCH`): the
CLI fires task-lifecycle events, the daemon fires `session-idle`.

### 4b. Global hooks — machine-level notifications

Mirror of the project-hook contract at the machine level: `~/.config/orch/hooks/`
(created `0700`, owned by the machine owner, outside every repo, so no project
agent can edit them). `orch init` writes an inert `.example` template for each
documented event, plus one for every opencode server event (fired by the daemon's
SSE consumer, same as the project event hooks); an executable file at the exact
event name opts in. The daemon
and the CLI both fire them — `alert` (blocked permission/question, untrusted
project), `needs-review`, `merged`, `removed`, `dispatched`, `dispatch-failed`,
`resolve-failed`, `restart-server`, `reset`, `archived`.

They are **advisory and asynchronous**: spawned detached with `start_new_session`,
the daemon never waits on one, and a non-zero exit (or a missing hook) only lands
in `~/.local/state/orch/hooks/<event>.log`. Fail-open by construction: a broken
notify script can never stall a pass. Env: the full environment plus `ORCH_EVENT`,
`ORCH_ACTION`, `PROJECT_NAME`, `PROJECT_DIR`, task-scoped vars, and
`ORCH_REASON`/`ORCH_DETAIL` (plus `ORCH_REQUEST_ID`/`ORCH_SESSION_ID`/`ORCH_KIND`
for blocked alerts). Existing dedup carries over — alerts fire once per request id,
`needs-review` once per backoff window.

### 5. The primary agent — the project's own conversation

Distinct from task agents, a project can have **one persistent primary agent**
(`orch attach`). It is a session on the shared server attached to the project's
base worktree, found live by its `orch:agent` session title — no stored state,
matching the "the repo is the state" rule. First use creates it with
`POST /session?directory=<base>`; `orch attach` then runs opencode's own
`attach <url> --session <id>` TUI on it, so every further message continues the
same session via the normal client, not a headless proxy. Like task agents it is a
plain server session with **no `opencode run` loop**, so a permission ask stays
pending and is surfaced in the attached TUI instead of being auto-rejected. The
primary agent edits the base worktree uncommitted and is **outside the daemon's
loop**: it is never verified, merged, or torn down, and its work is never a task.

## Flow A — dispatch

1. `orch task start <name>` (human, parent agent) or the **scheduler** (see below).
2. CLI refuses if `refs/heads/<name>` exists — the branch is the claim, atomically.
3. CLI runs `hooks/pre-task-create` (the copy committed on the base branch) from the
   dispatcher's worktree.
4. CLI creates the worktree (`git worktree add -b <name> <base>`), pruning first.
5. CLI runs `hooks/post-task-create` (the committed base copy) from the new worktree.
6. CLI notes whether a committed verification hook exists; absence is fine — verification is opt-in by committing the hook.
7. CLI creates a task session on the shared server, bound to the new worktree, and
   sends the task prompt — no subprocess is spawned, so nothing is detached and
   nothing can auto-answer a permission ask:

   ```
   POST /session?directory=<worktree>            (title: <name>)
   POST /session/{id}/prompt_async               (parts: the task prompt)
   ```

The dispatch call returning is not the task finishing. Completion is derived later
from the worktree state, by the daemon; a permission ask stays pending until a
human answers it with `orch task answer`.

Intake has no CLI one-shot. It is a conversation: an opencode session in the base
worktree (the TUI, via `orch attach`) turns a free-text description into one
or more `tasks/<name>/task.md` specs by calling the machine-global `create_task`
custom tool — writing into the base worktree and **committing the spec to base when
`wait_human_start` is false**, so the daemon sees it and the loop runs on its own.
With `wait_human_start: true` the spec stays an uncommitted draft the human reviews
before it is committed. The tool takes the wait flags (`wait_human_start` /
`wait_human_merge`) as optional booleans the agent passes as the conversation
decided (default `false`); `commit` overrides the default. The tools
never claim or dispatch a task — but they do commit, because the human-in-the-loop
gates are the wait flags, not the commit.

## Flow A′ — the scheduler

The daemon can dispatch `todo` tasks itself. A project opts in via
`.orchestration/config.json` — read, like the verification hook, from the
  **committed copy on the
base branch** (`orch project scheduler <n>` writes the working-tree copy for you to
review and commit; only the committed values apply):

```json
"scheduler": { "enabled": true, "maxConcurrent": 1 }
```

Each cycle, per project, the daemon fills free slots (claimed, in-flight tasks count
against `maxConcurrent`) with the next unclaimed declared task whose **dependencies are
satisfied**, in name order. A task whose `task.md` is uncommitted on base, or a project
with no base worktree to dispatch from, is skipped. Failed dispatches back off (60s) and
the daemon's journal records why — `dispatch_error` is in-memory only. The scheduler
never dispatches a task whose front matter says `wait_human_start: true`.

**Ordering is the `dependencies:` front matter.** Each `tasks/<name>/task.md` may
declare the tasks that block it, as slugs in the gray-matter block:
`dependencies: [spec, auth-migrate]`. A `todo` task dispatches only when every listed
dependency is **archived** (or was never declared — a slug that never existed cannot
block); an open declared task always holds its dependents, even one reset back to
`todo`. The frontier — everything dispatchable right now — is the all-dependencies-archived
set, so the question "what can I grab?" is derived from the repo, never stored. A spec
task (`wait_human_start: true`, the anchor for a feature) is never auto-dispatched and is never
"done"; it closes by convention when its slices archive. `orch doctor` fails on a
dependency slug that is neither declared nor archived — a typo would block the task
forever.

On top of the per-project cap, a **machine-wide budget** (`orch daemon budget <n>`,
stored in `~/.config/orch/daemon.json`) caps the total in-flight task agents the
scheduler may hold across *every* project at once: each cycle the scheduler counts
claimed tasks project-wide and stops emitting dispatches once the running total
reaches the budget (`0` disables scheduling machine-wide; `off`/absent means
per-project caps only). The budget bounds auto-dispatch only — a human
`orch task start` is not refused.

Also machine-wide in the same file, `{"settleBudget": N}` sets how many **seconds** an
idle task session must sit before the daemon settles it (verifies/merges/sweeps). It is
re-read every pass, so editing `~/.config/orch/daemon.json` applies on the next cycle
with no daemon restart; a value of 1 second or less is ignored (the daemon wakes every
second, so the budget would be meaningless), and absent, it falls back to the built-in
default (30s).

**The machine runs and merges by default; the human opts in per task.** Two front-matter
flags (the gray-matter block at the top of `tasks/<name>/task.md`) put the human back
in the loop: `wait_human_start: true` keeps the scheduler from auto-dispatching a task
until a human runs `orch task start`; `wait_human_merge: true` verifies and commits a
task but leaves it ready — only `orch task merge <name>` folds it in. Both default to
false, and both are read from the **committed** copy on base — never the working tree —
so neither an agent nor an uncommitted edit can auto-grant or auto-wait a task.

## Flow B — the verification loop

1. The task settles (session idle or gone; an orchestrated run process is held until
   it exits, so a slow agent is never torn down mid-tool).
2. The daemon runs the committed `session-idle` verification hook in the worktree.
3. **exit 0** → the hook committed → diff → merge + archive + teardown; no diff →
   remove + archive.
4. **non-zero** → the daemon injects the output into the session and waits for the
   agent to work again. This is bounded by `verify.retryCap`, tracked in memory.
5. **Cap spent** → `needs-review` (`retries-exhausted`). **No verification hook** →
   the daemon commits the work itself and accepts it without verification — the hook
   is the opt-in; its absence is the implicit unverified state.
6. **Merge conflicts** → the daemon leaves the merge **in progress** (so the state is
   derivable: the scan sees `MERGE_HEAD` and reads the task as
   `needs-review` (`merge-conflict`)) and never re-runs it. **Who resolves follows
   `wait_human_start`**: a human resolves a human-started task in the base worktree
   (`git add`/`git commit`); a machine-started task gets its own agent dispatched into
   the base worktree (`opencode run --title <name>-merge-conflict`) to resolve and
   commit, bounded by the same `retryCap` before it escalates to a human. Once the
   merge lands either way, the branch is an ancestor of base and the daemon's no-op
   path archives the task. A conflicted task is never re-verified, re-merged, or torn
   down; the resolution agent is detected by worktree + task title so a task agent in
   a sibling worktree is never mistaken for it.

The agent declares done by committing its work; the daemon's verification run judges
acceptance, and the merge is the verdict. Neither side declares success alone — and there is
no side inside the agent's server that reports a verdict at all.

## Flow C — observation and failure handling

- The daemon reads the scan every ~10s. Absence (a task whose session vanished) is as
  important as presence: a claimed task with no session and a dirty tree is
  `needs-review`.
- **Server death** → restart. Tasks settle afterwards and their worktree state
  decides: dirty → review, clean+diff → verify and merge, clean+no-diff → remove.
  The verification is the daemon's own run, so a branch that merges after a crash is
  verified
  work, not an unexamined claim.
- **Kill by pid, never by pattern.** The dispatch process is identified by its exact
  cmdline (`opencode run … --dir <worktree>`) and killed by that pid.

## Flow D — routing, and the human switch

The daemon's only human-facing route is the **alert**: when a task's agent is blocked
on a permission or a question, the daemon raises `alert` (deduped by request id) and
fires the machine-level `alert` global hook (see 4b). `orch` knows nothing about
phones, Termux, or notify add-ons — it only *exposes* that a human is needed, through
signals any consumer can read:

- `orch task list` renders the scan — blocked sessions and every `needs-review`
  task (`--json` for machines).
- `orch task answer <name> allow|deny` replies to the blocked permission (or the
  answer text, for a question) over the shared server, unblocking the agent directly.
- The daemon's journal carries the `alert:` line (and every `needs-review` outcome).
- A global `alert` hook (or `needs-review`, `merged`, …) does the delivery — push to
  a phone, an HTTP endpoint, anything the machine owner's script can do.

The phone add-on (`~/stuff/phone-approval` — `notify-phone` → Termux, plus the
approve/deny loop) integrates **with** `orch` the other way around: it polls
`orch task list` or tails the daemon journal, and only it knows about the phone.
Whether a parent agent should be unblocked, or a `needs-review` task routed onwards,
is a decision still to make — the machinery stores no verdicts, so routing will be
built on the daemon's log + scan rather than on a verdict table.

## Invariants

1. No agent is created that cannot be observed — structural: agents are headless
   servers, and a server always has a port.
2. The scan never writes. The daemon holds all authority.
3. One authority per question: git for intent, live system for runtime, memory for
   transient bookkeeping.
4. The agent is never trusted for state. Git records what the agent committed; the
   daemon's own verification decides what is accepted and merged.
5. The verification hook's entry point comes from the base branch; the working-tree
   copy is never the judge, and a diverging copy is flagged.
6. Verification is opt-in: a project without a committed hook accepts its tasks
   without verification. A present hook that fails never passes.
7. The agent declares done by committing; only a committed, diffing branch can merge,
   and it merges only after the daemon's own verification run passed it (or, when no
   hook is committed, after the daemon's implicit acceptance commit).
8. Kill by recorded pid (a specific process found by exact cmdline), never by pattern.
9. Unavailability never produces a pass. Store gone (there is none), server down,
   verification hook timed out, worktree gone: each lands on `needs-review`.
10. Every project-specific decision is a hook or config value, never daemon or CLI
    code.

## Settled: threat model, scope, and versions

**The threat model is a lazy agent, not a hostile one.** An agent takes the shortest
route to looking finished; it is not assumed to attack the machine. The blast radius is
still bounded:

- **Authority is trust-bound.** The daemon settles, verifies, merges, dispatches and
  resolves only in projects the machine owner approved (`orch project trust`);
  untrusted projects raise a single deduped `alert` and nothing else. Discovery stays
  wide so everything is visible, but a repo you merely clone can never auto-run its
  verification hook or dispatch an agent. `doctor` fails on an untrusted project and warns when
  `ORCH_ROOTS` includes `/`.
- **The verification hook is the committed base copy** (the daemon runs it from
  `git show base:…`),
  and `orch install` writes machine-level opencode permission rules denying
  `edit`/`write` on `.orchestration/**` and on the config itself, so the lazy agent
  cannot silently rewrite its own verification hook through its file tools. Working-tree edits still
  surface as `diverged`.
- **Agents, verification hooks, and project hooks run with a filtered environment.** `SSH_AUTH_SOCK`,
  cloud credentials, `*_PASSWORD`/`*_SECRET`/…, `GH_TOKEN`, `DATABASE_URL`, `KUBECONFIG`
  and friends are dropped before `opencode run`, a verification hook, or a project
  hook runs — the
  task lifecycle hooks included; provider keys survive. Machine relaxation lives in
  `~/.config/orch/env.json` (`drop`/`keep`); a project can only restrict further
  (`agentEnv.drop`) and a verification hook — or a project hook — can opt into the
  full env (`verify.env: full`). Env filtering is defence in depth, not containment: a hostile
  agent with `bash` can still read `~/.ssh` from disk, which is why the threat model
  stays lazy-agent.
- **Permissions are one-time.** `task answer` approves a blocked permission once;
  nothing is persisted to opencode's runtime config.
- **Loopback control plane, private state.** The shared server binds `127.0.0.1`;
  `~/.local/state/orch` is created `0700` with `0600` files, so other users can read
  neither the port nor the session logs.

Same-user compromise is explicitly out of scope: anything running as the same user can
read the 0600 files and drive the agents. OS-level containment (sandboxing agents or
verification hooks) is deliberately not provided.

**The scope is infrastructure, not agent workflow.** What an agent may do, which
model it uses, what its prompt says — all out of scope. A project supplies its own
checks through `.orchestration/`.

**The supported harness floor is 1.18.11.** Anything newer is a bonus. The shared
server's live version is checked by `doctor` against the floor; there is no version
history store any more.

**Deferred, with nothing built on either:**
- Routing `needs-review` onwards or unblocking a parent agent. (The "human needed"
  signal exists — `orch task list --json` and the daemon journal — and the phone
  add-on in `~/stuff/phone-approval` consumes it; orch has no phone knowledge.)
- A budget that also bounds the primary agents or a human's manual `orch task start`.
  The scheduler's auto-dispatch is capped per project (`scheduler.maxConcurrent`) and
  machine-wide (`orch daemon budget`); a per-model cap is not there.
