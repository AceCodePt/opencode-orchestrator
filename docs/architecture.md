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
else. Runtime facts — what is running right now — are read live from `/proc`, `ss`,
and the shared server. There is no database, no cache, no registry, no generated
backlog file. Nothing can drift because there is nothing to drift from.

| Concept | Derived from |
| --- | --- |
| task declared | `git ls-tree <base> -- tasks/` |
| task claimed | `refs/heads/<name>` exists — **the branch is the claim** |
| worktree / branch | `git worktree list` |
| dirty / diff | `git status --porcelain` in the worktree; `rev-list --count base..branch` |
| session state | the shared server: `/session?directory=<worktree>` |
| dispatch alive | `/proc` — the process whose cmdline is `opencode run … --dir <worktree>` |
| dispatch orchestrated | `/proc` — such a process **with no controlling terminal**: one the daemon or CLI started detached |
| which projects exist | a live scan of `$HOME` and `/mnt` (env `ORCH_ROOTS`) for `.orchestration/` dirs |
| retries, settle timers | **in-memory in the daemon only** — lost on restart, accepted |

## The task lifecycle

```
todo  ->  running  ->  merged  ->  archived
            |  \->  needs-review
```

- **todo** — `tasks/<name>/task.md` on base, no branch.
- **running** — a branch named `<name>` exists (a live session or dispatch).
- **needs-review** — claimed, but the loop cannot settle it green: uncommitted work,
  a failing gate with no session left to retrigger, retries exhausted, or a missing
  worktree. Derived from the worktree, never stored.
- **merged** — a clean, committed, diffing branch passed the daemon's gate and was
  folded into base. The branch and worktree are removed and the spec is **archived**
  (`archive/<name>/task.md`) so the active backlog stays light.
- **archived** — the spec has left `tasks/`; git history and the merge commit are the
  record of what was done.

## The completion signal: the worktree tells you

The agent does **not** commit, and the agent does **not** decide when it is done.
The `session-idle` hook — the project's gate — does both:

- **Non-zero exit** → failed; the output is fed back to the agent.
- **Zero exit** → the gate *commits the work* (`git add -A && git commit` only if
  anything is dirty — idempotent), then passes.

So the observable state of the worktree is the whole story when a task settles
(session idle or gone):

| Worktree state | Meaning | Daemon does |
| --- | --- | --- |
| dirty (untracked/staged/modified) | the gate never passed, so nothing was committed | run the gate → retrigger or `needs-review` |
| clean + diff vs base | the gate passed and committed | run the gate → **merge + archive + teardown** |
| clean + identical to base | nothing was produced | **remove + archive** (no gate needed) |

## The loop lives in the daemon

There is no router plugin. The daemon owns the whole loop — it is the component that
observes, decides, verifies, retriggers, merges, and archives. `decide()` is a pure
function of a snapshot, so every branch above is testable.

### When is an agent "still working"?

`/session/status` only reports sessions that are streaming tokens, so an agent that is
sitting inside a long-running tool shows up idle. The reliable signal for **orchestrated**
agents — the ones `orch task start` or the scheduler spawned — is the run process
itself: `opencode run` stays alive for the whole agent loop and exits when it is done.
Orch spawns those **detached** (`start_new_session`, stdin from `/dev/null`), so a run
process is orchestrated when it matches the worktree **and** has no controlling
terminal — a process property read from `/proc`, not a title. A human-attached client
(one running in a terminal) is not the agent working, so the daemon settles on session
state alone; the process's tty is the human-switch for what "busy" means.

Per claimed task, each cycle:

1. **Blocked** on a permission/question → alert (deduped by request id, in-memory).
2. **Busy** session, or an orchestrated run process alive → running; nothing. (A run
   client stuck alive past `MAX_ORCHESTRATED_S` is escaped, so it can never stall a
   task forever.)
3. **Settled** (session idle or gone) past a settle budget (~30s):
   - worktree missing → `needs-review`
   - clean + no diff → `remove` (no-op)
   - otherwise run the committed gate:
     - **pass** → the hook committed → diff → `merge`; no diff → `remove`
     - **fail** → inject the failure via `prompt_async`; retry++; `< cap` wait for the
       agent to work again, `>= cap` → `needs-review` (`retries-exhausted`)
     - **no committed gate** → `needs-review` — never a pass
4. **Fingerprint unchanged** since the last failure for longer than a stall budget →
   `needs-review` (`stalled`) — the agent was told what to fix and changed nothing.

Retries and settle timers are in-memory: a daemon restart resets them, and the task
still lands correctly because `needs-review` is recomputed from the worktree, not
stored.

## The gate cannot be edited by the agent

The gate that runs is the version **committed on the base branch**, staged to a temp
dir outside the worktree (`gate.run`). The agent's working-tree copy is never the
judge. A diverging copy is flagged as `gate-modified` but does not overturn the
verdict — the committed gate ran. Improving a gate costs a commit to base, which is
reviewable; editing it mid-task buys nothing. This is the sharpest lesson this
design learned, and it still holds with the router gone: there is simply no plugin
inside the agent's server left to trust or distrust — the daemon runs every gate.

The commit-on-pass is inside the hook, so a passing gate is the only thing that can
produce a clean, diffing branch. A dirty worktree at settle time is therefore
unambiguous: the gate never passed.

## State: one authority per question

| Question | Authority |
| --- | --- |
| What work exists, and what does it mean? | **the base branch** — `tasks/*/task.md` as committed there |
| What is claimed? | **git refs** — `refs/heads/<name>` |
| Is the work committed and passing? | **the worktree** — clean + diff + the daemon's own gate run |
| What is running right now? | **live** — `/proc`, the shared server |
| How long since… (retries, settle) | **the daemon's memory** — restart-losing by design |

## Separation of concerns

```
                          ┌──────────────────────────────┐
    phone / tailnet ──────│  orchestrator daemon         │
                          │  settle · decide · verify    │
                          │  retrigger · merge · archive │
                          └───────┬──────────────┬───────┘
                                  │ facts        │ actions
                          ┌───────▼──────┐       │  inject failure / approve / abort
                          │   scan       │       │  merge / remove / kill pid
                          │  git + /proc │       │
                          └───────┬──────┘       │
                                  │ HTTP + SSE   │
   ┌────────────────────────────▼──────────────▼────────────────────┐
   │  ONE `opencode serve` per machine (headless, always a port)    │
   │  sessions per worktree; agents run with `opencode run --attach`│
   └───────────────────────────────┬────────────────────────────────┘
                                    │ the gate
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
restart the shared server, inject a gate failure back into a session, merge a verified
branch, remove a no-op, mark `needs-review`, alert on a blocked permission. It runs
the gate itself — there is no verdict it accepts from anywhere, because there is no
plugin left to report one.

**One per machine, under systemd, with linger.** Not one per project: the shared
server is one process, the retries are one set, and project discovery is machine-wide.

A long-lived daemon holds the code it imported at startup. `doctor` therefore fails
when the running supervisor's start time precedes the newest source file — a version
string cannot do this job.

### 3. CLI — per-invocation action

| Command | Does |
| --- | --- |
| `orch init` | scaffold `.orchestration/` (config + the `session-idle` gate template) |
| `orch project create <name>` | new project from scratch: `git init` + `orch init` + a private gh repo pushed (`gh` must exist and be authenticated, else abort; `--no-gh` skips) |
| `orch task add <name…>` | scaffold `tasks/<name>/task.md` (no TASKS.md is written or generated) |
| `orch task draft "<text>"` | a one-shot agent converts free text into `tasks/` specs (review and commit) |
| `orch task start <name…>` | claim (create branch+worktree), run hooks, dispatch |
| `orch task list` | render the scan for this project |
| `orch task answer <name> allow|deny` | reply to a blocked permission (or a question's text) |
| `orch task reset <name>` | delete branch + worktree → back to `todo` (a deliberate human act) |
| `orch task archive <name>` | close a reviewed `needs-review` task (delete + archive the spec) |
| `orch task merge <name>` | re-run a merge after a conflict |
| `orch gate` | inspect whether the committed gates are trustworthy |
| `orch doctor` | verify the contracts that fail silently |
| `orch gate-run` | run the committed gate here and say why |
| `orch ask "<message>"` | converse with the project's primary agent (create on first use; `--async`, `--compact`, `--status`) |

The CLI is what agents call, via a tool. A task is a branch: `task start` refuses
when the branch already exists (the claim), and refuses an uncommitted spec or an
ungated project.

### 4. Project `.orchestration/` — project knowledge

- `hooks/session-idle` — the gate: verify, and on success commit the work. The daemon
  fires it on settle; the CLI fires it via `gate-run`.
- Lifecycle hooks: `pre-task-create`, `post-task-create`, `pre-task-cleanup`,
  `pre-branch-delete`, `post-task-cleanup`. `orch init` scaffolds git-style
  `hooks/*.sample` references for all five — inert until copied to the real name,
  made executable, and committed.
- `config.json` — `baseBranch`, `gate.include`/`exclude`/`retryCap`, `scheduler`.

Two dispatchers fire hooks from one directory under one env-var contract
(`TASK_NAME`, `BRANCH_NAME`, `WORKTREE_PATH`, `WORKTREE_NAME`): the CLI fires
task-lifecycle events, the daemon fires `session-idle`.

### 5. The primary agent — the project's own conversation

Distinct from task agents, a project can have **one persistent primary agent**
(`orch ask`). It is a session on the shared server attached to the project's
base worktree, found live by its `orch:agent` session title — no stored state,
matching the "the repo is the state" rule. First use creates it with the
`opencode run --attach` pattern (same one `task draft` uses); later asks continue the
same session via `POST /session/{id}/message`. `--compact` calls
`POST /session/{id}/summarize`, which enqueues a `compaction` step and runs
opencode's own `SessionCompaction` loop (the same path automatic context
overflow uses), never a stand-in. The primary agent edits the base worktree
uncommitted and is **outside the daemon's loop**: it is never gated, merged, or
torn down, and its work is never a task.

## Flow A — dispatch

1. `orch task start <name>` (human, parent agent) or the **scheduler** (see below).
2. CLI refuses if `refs/heads/<name>` exists — the branch is the claim, atomically.
3. CLI runs `hooks/pre-task-create` from the dispatcher's worktree.
4. CLI creates the worktree (`git worktree add -b <name> <base>`), pruning first.
5. CLI runs `hooks/post-task-create` from the new worktree.
6. CLI refuses ungated work (no committed gate) unless `--allow-ungated`.
7. CLI dispatches into the shared server, **detached** (its own session, stdin from
   `/dev/null`) — that detached start is what marks the process as an orchestrated
   agent, so the daemon can find it by "matching the worktree, no controlling tty":

   ```
   opencode run --attach $URL --dir <worktree> --format json --title <name> "<prompt>"
   ```

The dispatch process exiting is not the task finishing. Completion is derived later
from the worktree state, by the daemon.

`orch task draft "<text>"` turns a free-text description into one or more
`tasks/<name>/task.md` specs with a one-shot opencode agent (attached to the shared
server, writing into the base worktree). It never claims or dispatches the task — the
human reviews and commits the draft. It is spec-writing help, not automation: model
and wording stay opencode's and the human's.

## Flow A′ — the scheduler

The daemon can dispatch `todo` tasks itself. A project opts in via `.orchestration/config.json`:

```json
"scheduler": { "enabled": true, "maxConcurrent": 1 }
```

Each cycle, per project, the daemon fills free slots (claimed, in-flight tasks count
against `maxConcurrent`) with the next unclaimed declared task, in name order. A task
whose `task.md` is uncommitted on base, or a project with no base worktree to dispatch
from, is skipped. Failed dispatches back off (60s) and the daemon's journal records why
— `dispatch_error` is in-memory only. The scheduler never dispatches ungated work —
`--allow-ungated` is a human flag only.

**You pick which tasks run; the machine does the work.** A task needs a human by
default — `hitl: true` is the default in the front matter (the gray-matter block at
the top of `tasks/<name>/task.md`) and the scheduler skips it until a human starts it
(`orch task start`). To let the machine drain a task unattended, opt it in with
`hitl: false` (`orch task add --auto`). The flag is read from the **committed** copy
on base — never the working tree — so neither an agent nor an uncommitted edit can
auto-grant a task.

## Flow B — the gate loop

1. The task settles (session idle or gone; an orchestrated run process is held until
   it exits, so a slow agent is never torn down mid-tool).
2. The daemon runs the committed `session-idle` hook in the worktree.
3. **exit 0** → the hook committed → diff → merge + archive + teardown; no diff →
   remove + archive.
4. **non-zero** → the daemon injects the output into the session and waits for the
   agent to work again. This is bounded by `gate.retryCap`, tracked in memory.
5. **Cap spent** → `needs-review` (`retries-exhausted`). No gate → `needs-review`
   (`no-gate-configured`), because absence of checks must never be a pass.
6. **Merge conflicts** → the daemon leaves the merge **in progress** (so the state is
   derivable: the scan sees `MERGE_HEAD` and reads the task as
   `needs-review` (`merge-conflict`)) and never re-runs it. **Who resolves follows
   `hitl`**: a human resolves a `hitl: true` task in the base worktree
   (`git add`/`git commit`); a `hitl: false` task gets its own agent dispatched into
   the base worktree (`opencode run --title <name>-merge-conflict`) to resolve and
   commit, bounded by the same `retryCap` before it escalates to a human. Once the
   merge lands either way, the branch is an ancestor of base and the daemon's no-op
   path archives the task. A conflicted task is never re-gated, re-merged, or torn
   down; the resolution agent is detected by worktree + task title so a task agent in
   a sibling worktree is never mistaken for it.

The agent may assert it is done; the gate verifies and commits. Neither side can
declare success alone — and there is no side inside the agent's server that reports
a verdict at all.

## Flow C — observation and failure handling

- The daemon reads the scan every ~10s. Absence (a task whose session vanished) is as
  important as presence: a claimed task with no session and a dirty tree is
  `needs-review`.
- **Server death** → restart. Tasks settle afterwards and their worktree state
  decides: dirty → review, clean+diff → gate and merge, clean+no-diff → remove. The
  gate is the daemon's own run, so a branch that merges after a crash is verified
  work, not an unexamined claim.
- **Kill by pid, never by pattern.** The dispatch process is identified by its exact
  cmdline (`opencode run … --dir <worktree>`) and killed by that pid.

## Flow D — routing, and the human switch

The daemon's only human-facing route is the **alert**: when a task's agent is blocked
on a permission or a question, the daemon raises `alert` (deduped by request id) and
logs it. `orch` knows nothing about phones, Termux, or notify add-ons — it only
*exposes* that a human is needed, through signals any consumer can read:

- `orch task list` renders the scan — blocked sessions and every `needs-review`
  task (`--json` for machines).
- `orch task answer <name> allow|deny` replies to the blocked permission (or the
  answer text, for a question) over the shared server, unblocking the agent directly.
- The daemon's journal carries the `alert:` line (and every `needs-review` outcome).

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
4. The agent is never trusted for state. The worktree and the daemon's own gate
   decide.
5. The gate's entry point comes from the base branch; the working-tree copy is never
   the judge, and a diverging copy is flagged.
6. Absence of a gate is not a pass. `no-gate-configured` → `needs-review`.
7. A dirty worktree is not done. Only a committed, diffing branch can merge, and only
   a gate the daemon ran itself can produce one.
8. Kill by recorded pid (a specific process found by exact cmdline), never by pattern.
9. Unavailability never produces a pass. Store gone (there is none), server down,
   gate timed out, worktree gone: each lands on `needs-review`.
10. Every project-specific decision is a hook or config value, never daemon or CLI
    code.

## Settled: threat model, scope, and versions

**The threat model is a lazy agent, not a hostile one.** An agent takes the shortest
route to looking finished; it is not assumed to attack the machine.

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
- Global concurrency limits across projects (per-project `scheduler.maxConcurrent`
  exists today; a machine-wide budget is a future daemon setting).
