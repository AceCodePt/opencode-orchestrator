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
- **The repo is the tracker; GitHub issues are not.** Work is tracked in the
  project's own `tasks/` — a spec file, a branch as the claim, the daemon as the
  verification and merge — with ordering expressed as `dependencies:` front matter. The
  earlier workflow that drove an external issue tracker (GitHub issues, triage
  labels, blocking links, agent briefs as comments) was removed: a second tracker
  was a second store, and every answer drifted toward the repo. What survived is
  GitHub as *hosting only*: `orch project create` still pushes a private repo, but
  nothing reads or writes issues, labels, or PRs.

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
- **Dispatch is a server session, not a subprocess.** Task agents are created with
  `POST /session?directory=<worktree>` plus `prompt_async`, so there is no `opencode run`
  process to scan for or kill. A hand-opened viewer (`opencode attach`) attaches to the
  same shared server and cannot create a second claim — the branch is the claim.
- **The base branch is excluded from the task set.** `main` is an integration branch,
  not a task; only branches other than base are claims.
- **`project base` is clone-parity, and it is non-destructive.** Repointing an
  existing project's base branch (`orch project base <branch>`) makes it behave as if
  it had been cloned with `--base <branch>` — the same semantics, so there is one
  rule for what the base branch means no matter how the project got it. The branch
  must **already exist** (the command never auto-creates one) and the old base branch
  is **not removed** — it stays in place as an ordinary checkout, which is a deliberate
  choice: repointing changes only which branch is *base*, never the branch's existence.
  It is a single per-project base repoint, not multi-environment support.
- **Archived tasks still scan.** The spec moves to `archive/`, so `task list` shows
  them as archived rather than silently forgetting the project's history.
- **The scan never writes** — it is the sensing half, safe to run when the daemon is
  broken.

## `daemon.py`

- **Policy is pure; the executor is thin.** `decide()` takes a snapshot and returns
  actions; the verification hook runs, the injection happens, the merge lands in
  `apply()`. That
  is the only reason budgets, retries and settle rules are testable at all.
- **The git state is the verdict.** The agent declares done by committing its work;
  a committed, clean, diffing branch is the signal, and nothing is stored anywhere else
  to record it. The daemon still runs the verification hook itself before merging —
  "clean + committed" alone never stands in for a verification the daemon performed.
- **The hook commits on pass** (idempotently), so a passing task leaves
  a clean, committed, diffing branch even when the agent did not commit everything. The
  agent decides when it believes the work is done and commits; the daemon decides
  acceptance.
- **A running verification hook is not an unexamined agent** — the old problem is
  gone because the daemon runs it itself and knows it is running; a settle budget
  (~30s) only
  prevents acting on a brief idle blip.
- **Retries are bounded and in-memory.** A failing verification hook injects the
  failure back up
  to `retryCap`, tracked in the daemon's memory. A restart resets the counter — the
  task still reaches `needs-review` correctly because that status is derived.
- **A fingerprint guards against re-injecting into a stuck agent.** If the worktree
  is unchanged for the stall budget after a failure, the task is `needs-review`
  (`stalled`): the agent was told what to fix and changed nothing.
- **Server death is handled by the worktree, not a special case.** Restart the
  server; tasks settle and their state decides. This is weaker than the old
  "escalate on server death" rule, but the old rule depended on stored verdicts that
  no longer exist — and the verification is the daemon's own run, so a merged
  branch is
  verified work.
- **Alerts dedupe by request id in memory.** One turn can raise several permission
  requests; alerting on "is /permission non-empty" fires repeatedly for one decision.
- **`needs-review` carries a reason in the log.** `retries-exhausted`, `stalled`,
  `worktree-gone`, `verify-failed` are recorded in the daemon's journal; `task list`
  re-derives the state from the worktree.
- **Actions carry their project** (the common-dir), because one daemon supervises
  every project and task names are unique only within one.
- **"Busy" is the session status, and it is enough.** opencode's `/session/status`
  reports `busy` for the whole agent loop (LLM turns and long-running tool calls) and
  `idle` when the agent is done; a pending `/permission` or `/question` ask for the
  worktree is `blocked`. Dispatch no longer spawns an `opencode run` process, so there
  is no process-based signal to hold settling on — the session state plus the worktree
  state and the verification hook are the truth. Blocked asks stay pending until a human
  answers them: the headless `opencode run` loop used to auto-reject (or auto-approve
  with `--auto`) every ask before a human could see it.
- **A user interrupt and an orphaned ask are derived from the message record, never
  stored.** When a turn is stopped in the TUI (`POST /session/{id}/interrupt`), opencode
  records it on the message (`MessageAbortedError`, the tool part's
  `metadata.interrupted`) but does **not** garbage-collect the permission ask that turn
  created — so an idle session can carry a pending ask forever, and a scan that treats
  "a pending ask exists" as `blocked` would freeze the task for good. `observe.look_task`
  therefore reads the session's recent messages: if the **last** assistant message was
  aborted the session is `interrupted`, and a pending ask whose **owning message** was
  aborted is **orphaned** (never a live block, and the daemon replies `reject` to clear
  it). An interrupted task with work (`dirty` or `diff`) is held as `needs-review`
  (`interrupted`) — the human stopped the turn and must verify it, so it is never
  auto-merged or auto-committed; an interrupted task with no produced work still closes
  as a no-op. Only live asks (owning message not aborted) keep the `blocked` alert flow.
- **The scheduler is opt-in per project.** `scheduler.enabled` in
  `.orchestration/config.json`; the daemon fills free `maxConcurrent` slots with the
  next unclaimed declared task. It is opt-in because auto-spawning agents is
  surprising, and it never dispatches a task whose front matter says
  `wait_human_start: true`. The
  policy is read from the **committed copy on the base branch** — the same authority
  rule as the verification hook — so an uncommitted edit (`orch project scheduler <n>` writes the
  working-tree copy for review) never takes effect silently, and the policy is never
  whatever `.orchestration` directory discovery happened to find first. Beyond the
  per-project cap, a **machine-wide budget** (`orch daemon budget`, stored in
  `~/.config/orch/daemon.json`) caps the scheduler's total in-flight task agents
  across all projects; like the per-project cap it bounds auto-dispatch only, never
  a human's `orch task start`.
- **The machine runs and merges by default; the human opts in per task.** Two
  front-matter flags, both default false: `wait_human_start: true` keeps the
  scheduler from auto-dispatching a task until a human runs `orch task start`;
  `wait_human_merge: true` verifies and commits a task but leaves it ready for
  `orch task merge <name>` instead of auto-merging. This decomposes the earlier
  `hitl` flag (which meant "a human starts it" and also "a human resolves its
  conflicts") into one flag per human checkpoint. Both flags are read from the
  committed copy on base — the same authority rule as the verification hook — so
  neither an agent nor an uncommitted edit can grant or revoke them.
- **The phone notify is the consumer's job, not orch's.** The daemon's `alert`
  action only logs that a human is needed (deduped by request id); `orch task list`
  renders blocked sessions and `needs-review` from the scan. orch has zero phone
  knowledge. The add-on (`~/stuff/phone-approval`) integrates with orch by polling
  `orch task list --json` or tailing the journal — the coupling points the right
  way, so adding a notifier costs orch nothing.
- **The scheduler conditions dispatch on `dependencies:` front matter.** A declared `todo` task
  dispatches only when every dependency slug is **archived** — or was never declared,
  because a slug that never existed cannot block. An open declared task with the same
  name always holds its dependents, even one `reset` back to `todo`. The frontier is
  exactly the all-dependencies-archived set, so the map of what is grabbable is
  derived, never stored.
- **Task references are slugs that render as links.** The body-level
  `- **Dependencies:** none` line is gone; blocking lives in the front matter as
  `dependencies: [<slug>, ...]`, where a slug is the sibling `tasks/<slug>/` name.
  The scheduler parses slugs (never links); a link is only how a task renders a
  reference to another task.
- **A dependency typo is a permanently blocked task, and `doctor` says so.** A slug
  that is neither declared nor archived can never satisfy the dependency check, so
  `check_task_dependencies` fails loudly on it.
- **The human answers a block through the CLI, not through orch's memory.**
  `orch task answer <name> allow|deny` (or a question's text) posts the reply to the
  shared server's permission endpoint for the pending request the scan already surfaced:
  `POST /permission/{requestID}/reply?directory=<worktree>` with `once`/`reject` (allow
  maps to `once`, deny to `reject`). It is a human act against the live server, not
  stored state; the daemon notices the unblock on its next scan.

## `dispatch.py`

- **Claim is `git worktree add -b`.** The refusal to double-claim is the branch
  already existing — checked even in dry-run, so a dry-run cannot mislead.
- **Order is the design.** Refuse an existing claim → `pre-task-create` from the
  dispatcher's worktree → create the worktree → `post-task-create` from the new one →
  note whether a verification hook is committed → dispatch.
- **Dispatch is a server-side session, not a spawned process.** `POST /session?directory=<worktree>`
  binds the session to the task worktree (the workspace-routing middleware honours the
  query), then `POST /session/{id}/prompt_async` sends the task prompt. Nothing is
  forked, so a Ctrl-C meant for the CLI cannot touch the agent, and there is nothing to
  detach or kill at teardown. The trade that buys the permission gate: the headless
  `opencode run` loop that used to auto-answer every ask is gone, so asks stay pending
  until a human answers them.
- **Completion is never the dispatch call's return.** The agent declares done by
  committing its work; the daemon then derives completion from the git state (clean +
  committed + diffing) once the session idles.
- **Task agents commit with the machine's existing git identity.** Per-worktree
  identity was tried and rejected: it needs `extensions.worktreeConfig`, and enabling it
  in the shared config breaks every existing linked worktree of a bare-repo project
  (the `project create` layout) that lacks a `config.worktree` file — git then refuses
  `git status` in the base worktree with "must be run in a work tree". So nothing is
  configured at dispatch; the agent commits with whatever identity exists (global or
  repo-local), exactly like the merge-resolution agent, and the verification hook's
  pass-commit still uses the explicit `-c user.name=orch` identity.

## `verify.py`

- **The hook that runs is the one committed on the base branch, never the
  working-tree copy** — because a dispatched agent was once observed rewriting its
  own verification hook and being reported `passed`. An agent that can edit its own
  verification hook can always pass it.
- **The staged hook copy lives outside the worktree**, so it is neither
  fingerprinted
  nor visible to `git status`.
- **NO_VERIFY is the implicit unverified state, not a failure.** Verification is
  opt-in per project: committing the `session-idle` hook turns it on; its absence
  means the daemon accepts the work without verification. There is deliberately no
  switch to turn verification off — the presence or absence of the hook is the whole
  statement. A present hook that fails still never passes; only a present-and-passing
  (or absent-and-implicitly-accepted) task reaches done.
- **A hanging verification hook fails, never "unknown".** Inconclusive-therefore-fine is how a
  wedged suite starts passing work.
- **`DIVERGED` does not disqualify.** The committed copy is what ran, so its verdict
  is sound; it is flagged because an agent editing its own verification hook is
  worth a human's
  attention.
- **The router is gone.** There is no plugin contract, no plugin freshness check, no
  per-directory import caching to keep in sync — the daemon runs every
  verification hook. Deleting
  the plugin deleted three whole classes of staleness problem.

## `init.py`

- **The scaffolded verification hook is fail-closed** and documents the contract:
  on pass it
  commits the work (idempotently); on failure the output is fed back by the daemon.
  `orch init` installs it machine-level as `~/.config/orch/hooks/session-idle.example`,
  inert until the project copies it to `hooks/session-idle`, makes it executable, and
  commits it — the same opt-in contract as the lifecycle hooks.
- **A bare target is refused, not silently written beside.** Hook definitions must
  live inside a worktree and be tracked.
- **Templates are never hooks.** `orch init` writes every template as an inert
  `*.example` in `~/.config/orch/hooks/`; nothing template-shaped is ever scaffolded
  into a repo, and `.gitignore` rules (`*.sample`, `*.example`) keep even copied-in
  template files untracked. A project has no verification hook at all until it copies
  a template to the real name,
  makes it executable, and commits it. Nothing is overwritten without `--force`.
- **Every opencode event gets a hook, and the daemon fires it from the SSE stream.**
  `orch init` installs one template per event type (machine-level `.example`),
  dots mapped to dashes, from `src/orch/opencode_events.py` — the
  single source of truth for the vocabulary, replacing `docs/events-raw.tsv`;
  with nothing left to mirror there is no drift test. All templates come from one
  source (`hooks.event_hook_template` for events, `SESSION_IDLE_TEMPLATE` for the
  verification hook, `TASK_HOOK_TEMPLATE` for the lifecycle hooks), so the copies
  cannot drift either. The daemon's `EventWatcher` thread reads the shared
  server's `GET /event` stream and fires a copied, executable hook per event,
  with the event JSON on stdin; a session-scoped event routes to the project
  whose worktree owns the session. Hooks are fire-and-forget: a slow hook never
  blocks the stream, and a non-zero exit only lands in the log. One reservation,
  not an omission: `session.idle` is the verification hook's name, so its template is
  the verification one (there is no separate event template for it) — and the consumer
  never fires the verification hook
  from the stream, because the daemon already runs it on settle.
- **The stream is a trigger, not a truth source.** The SSE consumer fires hooks;
  it never decides task state. Task status still comes from git + `/proc` + the
  server's REST endpoints, so a missed event or a dropped connection degrades to
  "no notification", never to a wrong verdict. Reconnection is automatic and the
  poll loop is the source of authority.

## `merge.py`

- **Only a clean, committed branch merges.** The agent is expected to commit its work as
  the declaration of done; the old "commit uncommitted work before merging" step is gone,
  and the daemon never auto-commits a dirty branch as a shortcut past its own
  verification — the verification hook's pass-commit (and the no-hook implicit acceptance)
  are the only commits that stand in for a verification the daemon performed.
- **The archive is `git mv tasks/<name>/task.md archive/<name>/task.md` + a commit.**
  The spec survives, the merge commit is the log, and the active `tasks/` tree stays
  light. `git mv` needs the destination directory to exist first.
- **The merge is `--no-ff` and a conflict is left in progress, never silently retried.**
  On conflict the daemon does not abort: `MERGE_HEAD` stays, the scan derives the task
  as `needs-review (merge-conflict)`, and the daemon backs off instead of re-running
  the merge every cycle. **Resolution follows `wait_human_start`**: a human resolves
  a human-started task in the base worktree; a machine-started task gets an agent
  dispatched into the
  base worktree to resolve and commit, bounded by `retryCap` before it escalates to a
  human. Either way, once the merge lands the branch is merged into base and the
  no-op path archives the task. The failed-merge backoff only delays *re-attempts*,
  never the sweep: a clean branch identical to base is already merged, so the
  no-op archive is not held hostage by the stale `wait_until`. The resolution agent
  is detected by base-worktree path **and** task title, because the base worktree is
  shared with every task. `task merge`
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
- **`task draft` is gone; intake is an opencode conversation over custom tools.**
  The one-shot CLI draft (`opencode run --attach` writing specs from a hardcoded
  prompt) is replaced by four machine-global opencode custom tools —
  `create_task`, `update_task`, `archive_task`, `delete_task` — installed by orch
  into `~/.config/opencode/tools/` (the same config dir the shared server loads, so
  headless sessions and any local TUI get them). The tools are thin zod-validated
  bridges to a Python backend (`bin/orch tasktool` — a hidden dispatcher
  subcommand), so slugs, front matter, dependencies and refusals share orch's code.
  **The tools commit their change to the base branch by default.** The
  human-in-the-loop gates are the wait flags — `wait_human_start` (a human starts
  the task) and `wait_human_merge` (a human merges verified work) — exactly like
  the daemon's automatic merge. A task with `wait_human_start: true` stays an
  uncommitted draft the human reviews before it is committed; a task with no human
  gate is committed on the spot so the daemon can dispatch it and the loop runs on
  its own. `commit: false` (or `commit: true` to override a wait gate) opts out.
  The tools never claim or dispatch a task, and they never trust a project.
- **The wait flags are decided in the conversation, never hardcoded.** The old
  prompt baked in `wait_human_start: true` for the spec task and `false` for the
  slices. `create_task` takes `wait_human_start`/`wait_human_merge` as optional
  booleans that the agent passes as the human's preferences surfaced in the
  conversation — they default to `false` (scheduler-eligible, auto-merge) when
  not passed. The tool validates their type, but the decision is the
  conversation's, not the tool's.
- **The decomposition teaching lives in the tool description, not a skill.** The
  spec-and-slices workflow (a spec task anchoring one or more vertical slice tasks,
  `dependencies:` blocking edges, expand-contract for wide refactors,
  behavior-over-procedure, runnable Verifications) is carried by `create_task`'s
  own description. A skill was considered and dropped: it only fires if a model
  decides to load it, and it would be a second source of truth that can drift from
  the tools. The tool is always present, so its description is the single contract.
- **Agent control is bounded by the tool, not by config.** Because the tools are
  machine-global, they are callable from any opencode session, so a per-project
  config gate would be theater. The safety invariants live in the tool itself:
  refuse outside a project, refuse off the base branch, refuse a claimed task,
  refuse `delete_task` while any open declared task depends on it.

## `agent.py`

- **One primary agent per project, found live, never stored.** It is the session
  titled `orch:agent` in the project's base worktree on the shared server; the first
  `orch attach` creates it via `POST /session?directory=<base>`, and every later
  conversation continues the same session through opencode's own attach TUI. No
  registry, no file — the "repo is the state" rule holds for a conversation too.
- **`orch ask` was removed; `orch attach` replaced it, because a second client was
  more inconvenient than useful.** The headless ask surface — a blocking reply, a
  *thinking* spinner, `--stream`, `--async`, `--compact`, `--status` — duplicated a
  tool the human already has: opencode's interactive TUI. Every feature was a shim
  that had to keep up with opencode's own conversation UI (streaming parts, thinking,
  tool traces, compaction, permission handling), which is a treadmill: the moment
  opencode's UI improves, the shim drifts. Managing the conversation through the TUI
  is simpler because there is nothing to maintain — orch only resolves the project's
  server and session, and opencode owns the conversation. `orch attach` is therefore
  a **thin resolver, not a client**: it finds or creates the session id and `exec`s
  `opencode attach <url> --session <id> --dir <base>`, so the interactive TUI owns
  the terminal and conversation features stay opencode's. The one thing the headless
  client *did* provide — sending a message without opening the TUI (`--async`) — was
  kept reachable through the same session from any opencode client, and answering
  blocked permission asks stays a first-class `orch permissions` surface.
- **The primary agent is outside the daemon's loop.** It works in the base worktree
  (the human's checkout), its edits stay uncommitted for the human to review, and it
  is never verified, merged, or torn down. Task worktrees branch from base HEAD, so its
  uncommitted edits never leak into a task.
- **The primary agent is a server session like any task agent — no run loop.**
  Creation is `POST /session?directory=<base>` with no `prompt_async`; nothing ever
  spawns `opencode run`. That means the primary agent is held to the same permission
  gate as task agents: an ask stays pending (surfaced as a `primary-agent@<base>`
  owner in `orch permissions` and shown live in the attached TUI) instead of being
  auto-rejected by a headless run loop. The workspace-routing middleware binds the
  session to the base worktree, so a neutral server cwd is no obstacle.

## `cli.py`

- **`orch permissions` is coverage, not policy.** An approval is still a one-time
  reply to one pending request — the interactive walk, the one-shot, and the list
  all hit the same `reply_permission`/`reply_question` endpoints and persist nothing.
  Persistent tiers of allow/deny rules (global / project / task) were considered and
  rejected: they would be a new state store the design has none of, and they would
  quietly make approval not-one-time. The "three levels" the walk offers are scopes —
  task, project, machine — not layers of policy.
- **Interactive by default; a flag is the non-interactive switch.** The TTY gate
  guards only the flagless default. `--task <name> allow|deny [--reason]` (one-shot),
  `-C <dir>` (list this project's asks), and `--json` all skip it, so the command
  never hangs in a script or a notification flow. `-C` alone means "list, don't
  prompt", which is why an interactive walk for a specific project must be run from
  inside that project.
- **A permission ask is described by what the agent is trying to run, not the
  permission glob.** The server's ask carries tool-specific `metadata`
  (`filepath`, `command`, `url`, …); rendering the glob made a denial unjudgeable
  ("external_directory: /tmp/*" vs "read /tmp/x.sh"). `observe._describe_permission`
  prefers the metadata target and falls back to the resource glob only when metadata
  carries nothing. The same text feeds the walk, the `--json`/`-C` lists, and the
  daemon's alert lines, so a human never decides on a glob.
- **A deny explains itself and keeps the agent working.** Rejecting a tool call ends
  the agent's turn; without a reason the model stops, the session idles, and the
  daemon sweeps the clean, unchanged task as a no-op. So a deny sends its reason both
  in the reply's `message` field (the rejected tool result tells the agent why) and
  as a `prompt_async` nudge to adjust and continue. A denial becomes a conversation —
  the agent responds, tries a different approach, and may raise a fresh ask for the
  human to decide again — instead of a dead end. The empty reason falls back to a
  generic denial so the flow never blocks on the human's typing.

## `harness.py`

- **All harness knowledge lives on the Python side.** `prompt_async`
  (`POST /session/{id}/prompt_async`) is the injection channel the daemon uses to
  retrigger an agent on a failed verification hook.
- **Use `/permission`, `/question`, `/session?directory=`, not the `/api/*`
  equivalents** — the latter two returned empty against a session that was demonstrably
  busy and blocked.
- **`/permission` and `/question` are workspace-scoped.** The list endpoints only
  return asks for the workspace named in the `?directory=` query, so every call must
  pass the worktree — an unscoped call silently reads as "nobody is blocked". The
  reply endpoint is `POST /permission/{requestID}/reply?directory=<worktree>` with
  `once`/`always`/`reject`; the dead v1 path was `POST /session/{id}/permissions/{id}`
  with `{response: allow|deny}`.
- **The permission reply takes an optional `message`.** It is the rejection text the
  agent sees in its tool result, so a deny can explain itself in the agent's own
  context rather than only in the human's terminal. The explainable-denial flow in
  `cli.py` relies on it (alongside a `prompt_async` nudge to keep the session busy).
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
