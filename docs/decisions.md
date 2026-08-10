# Decisions and rationale

The code in this repository is deliberately comment-free. The reasoning that used to
live inline lives here instead. `docs/architecture.md` is the design; this file records
the decisions and measured lessons the code embodies, organised by module so a change
in one source file has one place to check.

## Cross-cutting

- **`orch` is stdlib-only Python.** It must run on a box that is a pinned-binary
  environment, so a dependency tree is a liability.
- **The repo is the state; there is no store.** Task intent and status are derived
  from git, runtime facts from `/proc` and the shared server, and only transient
  bookkeeping (retries, settle timers, alert dedup) lives in the daemon's memory. A
  copy of task state elsewhere — a database, a registry file — was built once and
  removed: a cache that is reconciled lazily drifts, and drift was the failure this
  design exists to prevent. The cost is a slightly weaker guarantee (retries reset on
  daemon restart), which is accepted because `needs-review` is recomputed, never
  stored.
- **Silence that reads as health is the failure mode this project exists to prevent.**
  Most of the decisions below are that sentence applied somewhere specific.

## `state.py`

- **A branch is the claim.** `refs/heads/<name>` existing *is* the task being claimed
  — atomic, reviewable, and impossible to drift. Status transitions that used to be
  database writes are now git operations.
- **Project discovery scans live.** `fd` (falling back to `find`) walks `$HOME` and
  `/mnt` (env `ORCH_ROOTS`) for `.orchestration/` dirs, bounded depth, pruning
  `.git`/`node_modules`/`.local`/`.cache`. The filesystem is the source of truth, so
  a freshly mounted disk or `orch init` is picked up within the next scan. `locate`
  was rejected precisely because it is a *snapshot* — the stale-cache pattern this
  design removes.
- **`dispatch_pid` matches the exact cmdline** (`opencode run … --dir <worktree>`),
  not `cwd`, so a hand-opened viewer (`opencode attach`) cannot masquerade as a
  dispatch and hold a task open forever.
- **The base branch is excluded from the task set.** `main` is an integration branch,
  not a task; only branches other than base are claims.
- **Archived tasks still scan.** The spec moves to `archive/`, so `task list` shows
  them as archived rather than silently forgetting the project's history.
- **The scan never writes** — it is the sensing half, safe to run when the daemon is
  broken.

## `daemon.py`

- **Policy is pure; the executor is thin.** `decide()` takes a snapshot and returns
  actions; the gate runs, the injection happens, the merge lands in `apply()`. That
  is the only reason budgets, retries and settle rules are testable at all.
- **The worktree state is the verdict.** Dirty → uncommitted (the gate never passed).
  Clean+diff → verified, merge. Clean+no-diff → nothing was produced, remove. The
  daemon still runs the gate itself before merging — "clean + diff" alone never
  stands in for a gate the daemon performed.
- **The gate commits on pass** (in the hook, idempotently), so a passing task leaves
  a clean, committed, diffing branch. The agent never commits and never decides
  completion.
- **A running gate is not an unexamined agent** — the old problem is gone because the
  daemon runs the gate itself and knows it is running; a settle budget (~30s) only
  prevents acting on a brief idle blip.
- **Retries are bounded and in-memory.** A failing gate injects the failure back up
  to `retryCap`, tracked in the daemon's memory. A restart resets the counter — the
  task still reaches `needs-review` correctly because that status is derived.
- **A fingerprint guards against re-injecting into a stuck agent.** If the worktree
  is unchanged for the stall budget after a failure, the task is `needs-review`
  (`stalled`): the agent was told what to fix and changed nothing.
- **Server death is handled by the worktree, not a special case.** Restart the
  server; tasks settle and their state decides. This is weaker than the old
  "escalate on server death" rule, but the old rule depended on stored verdicts that
  no longer exist — and the gate is the daemon's own run, so a merged branch is
  verified work.
- **Alerts dedupe by request id in memory.** One turn can raise several permission
  requests; alerting on "is /permission non-empty" fires repeatedly for one decision.
- **`needs-review` carries a reason in the log.** `retries-exhausted`, `stalled`,
  `worktree-gone`, `gate-failed` are recorded in the daemon's journal; `task list`
  re-derives the state from the worktree.
- **Actions carry their project** (the common-dir), because one daemon supervises
  every project and task names are unique only within one.
- **"Busy" is the orchestrated run process, not the session status.** `/session/status`
  only lists sessions streaming tokens, so an agent inside a long-running tool looks
  idle. `opencode run` stays alive for the whole agent loop and exits when done, so the
  daemon treats a live **detached** `opencode run` process in the worktree as the agent
  working and holds settling until it exits — no premature teardown of a slow agent.
  Detached means `start_new_session` with stdin from `/dev/null`, read from
  `/proc` (no controlling terminal) — a process property, not a title. A human-attached
  client (one running in a terminal) is not the agent working and is ignored for
  settling (the session is the truth). A run client stuck alive past
  `MAX_ORCHESTRATED_S` is escaped, so it can never stall a task forever. This restores
  the original dispatch-process signal that the earlier "session idle is always the
  truth" fix removed after misreading a 5-second SIGTERM grace as a lingering client.
- **The scheduler is opt-in per project.** `scheduler.enabled` in
  `.orchestration/config.json`; the daemon fills free `maxConcurrent` slots with the
  next unclaimed declared task. It is opt-in because auto-spawning agents is
  surprising, and it never dispatches ungated work (that flag is human-only). The
  policy is read from the **committed copy on the base branch** — the same authority
  rule as the gate — so an uncommitted edit (`orch project scheduler <n>` writes the
  working-tree copy for review) never takes effect silently, and the policy is never
  whatever `.orchestration` directory discovery happened to find first. Beyond the
  per-project cap, a **machine-wide budget** (`orch daemon budget`, stored in
  `~/.config/orch/daemon.json`) caps the scheduler's total in-flight task agents
  across all projects; like the per-project cap it bounds auto-dispatch only, never
  a human's `orch task start`.
- **A human picks each task; the machine does the work.** `hitl` is the default
  state of a task's front matter, so the scheduler runs nothing a human did not
  explicitly hand over (`hitl: false`, `orch task add --auto`). This inverts the
  earlier default: rather than the machine draining everything that wasn't exempted,
  you opt tasks *into* the machine one at a time. The flag is read from the committed
  copy on base — the same authority rule as the gate — so neither an agent nor an
  uncommitted edit can grant a task auto-dispatch.
- **The phone notify is the consumer's job, not orch's.** The daemon's `alert`
  action only logs that a human is needed (deduped by request id); `orch task list`
  renders blocked sessions and `needs-review` from the scan. orch has zero phone
  knowledge. The add-on (`~/stuff/phone-approval`) integrates with orch by polling
  `orch task list --json` or tailing the journal — the coupling points the right
  way, so adding a notifier costs orch nothing.
- **The human answers a block through the CLI, not through orch's memory.**
  `orch task answer <name> allow|deny` (or a question's text) posts to the shared
  server's permission/question endpoint for the pending request the scan already
  surfaced. It is a human act against the live server, not stored state; the daemon
  notices the unblock on its next scan.

## `dispatch.py`

- **Claim is `git worktree add -b`.** The refusal to double-claim is the branch
  already existing — checked even in dry-run, so a dry-run cannot mislead.
- **Order is the design.** Refuse an existing claim → `pre-task-create` from the
  dispatcher's worktree → create the worktree → `post-task-create` from the new one →
  verify the gate is committed → only then dispatch.
- **No gate is a refusal, not a warning.** Absence of a gate is not a pass; a
  dispatcher that proceeds would break the invariant.
- **Dispatch is detached** (`start_new_session`, stdin from `/dev/null`): it must
  survive the shell that started it and must not die from a Ctrl-C meant for the CLI —
  and the detachment doubles as the orchestration marker.
- **`--format json` makes the first leg of a run machine-readable**, but the dispatch
  exit code is never task completion — completion is derived from the worktree.
- **"Orchestrated" is a process property, not a title.** `dispatch_pid` counts a run
  process as the agent working only if it also has no controlling terminal (read from
  `/proc`) — exactly how orch starts it. A human-attached run in a terminal is never
  matched. `_kill_dispatch` finds the same process by worktree alone at teardown.

## `gate.py`

- **The gate that runs is the one committed on the base branch, never the working-tree
  copy** — because a dispatched agent was once observed rewriting its own gate and
  being reported `passed`. An agent that can edit its own gate can always pass it.
- **The staged gate copy lives outside the worktree**, so it is neither fingerprinted
  nor visible to `git status`.
- **NO_GATE is deliberately not a pass and not an error.** Absence of checks must
  never manufacture confidence.
- **A hanging gate fails, never "unknown".** Inconclusive-therefore-fine is how a
  wedged suite starts passing work.
- **`DIVERGED` does not disqualify.** The committed copy is what ran, so its verdict
  is sound; it is flagged because an agent editing its own gate is worth a human's
  attention.
- **The router is gone.** There is no plugin contract, no plugin freshness check, no
  per-directory import caching to keep in sync — the daemon runs every gate. Deleting
  the plugin deleted three whole classes of staleness problem.

## `init.py`

- **The scaffolded gate is fail-closed** and documents the contract: on pass it
  commits the work (idempotently); on failure the output is fed back by the daemon.
  `orch init` writes it as `hooks/session-idle.sample`, inert until the project
  copies it to the real name and commits it — the same opt-in contract as the
  lifecycle hooks.
- **A bare target is refused, not silently written beside.** Hook definitions must
  live inside a worktree and be tracked.
- **Samples are never a gate.** `orch init` writes every hook as an inert
  `*.sample`; a project has no gate at all until it copies one to the real name,
  makes it executable, and commits it. Nothing is overwritten without `--force`.
- **Every opencode event gets a hook, and the daemon fires it from the SSE stream.**
  `orch init` scaffolds one hook per event type (project `.sample` + global
  `.example`), dots mapped to dashes, from `src/orch/opencode_events.py` — the
  single source of truth for the vocabulary, replacing `docs/events-raw.tsv`;
  with nothing left to mirror there is no drift test. The project and global
  samples come from one template (`hooks.event_hook_template`), so the two copies
  cannot drift either. The daemon's `EventWatcher` thread reads the shared
  server's `GET /event` stream and fires a copied, executable hook per event,
  with the event JSON on stdin; a session-scoped event routes to the project
  whose worktree owns the session. Hooks are fire-and-forget: a slow hook never
  blocks the stream, and a non-zero exit only lands in the log. One reservation,
  not an omission: `session.idle` is the gate's name, so in a project it keeps
  the gate sample instead of a generic event one (globally, where there is no
  gate, it still gets an event example) — and the consumer never fires the gate
  from the stream, because the daemon already runs it on settle.
- **The stream is a trigger, not a truth source.** The SSE consumer fires hooks;
  it never decides task state. Task status still comes from git + `/proc` + the
  server's REST endpoints, so a missed event or a dropped connection degrades to
  "no notification", never to a wrong verdict. Reconnection is automatic and the
  poll loop is the source of authority.

## `merge.py`

- **Only a clean, committed branch merges.** The old "commit uncommitted work before
  merging" step is gone: uncommitted work is now `needs-review`, and auto-committing
  it would manufacture the exact "verified" stamp the gate is supposed to produce.
- **The archive is `git mv tasks/<name>/task.md archive/<name>/task.md` + a commit.**
  The spec survives, the merge commit is the log, and the active `tasks/` tree stays
  light. `git mv` needs the destination directory to exist first.
- **The merge is `--no-ff` and a conflict is left in progress, never silently retried.**
  On conflict the daemon does not abort: `MERGE_HEAD` stays, the scan derives the task
  as `needs-review (merge-conflict)`, and the daemon backs off instead of re-running
  the merge every cycle. **Resolution follows `hitl`**: a human resolves a `hitl: true`
  task in the base worktree; a `hitl: false` task gets an agent dispatched into the
  base worktree to resolve and commit, bounded by `retryCap` before it escalates to a
  human. Either way, once the merge lands the branch is merged into base and the
  no-op path archives the task. The resolution agent is detected by base-worktree path
  **and** task title, because the base worktree is shared with every task. `task merge`
  still re-runs a merge after a human has aborted or fixed one.
- **`remove_task` and `reset_task` force-delete the branch** — they are explicit
  removal acts (a no-op task produced nothing; a human decided to discard). Only
  `merge_and_cleanup` uses `-d`, preserving "a `-d` refusal means real unmerged work".
- **Kill by recorded pid**: the dispatch process is found by exact cmdline and killed
  by pid.

## `tasks.py`

- **`task add` writes only the spec.** No TASKS.md is created, appended to, or
  generated. The backlog is `tasks/*/task.md` on base; the CLI renders it.
- **An existing spec is never overwritten** — it is hand-written prose nothing can
  regenerate.
- **Being off base is warned about, not silently redirected** — a task committed on a
  feature branch is not dispatchable, and hiding that would be a silent surprise.
- **Names go through `slug`** so the directory and the branch cannot disagree about
  what the task is called.
- **`task draft` writes specs from free text, it never claims.** A one-shot opencode
  agent (the same `opencode run --attach` pattern the primary agent uses) turns a
  description into one or more `tasks/<name>/task.md` specs and reports what it
  created (`tasks/` diff before/after — never a git-status guess). It prefers one
  task unless the text clearly decomposes into independent work, never touches files
  outside `tasks/`, and never claims or dispatches — the human reviews and commits,
  so drafting lowers the cost of a good spec without removing the human from the
  loop. The raw text is appended to the prompt after formatting, so the description's
  own `{}` cannot break the template.

## `agent.py`

- **One primary agent per project, found live, never stored.** It is the session
  titled `orch:agent` in the project's base worktree on the shared server; the first
  `orch ask` creates it via the `opencode run --attach` pattern (the same one
  `task draft` uses), later asks continue the same session through
  `POST /session/{id}/message`. No registry, no file — the "repo is the state" rule
  holds for a conversation too.
- **The primary agent is outside the daemon's loop.** It works in the base worktree
  (the human's checkout), its edits stay uncommitted for the human to review, and it
  is never gated, merged, or torn down. Task worktrees branch from base HEAD, so its
  uncommitted edits never leak into a task.
- **`--compact` is opencode's own compaction, not a stand-in.**
  `POST /session/{id}/summarize` enqueues a `compaction` step and runs the message
  loop, hitting the same `SessionCompaction.process` path automatic context-overflow
  uses; it is the only working route — the v2 `POST /api/session/{id}/compact` is a
  stub that returns 503 on 1.18.x.
- **First use is `opencode run`, not `POST /session`.** The shared server starts in a
  neutral cwd, so a session created via `POST /session` would bind to the wrong
  directory. The run binds the session to the base worktree and answers the first
  message in one step.
- **Reply rendering skips synthetic parts.** Only non-synthetic `text` parts are the
  agent's actual answer; tool traces and "Called the Read tool" fillers are noise.

## `harness.py`

- **All harness knowledge lives on the Python side.** `prompt_async`
  (`POST /session/{id}/prompt_async`) is the injection channel the daemon uses to
  retrigger an agent on a failed gate.
- **Use `/permission`, `/question`, `/session?directory=`, not the `/api/*`
  equivalents** — the latter two returned empty against a session that was demonstrably
  busy and blocked.
- **`GET /session` is global, not per-server** — the `?directory=` filter is the only
  usable join from a task to its state.
- **`server.heartbeat` separates "stream alive, agent quiet" from "stream dead"** —
  the absence problem the daemon has to solve.

## `server.py`

- **One headless server per machine hosts every worktree.** A server always has a
  port (observability becomes structural) and is far cheaper than a TUI per task.
  The TUI is demoted to `opencode attach`.
- **Identity is recorded pid plus start time, never a command-line pattern.** A stale
  pid file can never kill a stranger's process.
- **Stop is SIGTERM then SIGKILL, by recorded pid, no tmux teardown.**
- **The server starts in a neutral directory** that is not a project, so its own
  config never becomes the ambient default for dispatched work.

## `doctor.py`

- **Every check exists because its failure mode is invisible.** Loud verification is
  the only defence.
- **No daemon is a FAIL, not a note** — everything appears to work until you ask for
  a result.
- **Staleness is a start-time comparison.** `doctor` reads the running supervisor's
  elapsed time (`ps -o etimes`) and fails if any source file is newer — a long-lived
  process holds what it imported at startup.
- **Linger is checked**, because it is easy to miss and silently defeats the point.

## Removed with the store and the router

- **Leases are gone.** "Definitely dead" no longer needs a lease; the settle budget
  and the worktree state replace it.
- **Verdicts are gone.** The daemon's log is the record; the merge commit is the
  verdict. A verdict a plugin reported from inside the agent's server was a
  self-report, and the agent's bash tool could forge any of it — deleting the plugin
  deleted the channel.
- **`harness_seen` history is gone.** `doctor` reads the live version against the
  floor; the dated history was a nice-to-have that required a store.
- **The `- [-]` TASKS.md write-back is gone** — there is no TASKS.md, and the branch
  is the rendered convenience now.
