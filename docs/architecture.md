# Architecture

## Vocabulary

| Term | Meaning |
| --- | --- |
| **Harness** | opencode. Supplies the TUI, agents, sessions, HTTP API, plugin API, event bus. |
| **Project** | A bare git repo plus worktrees. Owns its own code, checks, database, env. |
| **Task** | A unit of work with a spec, dispatched into its own worktree with its own agent. |
| **`orch`** | The global tool: one CLI and one daemon per machine. |
| **`.orchestration/`** | Per-project, tracked, project-specific hooks and config. |
| **Router plugin** | Installed into a project's `.opencode/` by `orch init`. Turns harness events into hook invocations. |

## The criterion

Every "where does this go" question is answered by one rule:

> `.orchestration/` holds what **only this project** knows.
> `orch` holds what **every project does identically**.

Applied: creating a worktree, pruning stale ones, starting tmux, launching the TUI
with a port, writing a lease — identical everywhere, so global. Creating a
Postgres database named `reeeally_*`, rewriting `.env.local.web`, checking a
docker container, knowing that `pnpm check && pnpm test` is what "verify" means —
project knowledge, so `.orchestration/`.

## Separation of concerns

```
                        ┌──────────────────────────────┐
   phone / tailnet ─────│  orchestrator daemon         │
                        │  policy + authority + server │
                        └───────┬──────────────┬───────┘
                                │ facts        │ actions
                        ┌───────▼──────┐       │  approve / deny
                        │   observer   │       │  abort / reclaim
                        │  read-only   │       │
                        └───────┬──────┘       │
                                │ HTTP + SSE   │
   ┌────────────────────────────▼──────────────▼────────────────────┐
   │  opencode TUIs, one per worktree, each with --port             │
   │  each loads the router plugin from its branch's .opencode/     │
   └───────────────────────────────┬────────────────────────────────┘
                                   │ events → hooks
                        ┌──────────▼───────────┐
                        │  .orchestration/     │  project knowledge
                        │  hooks/<event-name>  │
                        └──────────────────────┘

   orch CLI ── claims tasks, creates worktrees, launches TUIs, writes leases
   SQLite   ── runtime truth (claims, leases, ports, retries, alert state)
   git      ── intent truth (TASKS.md, tasks/*/task.md)
```

### 1. Observer — sensing only

Discovers and reads. Holds no policy and takes no action.

- **Discovery, no registry file.** `ss -ltnp | grep opencode` yields port and pid;
  `readlink /proc/<pid>/cwd` and `GET /api/location` map a port to a worktree.
  The kernel is the registry, so nothing goes stale.
- **Reads:** `/session/status` (`{}` = idle, populated = busy), `/permission`,
  `/question`, and the `/event` SSE stream.
- **Emits facts, not verdicts:** "session X busy since T", "blocked on
  `per_fcd9…` (`external_directory`, `/tmp/opencode/*`)", "tmux alive, port
  unreachable".
- **Runs standalone** as `orch observe`. When the daemon is the broken thing, this
  is the only way to see anything.
- **Writes nothing. Ever.** No authority, so it can never make a bad situation
  worse.

Do not use `/api/session/active` or `/api/permission/request` — both returned
empty against a session that was demonstrably busy and blocked. Use the
unprefixed routes. Avoid `/experimental/*`; it is unstable by name and its
worktree list has already drifted from `git worktree list`.

### 2. Orchestrator daemon — policy and authority

Consumes observer facts, decides, and is the only component permitted to act.

- **Policy:** per-state silence budgets, retry caps, alert dedup by permission id,
  burst coalescing, concurrency limits.
- **Authority:** approve/deny permissions, `POST /session/{id}/abort`, kill a tmux
  session, reclaim an expired lease.
- **Server:** faces the phone over the tailnet. Read status *and* take action —
  approving an approval from your phone is the point, not a bonus.
- **Routing:** decides who learns about an outcome — parent agent, human, or
  nobody.
- **Stateless in memory.** Rebuilds everything on restart from SQLite plus
  `ss -ltnp`. A crash loses nothing, including children dispatched while it was
  down.

### 3. CLI — per-invocation action

| Command | Does |
| --- | --- |
| `orch init` | Install the router plugin into `.opencode/`, scaffold `.orchestration/`, write a version marker |
| `orch task start <name…>` | Claim, create worktree, run hooks, launch TUI with `--port`, write the lease |
| `orch observe` | Standalone observer output |
| `orch doctor` | Verify the launch line carries `--port`, hooks resolve, version marker matches, list emitted events |
| `orch upgrade` | shadcn-style diff of scaffolded files against the current template |

The CLI is what agents call, via a tool. It **refuses to create an agent it
cannot observe**: if the daemon is unreachable it fails loudly, with an explicit
override for when you are at the keyboard. It writes the lease row regardless, so
a daemon that dies after dispatch still reconciles on restart.

### 4. Project `.orchestration/` — project knowledge

- `hooks/<event-name>` — executables named after **what happened**, not what to
  do. Task lifecycle: `pre-task-create`, `post-task-create`, `pre-task-cleanup`,
  `post-task-cleanup`, `pre-branch-delete`. Agent lifecycle: `session-idle`,
  `session-error`, and whatever else the harness emits.
- Config: which files matter for the diff gate, the retry cap, the base branch.
- The project's own `TASKS.md` and `tasks/`.

Two dispatchers fire hooks from one directory under one env-var contract
(`TASK_NAME`, `BRANCH_NAME`, `WORKTREE_PATH`, `WORKTREE_NAME`): the **CLI** fires
task-lifecycle events, the **router plugin** fires agent-lifecycle events.

Consequence of naming hooks after harness events: the vocabulary is versioned with
opencode. Accepted — harness independence is explicitly not a goal.

## State: one authority per question

| Question | Authority |
| --- | --- |
| What work exists, and what does it mean? | **git** — `TASKS.md`, `tasks/*/task.md` |
| What is running, since when, where, on which port? | **SQLite** — one global DB per machine |
| Who is this agent's parent? | **SQLite**, not opencode's session parentage |

`- [-]` in `TASKS.md` is a **rendered convenience**: written back best-effort for
human readability, never read as authority. That kills two problems at once — the
stale `[-]` left by a dispatch that died, and two files disagreeing about state.

Claiming is one atomic statement, so no file lock is needed:

```sql
BEGIN IMMEDIATE;
UPDATE task SET status='running', lease_until=unixepoch()+900, worktree=?, port=?
 WHERE name=(SELECT name FROM task WHERE status='todo' ORDER BY seq LIMIT 1)
 RETURNING name;
COMMIT;
```

Leases expire, so a crashed agent's task becomes reclaimable rather than wedged.

Two traps: never `flock` a tracked file — git replaces inodes, so the lock stops
referring to the file everyone else sees. And git's own `index.lock` **fails**
rather than queueing, so concurrent commits produce errors, not serialization.

Parent↔child is owned by `orch` because opencode's parentage is scoped to one
project: `GET /project/current` reports the **bare repo** as `worktree` and lists
worktrees as `sandboxes`, and worktrees of one bare repo share a session list
(verified: `reeeally-main` and `reeeally-preview` return identical sessions,
`cloud-agent-infra` differs). So parentage may work across worktrees of one repo
and will not work across projects. Treat it as cosmetic.

## Flow A — dispatch

1. Human or parent agent invokes the `task_runner` tool, or `orch task start`.
2. CLI verifies the daemon is reachable. Not reachable → fail loudly (override
   available).
3. CLI claims the task in SQLite, atomically, with a lease.
4. CLI runs `hooks/pre-task-create` **from the dispatcher's worktree** — the new
   one does not exist yet.
5. CLI creates the worktree, guards against a stale directory, runs
   `git worktree prune`.
6. CLI runs `hooks/post-task-create` **from the new worktree's checkout** —
   database, `.env` rewrite.
7. CLI starts tmux and launches opencode **with `--port 0`**, carrying the task
   prompt. Strictly after step 6, or the agent boots against the wrong database.
8. CLI records port, pid, tmux name, and parent session in the lease row.
9. Best-effort: flip `[ ]` → `[-]` in `TASKS.md` and commit.

Steps 4 and 6 necessarily run different refs' hooks. Not a bug — but it means a
task branch can change its own post-create behaviour and not its pre-create
behaviour.

`--port 0` binds 4096 when free and an ephemeral port otherwise (verified: a
second instance took 37395), so concurrent worktrees never collide.

## Flow B — the event gate

The loop that makes a finished branch trustworthy.

1. The child's router plugin receives `session.idle`. Skip if `session.parentID`
   is set (a subagent) or the session errored.
2. Plugin fingerprints the project-declared file set — **content hashes**, and
   **persisted**, so a TUI restart does not silently re-baseline.
3. No difference → stop. Nothing happened worth checking.
4. Difference → acquire a concurrency slot from the daemon, then run
   `hooks/session-idle`.
5. Exit 0 → outcome `passed`, with a summary, handed to the daemon.
6. Exit non-zero → inject the failure output into the child session via
   `prompt_async`, increment the retry counter, and let the agent fix it.
7. Retry cap exceeded → halt and escalate as `stuck`. This loop is self-inflicted
   and therefore bounded on purpose.
8. **No hook of that name exists** → outcome `no-gate-configured`. Not `passed`.
   Routing decides whether an ungated project is acceptable, and `orch doctor`
   reports it. A gate that silently approves unchecked work is worse than no gate,
   because it manufactures confidence.

Inject via `prompt_async`, **not** `tui.appendPrompt` + `submitPrompt`. Driving
the TUI text box destroys whatever a human was typing, requires a TUI at all, and
races with the user.

The agent may **assert** it is done; the gate **verifies**. Neither side can
declare success alone.

## Flow C — observation and fallback

1. Observer streams `/event` and polls `/session/status` for every known port.
   Both are required: SSE catches transitions, polling catches **absence**, and
   absence is not an event.
2. The daemon applies per-state budgets — one global timeout is wrong, because it
   either fires during every legitimate test run or misses real stalls:

| State | Budget |
| --- | --- |
| busy, long tool call running | generous — healthy silence |
| busy, no output at all | tight — suspicious |
| blocked on `/permission` or `/question` | alert almost immediately; never self-resolves |
| idle with an open task | should not sit unnoticed |
| tmux alive, no reachable port | distinct alarm — the `--port` contract is broken |

3. **Definitely dead** — lease expired *and* port unreachable *and* tmux gone.
   Reclaim automatically; there is nothing left to preserve.
4. **Maybe looping** — busy, events flowing, cost climbing, todos static. Alert
   only. Auto-abort on a guessed threshold will eventually kill a legitimate long
   run, after which the automation stops being trusted.

Polling alone provably misses things. Measured on a live TUI at 2-second
sampling: two distinct approval requests (`per_fcd9baecb0`, `per_fcda1767b0`),
both `external_directory` for `/tmp/opencode/*`, appeared and cleared inside 27
seconds within a single unit of work.

## Flow D — routing, and the human switch

An outcome reaches the daemon: `passed`, `failed`, `stuck`, `stalled`,
`blocked-on-approval`, `no-gate-configured`. The daemon routes it to the parent
agent (via `prompt_async`), to the human (phone over tailnet), or to nobody.

This is why there is no `report_back` message. **A child produces a result; the
orchestrator decides who needs to know.** The child never knows whether a human
is involved.

Human-in-the-loop is therefore a **per-event toggle in config**, not an
architecture. Sensible defaults today:

| Event | Parent | Human |
| --- | --- | --- |
| approval requested | — | yes |
| stalled / stuck | yes | yes |
| checks passed | yes — unblocks dependents | optional |
| merge | — | **always** |

The human role can shrink over time by editing a table, with no code change.

One consequence to keep in view: **if a human reviews every merge, throughput is
bounded by review bandwidth, not CPU.** More parallel agents deepen the queue at
the reviewer rather than raising merges per day. The strongest argument for the
event gate is not reliability, it is that a branch arriving already type-checked
and tested is cheaper to review.

## Flow E — development, including recursion

`orch` is developed in its own repo — bare repo plus worktrees, the same ceremony
it dispatches for everyone else — and its own `.orchestration/` lives **inside**
`orch-main`, tracked. That is the point of recursion: if `orch` cannot orchestrate
`orch`, the contract is wrong.

The ceremony creates one ambiguity that has to be closed deliberately. With
several worktrees of `orch` checked out, *which* one is on `PATH`? Answer:

> **`PATH` points at an installed release, never at a worktree.** The installed
> version dispatches. A worktree copy is invoked by explicit path, under test, and
> never becomes the thing dispatching its own fix.

Without that rule a broken change to the dispatcher removes your ability to
dispatch the fix — the same discipline as editing a shell config in a shell that
sources it.

Scaffolded files are owned by the consuming project after `init` — the shadcn
model. Upgrades are a diff you review, which is why `.orchestration/` carries a
version marker and `orch upgrade` exists. The tradeoff is deliberate: projects can
edit their hooks freely, and the cost is reading a diff at upgrade time.

## Flow F — AI interactions

- **Parent agent** (architect, on `main`/`preview`): holds the dispatch tool,
  receives outcomes injected by the daemon, unblocks dependent tasks, performs
  review. Does not merge — the human does.
- **Child agent** (task worktree): has **no orchestration awareness by design**.
  It works, and the surrounding machinery observes it. It may assert completion;
  the gate decides. Failures arrive as ordinary prompts.
- **Subagents:** excluded from the gate, and the observer distinguishes them when
  judging whether an agent is working.
- **Cross-worktree conversation:** mapping owned by `orch`; opencode parentage
  used only where it happens to work.

## Invariants

1. No agent is created that cannot be observed — `--port` always, daemon
   reachable.
2. The observer never writes. The daemon holds all authority.
3. One authority per question: git for intent, SQLite for runtime.
4. A child is never trusted for state. It may assert; gates verify; observation
   decides.
5. Auto-abort only when definitely dead. Suspected loops alert.
6. Every project-specific decision is a hook or config value, never CLI or plugin
   code.
7. **Hook definitions live inside a worktree and are tracked.** Never beside the
   bare repo. A bare repo has no working tree, so anything placed next to it is
   outside version control by construction — which is exactly how
   `rdk-template`'s dispatcher and hooks ended up with no history and no backup.
   Tracked-in-a-worktree also makes hooks differ per branch, so changing how
   checks run is an ordinary reviewable change.
8. **Absence of a gate is not a pass.** A missing hook yields
   `no-gate-configured`, never `passed`. Otherwise every unconfigured project
   receives automatic green lights — the same silent blindness as a TUI launched
   without `--port`.

## Verified vs assumed

**Verified against a live TUI:** portless TUIs are unobservable (no socket, no
lock file, and the `permission` table holds zero rows — pending approvals live
only in TUI memory); `--port` turns a TUI into a queryable server; `/permission`,
`/question`, `/session/status` report real state; `session.idle` is a real event
name carrying `event.properties.sessionID`; `--port 0` does not collide;
worktrees of one bare repo share a session list.

**Assumed, not tested:** the rest of the event vocabulary beyond `session.idle`;
that `parentID` is accepted when the child's `directory` differs; concurrency
limits for N simultaneous check runs; whether the TASKS.md-as-rendered-view
decision is acceptable.

**Known skew:** local opencode is 1.18.13, the box is pinned 1.18.11. Endpoint
calls need a declared minimum version and must degrade rather than lie.
