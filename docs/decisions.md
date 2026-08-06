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
  daemon treats a live `--title orch:<task>` process as the agent working and holds
  settling until it exits — no premature teardown of a slow agent. A human-attached
  client carries no `orch:` title and is ignored for settling (the session is the
  truth). A run client stuck alive past `MAX_ORCHESTRATED_S` is escaped, so it can
  never stall a task forever. This restores the original dispatch-process signal that
  the earlier "session idle is always the truth" fix removed after misreading a
  5-second SIGTERM grace as a lingering client.
- **The scheduler is opt-in per project.** `scheduler.enabled` in
  `.orchestration/config.json`; the daemon fills free `maxConcurrent` slots with the
  next unclaimed declared task. It is opt-in because auto-spawning agents is
  surprising, and it never dispatches ungated work (that flag is human-only).
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

## `dispatch.py`

- **Claim is `git worktree add -b`.** The refusal to double-claim is the branch
  already existing — checked even in dry-run, so a dry-run cannot mislead.
- **Order is the design.** Refuse an existing claim → `pre-task-create` from the
  dispatcher's worktree → create the worktree → `post-task-create` from the new one →
  verify the gate is committed → only then dispatch.
- **No gate is a refusal, not a warning.** Absence of a gate is not a pass; a
  dispatcher that proceeds would break the invariant.
- **Dispatch is detached** (`start_new_session`): it must survive the shell that
  started it and must not die from a Ctrl-C meant for the CLI.
- **`--format json` makes the first leg of a run machine-readable**, but the dispatch
  exit code is never task completion — completion is derived from the worktree.
- **The session title is the orchestration marker.** Dispatches get `--title orch:<task>`.
  It is the exact token `dispatch_pid` requires before a run process counts as the
  agent working, and it is the same token `_kill_dispatch` uses to find the right
  process at teardown. Human-attached runs pick their own title and are never matched.

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
- **A bare target is refused, not silently written beside.** Hook definitions must
  live inside a worktree and be tracked.
- **`--force` must never clobber a gate** — the most valuable file in a project, its
  loss unrecoverable. `--force-hooks` is a separate, explicit act.

## `merge.py`

- **Only a clean, committed branch merges.** The old "commit uncommitted work before
  merging" step is gone: uncommitted work is now `needs-review`, and auto-committing
  it would manufacture the exact "verified" stamp the gate is supposed to produce.
- **The archive is `git mv tasks/<name>/task.md archive/<name>/task.md` + a commit.**
  The spec survives, the merge commit is the log, and the active `tasks/` tree stays
  light. `git mv` needs the destination directory to exist first.
- **The merge is `--no-ff` and a conflict aborts cleanly**, leaving the worktree and
  branch for a human; `task merge` re-runs it after resolution, and the daemon backs
  off for a while so it does not retry the conflict every cycle.
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
  the absence problem the watchdog has to solve.

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
