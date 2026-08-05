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

Three words that are easy to use interchangeably and must not be:

| Term | Means | Established by |
| --- | --- | --- |
| **stopped** | The session is idle and its dispatch has exited. | Observation. Says nothing about the work. |
| **finished** | The agent considers the task complete. | A claim by the agent. **Currently not captured at all.** |
| **done** | The work was checked and the check passed. | A gate the daemon ran itself. |

The gap is `finished`. Nothing asks the agent whether it completed the task, so
`stopped` is currently read as if it were `finished`, and any stopped agent gets its
gate run. That conflates completion with correctness: an agent that did a fifth of the
work and quit can still leave a green suite behind, and would then be called `done`.
The gate answers "is this broken", not "is this the task". See
`tasks/completion-signal`.

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

**Headless, not TUI.** Agents run as one shared `opencode serve`; tasks are
dispatched into it with `--dir`. The TUI is demoted to an optional *viewer* you
launch with `opencode attach <url>` when you want to look, and close without
affecting the agent. This deleted three problems rather than solving them:
discovery, the `--port` contract, and the entire `unobservable` state.

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
   │  ONE `opencode serve` per machine (headless, always a port)    │
   │  per-directory: loads each worktree's own .opencode/ plugins   │
   └───────────────────────────────┬────────────────────────────────┘
                                   │ events → hooks
                         ┌──────────▼───────────┐
                         │  .orchestration/     │  project knowledge
                         │  hooks/<event-name>  │
                         └──────────────────────┘

   dispatch  ── `opencode run --attach URL --dir <worktree> --format json`
   viewing   ── `opencode attach URL`  (ephemeral, optional, stateless)
   SQLite    ── runtime truth (claims, leases, pids, retries, alert state)
   git       ── intent truth (TASKS.md, tasks/*/task.md)
```

One shared server is viable because plugin loading is **per-directory and lazy**:
verified by starting a server in one directory and creating a session in another,
whereupon the second directory's plugin was imported and its factory received
`directory`/`worktree` pointing at that second directory. So each worktree still
runs its own hooks, from one 362 MB process instead of 947 MB per task.

The cost is a single failure domain: if that server dies, every running task dies
with it. The daemon must therefore treat server death as a first-class event and
re-dispatch from leases, which it can already do because leases are in SQLite.

### 1. Observer — sensing only

Discovers and reads. Holds no policy and takes no action.

- **No discovery needed for orchestrated agents.** The daemon spawns the server,
  so it *knows* the port; `serve` also announces it on stdout
  (`opencode server listening on http://127.0.0.1:35283`). Scanning with
  `ss -ltnp` remains only to spot **stray hand-started TUIs**, which is a
  courtesy, not a mechanism.
- **Reads:** `/session/status` (`{}` = idle, populated = busy), `/permission`,
  `/question`, `/session?directory=…`, and the `/event` SSE stream.
- **The unit of observation is a session, not a process.** This changed with
  headless and it is easy to miss: one shared server has a single cwd and hosts many
  sessions in many directories, so a per-process view can never say which *task* is
  busy. `GET /session` is also global rather than per-server — it returned 33
  sessions spanning directories going back days — so `?directory=<worktree>` is the
  only usable join from a task to its state. Subagents are excluded via `parentID`:
  a child going idle is not the task finishing.
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
- **Authority:** approve/deny permissions, `POST /session/{id}/abort`, kill a
  **recorded pid**, reclaim an expired lease.

  Kill by pid, never by pattern. `pkill -f 'opencode serve'` matches the very
  shell that invokes it, so it kills the command mid-flight; it did exactly that
  here, and the same pattern would have killed an unrelated live task had the
  string matched. tmux needs no separate teardown: killing the process ends the
  session on its own (verified — pane process killed, session gone).
- **Verdicts:** runs the committed gate itself once a task stops, and is the only
  component whose `passed` may write `done`.
- **Server:** faces the phone over the tailnet. Read status *and* take action —
  approving an approval from your phone is the point, not a bonus. Not built.
- **Routing:** decides who learns about an outcome — parent agent, human, or
  nobody. Not built.
- **Stateless in memory.** Rebuilds everything on restart from SQLite plus
  `ss -ltnp`. A crash loses nothing, including children dispatched while it was
  down.

**One per machine, under systemd, with linger.** Not one per project: there is a
single shared server, a single database and a single set of leases per machine, so a
per-project daemon would have to be remembered once per project and would silently
supervise nothing for any project nobody started. Actions therefore carry their
project, because task names are unique only within one.

`orch daemon install` writes a **user** unit — the daemon runs the same gates a human
runs, against worktrees in a home directory, and needs no privilege beyond the account
that owns them. Three settings are load-bearing:

| Setting | Why |
| --- | --- |
| `Restart=always` | A supervisor that exited *cleanly* is still not supervising, and nothing else notices its absence. |
| `KillMode=process` | The shared server and every agent are started with `start_new_session` and must survive a unit restart. Verified: restarting the unit left the server untouched. |
| `loginctl enable-linger` | Without it a user unit starts at login and dies at logout, so an unattended reboot comes back supervising nothing — while `systemctl status` looked correct when you set it up. |

The absence of a daemon is a **silent** failure, which is why `doctor` fails on it
rather than noting it. Dispatch still works, agents still run, in-loop gates still
fire. But no lease is renewed and no stopped agent is ever verified, so tasks sit at
`running` forever. Everything looks healthy until you ask for a result.

### 3. CLI — per-invocation action

| Command | Does |
| --- | --- |
| `orch init` | Install the router plugin into `.opencode/`, scaffold `.orchestration/`, write a version marker |
| `orch task start <name…>` | Claim, create worktree, run hooks, dispatch `opencode run --attach … --dir <worktree>`, write the lease |
| `orch observe` | Standalone observer output |
| `orch doctor` | Verify the shared server is up, hooks are **committed** and resolve, version marker matches, list emitted events |
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
| What is checked, and how? | **the base branch** — the committed hook and config |
| Did this task pass? | **a gate run the daemon performed itself** |

The router plugin used to keep gate fingerprints and retry counters in
`.orchestration/.state/` inside the worktree being judged, which broke this table
three ways: two authorities for one question, per-worktree files that cannot express
a machine-wide limit, and state `orch observe` could not see because it reads the
database. That state now lives in the one database, reached through the CLI, and
`orch init` no longer writes a `.gitignore` entry for it.

A subprocess rather than an HTTP endpoint on the daemon, which is a deliberate
departure from what `tasks/plugin-state-via-daemon` specified. SQLite in WAL mode
already serialises writers across processes, so a socket buys nothing and adds a
service that can be **down** — and "the daemon was unreachable" must never become a
route by which a gate passes. The requirement was one authority, not one server.

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

Parent↔child is owned by `orch`, but **not** for the reason this document
previously gave. That reason was tested and turned out to be wrong.

Measured in an isolated lab — one bare repo, two worktrees, a single server:

| Child created with | Result |
| --- | --- |
| `?directory=` another worktree of the **same** bare repo | parentage **accepted**, same `projectID` |
| `?directory=` a worktree of a **different** bare repo | parentage **accepted**, *different* `projectID` |

In both cases the child's own record carried the `parentID`, and
`GET /session/{parent}/children` returned it. The earlier prediction — that
cross-project parentage "almost certainly" would not work — was false. Harness
parentage spans projects, and one server can create a session in any directory on
the machine, which is a capability worth knowing about rather than a limitation to
route around.

`orch` still owns the mapping, for the reasons that survive the test: lease rows
need worktree, port, pid and tmux regardless, and orchestration truth should not
rest on harness internals that can change between versions. But that is now a
deliberate choice, not a forced one.

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
7. CLI **commits the gate** if `.orchestration/hooks/*` is uncommitted — the router
   only honours the committed version, so dispatching with a dirty gate silently
   yields `no-gate-configured`.
8. CLI dispatches into the shared server, strictly after step 6 or the agent boots
   against the wrong database:

   ```
   opencode run --attach $URL --dir <worktree> --format json [-m model] "<prompt>"
   ```

9. CLI records pid, session id, `--dir`, and parent session in the lease row.
10. Best-effort: flip `[ ]` → `[-]` in `TASKS.md` and commit.

Steps 4 and 6 necessarily run different refs' hooks. Not a bug — but it means a
task branch can change its own post-create behaviour and not its pre-create
behaviour.

### The dispatch process exiting does not mean the task is done

Measured, not assumed. A dispatched run exited **0 at 07:25:53** with its final
`step_finish` on stdout — and the session then performed **24 further tool calls**
until **07:26:29**, because the router injected a follow-up prompt after the gate
failed. The verdict flipped `failed → passed` 36 seconds after the process that
started the work had already gone.

So the exit code answers "did my dispatch command finish", never "is the task
finished". Anything that can inject a prompt — the router, the daemon, a human on
a phone — extends a session past its dispatcher's lifetime. Completion is a
property of the **session**, observed over SSE and confirmed by a `session.idle`
with no gate work outstanding. `run --format json` is a convenient way to *start*
work and stream its first leg; it is not a completion signal.

## Flow B — the event gate

The loop that makes a finished branch trustworthy.

1. The child's router plugin receives `session.idle`. Skip if `session.parentID`
   is set (a subagent) or the session errored.
2. Plugin fingerprints the project-declared file set — **content hashes**, and
   **persisted**, so a restart does not silently re-baseline.
3. No difference → stop. Nothing happened worth checking.
4. Difference → acquire a concurrency slot from the daemon, then run the gate —
   **the version committed on the base branch**, staged to a temp dir outside the
   worktree, never the working-tree copy. See below for why.
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

### An agent must not be able to edit its own gate

The sharpest thing this design got wrong, caught by running it.

A dispatched agent was given a task, wrote the deliverable, and went idle. The
router ran the fail-closed scaffold, which failed by design, and fed the failure
back. The agent then read `.orchestration/`, **rewrote `hooks/session-idle`** into
a gate tailored to the task, went idle again — and the router reported `passed`.

The loop worked exactly as designed. That is the problem. The agent authored the
criterion it was then judged by, and a human reading `session.idle -> passed`
would have no reason to doubt it. This particular rewrite was honest — it really
did check the deliverable — but nothing structurally prevented `exit 0`. An agent
that can edit its own gate can always pass it, and "it happened to behave" is not
a security property.

Fix, now implemented in the router:

- The gate that runs is `git show <baseBranch>:.orchestration/hooks/<event>`,
  staged to a temp dir **outside** the worktree so it is neither fingerprinted nor
  visible to `git status`.
- Not committed on the base branch → `no-gate-configured`. An uncommitted gate is
  not a gate.
- Working-tree copy differs from the committed one → additionally report
  `gate-modified`, naming it for review. It does not overturn the verdict, since
  the committed gate is what ran, but an agent editing its own gate is precisely
  what a human should see.

Improving a gate therefore costs a commit to the base branch, which is reviewable.
Editing it mid-task buys nothing. Same reason `--auto` is refused: the point is
that some judgements are not the agent's to make.

Full disclosure, because the same hole applies to this repository: `orch`'s own
`.orchestration/hooks/session-idle` was written by me, and my earlier report of
"gate passed: 7 shell, 10 python, tests green" was produced by a gate I authored.
Read it as a self-report, not an independent check.

### …and must not be able to report its own verdict either

Closing the first hole exposed a second one in the same shape, one step further out.

Reading the gate from the base branch removes the agent's authority over **what is
checked**. It does nothing about its authority over **what the result was**. The
router is a plugin loaded into the agent's own server, and the agent holds a bash
tool, so any channel the router uses to say `passed` is a channel the agent can use
directly. Verified rather than assumed: playing the agent, three moves — write the
wrong answer, rewrite the local gate to `exit 0`, then call
`orch gate-report --outcome passed` by hand.

So `done` is not written by anything that reports a verdict. It is written only after
**the daemon runs the committed gate itself**, in a process the session never spawned:

| Who ran the gate | Recorded as | May promote to `done` |
| --- | --- | --- |
| the router, in-loop, inside the agent's server | `source=router` | no — it is a self-report |
| the daemon, out-of-loop, after the session stopped | `source=daemon` | yes, and only on `passed` |
| a human via `orch gate-run` | `source=cli` | no, but it is recorded |

Both runs are kept, and neither is redundant. The router's job is to hand a failure
back while the agent is still there to fix it — advisory, and harmless if wrong, since
the worst case is a wasted nudge. The daemon's job is to decide, once, afterwards. The
cost is running the suite twice on a passing task, which is the right thing to pay: the
alternative is a `done` that means "something inside the sandbox said so".

Against the three-move sabotage above, the daemon reported
`failed gate-modified`, `needs-review`, and `answer.txt says '41', expected 42` — the
forged `passed` is still in the history as `source=router`, which is exactly where a
lie belongs: recorded, attributed, and not load-bearing.

Verdicts accumulate rather than overwrite, because the question a human actually asks
is "did this ever pass, and what did the run before it say".

## Flow C — observation and fallback

1. Observer streams `/event` and polls `/session/status` for every known port.
   Both are required: SSE catches transitions, polling catches **absence**, and
   absence is not an event.

   The harness helps more than expected here. `server.heartbeat` arrives roughly
   every 8-10 seconds, which separates **"stream alive, agent quiet"** from
   **"stream dead"** — two situations that are otherwise indistinguishable and
   demand opposite responses. Missing heartbeats mean the transport broke; quiet
   with heartbeats means the agent genuinely has nothing to say. Neither
   `server.heartbeat` nor `server.connected` appears in the OpenAPI schema list;
   both were found by watching the wire.
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

   Teardown is **not atomic**: a process losing its listener slightly before it
   exits means a mid-teardown sample reads "alive, no port". Under the headless
   model this is no longer ambiguous — a server that is a server always has a
   port, so "alive, no port" simply means *dying*, and the reaper can wait one
   more sample rather than reason about it.

   An earlier revision of this document framed the above as `tmux kill-session`
   leaving an orphan, and treated tmux as the way to stop an agent. Both were
   wrong. **The process is the unit.** Kill the recorded pid; the tmux session
   ends by itself because its pane command exited (verified directly: killed a
   pane's process, the session disappeared). tmux is a place to host a process,
   never a handle on its lifetime.
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

1. No agent is created that cannot be observed. Now **structural rather than
   enforced**: agents are headless servers, and a server always has a port. This
   invariant used to require a `--port` contract, a wrapper script and a `doctor`
   check, all of which existed only because a TUI can be started blind.
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
   receives automatic green lights.
9. **The gate's *entry point* comes from the base branch.** The committed version is
   what runs; an uncommitted gate counts as no gate; a diverging working-tree copy is
   reported as `gate-modified`. Learned by watching a dispatched agent rewrite its own
   gate and then be told it passed.

   Scoped deliberately, because an earlier draft of this invariant overclaimed. What
   the hook *invokes* — test files, scripts, linters — is read from the worktree and is
   writable by the agent. That is accepted, not overlooked: hooks and the scripts they
   call are maintained on the base branch and inherited by every worktree created from
   it, so the ordinary route to changing them is a reviewable merge. Deeper pinning
   would mean a gate could not test the code under test, which is incoherent.
10. **Kill by recorded pid, never by pattern.** `pkill -f` matches the invoking
    shell and neighbouring processes. tmux sessions end on their own when their
    process dies, so they need no separate teardown.
11. **A verdict from inside the agent's server is a self-report.** `done` requires a
    gate run the daemon performed itself, out-of-loop, after the session stopped.
    Anything the router reports is recorded and attributed, never load-bearing —
    because the agent's bash tool can send whatever the router can send.
12. **Unavailability never produces a pass.** Store unreachable, gate absent, gate
    timed out, worktree gone: each lands on `needs-review`. A gate that approves
    unchecked work because a dependency was down is the failure this project exists
    to prevent, and it is the one direction that cannot be noticed later — a wrongly
    failed gate gets complained about within minutes, a wrongly passed one never
    does.

## Settled: threat model, scope, and versions

Recorded so they stop being reopened.

**The threat model is a lazy agent, not a hostile one.** An agent takes the shortest
route to looking finished; it is not assumed to attack the machine. It holds a bash
tool and could in principle kill the daemon or the server — that is accepted, and
restricting what the agent may run is a separate concern handled at the permission
layer, when the time comes. Nothing here should be designed as if the agent were an
adversary with intent.

**Authority against GitHub is the operator's, not this system's.** Push rights, branch
protection and who may write to `main` are maintained outside `orch` and are not
enforced here.

**The scope is infrastructure, not agent workflow.** What an agent is and is not
allowed to do, which model it uses, what its prompt says — all deliberately out of
scope. `orch` supplies dispatch, observation, verdicts, leases and supervision. A
project supplies its own checks through `.orchestration/`.

**The supported harness floor is 1.18.11**, because that is what the box is pinned to.
Anything newer is a bonus and must not become a requirement by accident. Minor
releases are assumed API-compatible — an assumption, so it is recorded rather than
trusted: every version actually run against is written to `harness_seen`, with the
floor in `meta`. A version change is then a dated fact instead of a mystery, and is
handled like any other dependency moving. Below the floor is a `doctor` failure, not a
warning.

**Deferred, with nothing built on either:**

- What happens to a branch that passed — merge, PR, or nothing — pending a decision on
  whether a human belongs in that loop. `done` is currently a terminus.
- Notification routing and the phone-facing server. Recorded as
  `tasks/outcome-routing`, `tasks/permission-burst-keying` and `tasks/phone-server`.
- Concurrency limits. Known and accepted; nothing caps parallel agents or gate runs.

**Recursion is possible and not sanctioned.** `orch` can dispatch agents onto its own
backlog: its repo is a bare repo with worktrees, its tasks are in `TASKS.md`, and
`orch task start` on one of them produces a valid dispatch. Verified as a mechanism
only. It has never been used, because agents editing the gate, router and daemon that
supervise them makes this system's own safety properties the work product of the
agents it supervises. Not a technical blocker — an unmade decision.

## Language and distribution

**Decided: Python/bash for the CLI and daemon, TypeScript for the router plugin.**

The plugin has no choice — the opencode plugin API is TypeScript. So the real
question was never "avoid TS", it was one language or two, and two was chosen to
keep dispatch free of a node dependency.

The cost of two languages is duplicated harness knowledge, and the mitigation
falls out of the component split: **all HTTP knowledge of the harness lives on the
Python side.** The observer and CLI own `/session/status`, `/permission`,
`/question`, `/event`, `/api/location`, and version negotiation. The TS plugin
stays deliberately thin — receive an event, apply the diff gate, invoke a hook,
record the outcome by calling `orch`. It needs no HTTP vocabulary at all, so there is
nothing to keep in sync: the two CLI subcommands it uses (`gate-state`,
`gate-report`) are versioned with the code that implements them.

Open check before relying on this on the box: plugins execute inside opencode's
runtime, but the observed plugin subprocess used a mise-managed system node
(`~/.local/share/mise/installs/node/latest/bin/node`). Whether a plugin runs on the
box's deliberately pinned-binary install is unverified.

## Verified vs assumed

**Verified headless (2026-08-05, 1.18.13), the run that reshaped this design:**

| Claim | Evidence |
| --- | --- |
| `serve` announces its own port | prints `opencode server listening on http://127.0.0.1:35283` on stdout, so discovery is unnecessary |
| Headless is far cheaper | `serve` 362 MB vs TUI 947 MB RSS — 2.6×, so memory stops being the concurrency ceiling on an 8 GB box |
| One server serves every worktree | server started in dir A, session created with `?directory=B` → B's plugin imported, factory got `directory=B worktree=B` |
| Both plugin dir spellings load | `.opencode/plugin/` and `.opencode/plugins/` each worked |
| A TUI can be attached on demand | `opencode attach <url>` exists, so headless is not blind |
| Dispatch works end to end | `opencode run --attach URL --dir W --format json` → exit 0, JSON event stream, `cost: 0` on a `-free` model |
| **Exit code ≠ completion** | run exited 0 at 07:25:53; session did 24 more tool calls until 07:26:29 after the router injected a prompt |
| **An agent will rewrite its own gate** | fail-closed scaffold → failure injected → agent rewrote `hooks/session-idle` → reported `passed` |
| The retry loop is genuinely bounded | `failed: attempt 1/3` then `passed`, and the retry counter reset to 0 |
| Killing a process ends its tmux session | killed a pane's process; session gone without `kill-session` |
| `pkill -f` is dangerous | `pkill -f 'opencode serve'` killed its own invoking shell mid-command |
| Free models exist | `opencode/deepseek-v4-flash-free` et al. report `cost: 0`, so mechanism tests need not cost money |
| Basic auth is available | `serve` warns `OPENCODE_SERVER_PASSWORD is not set; server is unsecured`; `run`/`attach` take `-u`/`-p` — relevant to the tailnet server |

**Superseded but kept for the record** — the following concerned TUIs and no longer
constrains the design, since orchestrated agents are headless:

**Verified against a live TUI:** portless TUIs are unobservable (no socket, no
lock file, and the `permission` table holds zero rows — pending approvals live
only in TUI memory); `--port` turns a TUI into a queryable server; `/permission`,
`/question`, `/session/status` report real state; `session.idle` is a real event
name carrying `event.properties.sessionID`; `--port 0` does not collide;
worktrees of one bare repo share a session list.

**`--help` lies about `--port`.** It reports `--port [default: 0]`, which suggests
a server always starts. It does not. Verified on 1.18.13 by launching a TUI two
ways in a scratch repo:

| Launch | Listener |
| --- | --- |
| `opencode` | **none** |
| `opencode --port 0` | `127.0.0.1:39243` (4096 was taken) |

So the flag must be passed explicitly, and a shell wrapper is the only thing that
makes hand-opened TUIs observable. The wrapper is still worth keeping for TUIs you
open by hand — it demonstrably works, and made a real `--agent team-lead` task
observable without anyone thinking about it — but it is no longer load-bearing for
orchestration.

**Verified since:** the full event vocabulary (93 schema types plus two
undocumented ones — see `docs/events.md`); `parentID` is accepted across differing
`directory`, both within one bare repo and across two; `/api/health` omits the
version while `/global/health` carries it; `--help` misreports `--port` as
defaulting to 0 when omitting it starts no server at all.

**Assumed, not tested:** concurrency limits for N simultaneous check runs; whether
a TS plugin runs under the box's pinned opencode; whether the
TASKS.md-as-rendered-view decision is acceptable; how a shared server behaves when
one hosted worktree's plugin throws; whether `run` blocks indefinitely or gives up
when a permission is genuinely withheld.

**A caution about "block and notify":** the chosen policy is never `--auto`, and
that stands. But the dispatch above wrote a file **without any prompt** — the
default resolved to `action=allow` for `write` and `bash`. So blocking is not the
default behaviour that policy assumes; it only happens where a project's
permission config actually demands it. Getting agents to stop and ask is a
configuration job, not something the absence of `--auto` achieves by itself.

**Version skew is now a declared contract, not a known risk.** The floor is 1.18.11
(the box's pin); local runs 1.18.13. `harness.MIN_VERSION` and `store.MIN_HARNESS`
carry it, `doctor` fails below it, and every version seen is recorded in
`harness_seen` so a change is dated rather than discovered. Endpoint calls must still
degrade rather than lie across the gap.
