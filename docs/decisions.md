# Decisions and rationale

The code in this repository is deliberately comment-free. The reasoning that used to
live inline lives here instead. `docs/architecture.md` is the design; this file records
the decisions and measured lessons the code embodies, organised by module so a change
in one source file has one place to check.

## Cross-cutting

- **`orch` is stdlib-only Python.** It must run on a box that is a pinned-binary
  environment, so a dependency tree is a liability. The only exception is the router
  plugin, which is TypeScript because the opencode plugin API is TypeScript — the TS
  plugin is kept thin and knows no HTTP vocabulary, so there is nothing to keep in sync.
- **Two authorities, deliberately.** Git holds intent (what tasks exist and mean);
  SQLite holds runtime (what is actually running). `- [-]` in `TASKS.md` is a rendered
  convenience written back best-effort, never read as authority. See architecture.md
  "State: one authority per question".
- **Silence that reads as health is the failure mode this project exists to prevent.**
  Most of the decisions below are that sentence applied somewhere specific.

## `cli.py`

- **Bare `orch` prints help and exits 0.** An empty invocation is a request for help,
  not an error; the `required=True` subparser exists to catch a wrong first word, not to
  scold an empty one.
- **`events` demands a `--port` when the choice is ambiguous.** With several observable
  TUIs, silently tailing the first would depend on whatever `pgrep` happened to sort
  first. With one, tail it; with several, list them and ask.
- **`observe` exits non-zero when something needs a human**, so it composes in a shell.
- **`task reset` is deliberately a human action.** `failed`/`stuck` tasks may hold
  half-finished work in a worktree; auto-resetting would silently hand it to the next
  dispatcher.
- **`gate-state`/`gate-report` are CLI subcommands, not an HTTP endpoint on the daemon.**
  SQLite in WAL mode already serialises multi-process writes, so a socket would add a
  service that can be down — and "the daemon was unreachable" must never be a route by
  which a gate passes.
- **`events` histograms on exit** (SIGTERM/SIGINT too), otherwise `timeout orch events`
  discards everything it collected.
- A lease that has *expired* is only annotated in `task list`, never treated as
  permission to reclaim: a busy agent whose renewal broke looks identical to a dead one.
- `task list` shows who reached the verdict (`source:outcome`), because a router `passed`
  sitting under a `needs-review` task is the normal, correct combination.

## `daemon.py`

- **Policy is pure; the executor is thin.** `decide()` takes a snapshot and returns
  actions; it touches no process, socket or database. That is the only reason budgets,
  reclaim rules and "quiet vs dead" are testable at all.
- **A session outlives its dispatch process** — the most expensive lesson in the system.
  Measured: a run exited 0 and its session then performed 24 more tool calls over 36
  seconds because the router injected a follow-up prompt. A gone pid says nothing about
  whether work finished; reclaiming on it destroys live work.
- **`finished` is a property of the in-loop gate loop, not of the agent or its session.**
  Going idle is what fires the gate; a failing gate injects a prompt and the agent works
  again. So the router's last word decides what happens next: `passed` → finished,
  `stuck` → finished (cap spent), `failed` → a moment between attempts, nothing recorded
  → never gated and never verified.
- **The daemon runs the gate itself.** It does not accept a verdict from anywhere. The
  router's in-loop run is advisory (hand a failure back while the agent is there to fix
  it); the daemon's out-of-loop run is authoritative and terminal. The cost is running
  the suite twice on a passing task, which is the right thing to pay.
- **A running gate looks exactly like an unexamined agent.** The session is idle for the
  whole duration of a check and the dispatch has usually exited, so a missing verdict
  gets a grace period before it is believed — the router's `running` announcement is the
  mechanism, the grace period only covers the delay before it arrives. Acting on silence
  immediately would condemn every gate slower than one poll interval.
- **Never gated gets a grace period too**, because the router announces itself before
  running but fingerprints the worktree first, which on a large repo takes real time.
- **Do not verify work that was never gated in-loop.** One lucky out-of-loop run must
  not stand in for a gate that never fired; "nothing objected" must not become a pass.
- **A gate that announced itself and never returned** is escalated past its own timeout
  (900s), because beyond that the run is wedged or the server hosting it died.
- **Silence budgets differ by state.** A blocked agent is not "quiet", it is waiting on
  a human, so it gets a shorter fuse than one legitimately grinding. Its lease is still
  renewed — waiting is the opposite of dead.
- **Escalate, never auto-re-dispatch.** The worktree still exists and may hold partial
  work; re-running from scratch and resuming are different operations, and picking
  silently would hide which happened.
- **Reclaim only when definitely dead**: lease lapsed *and* dispatch gone *and* nothing
  listening for the worktree.
- **Actions carry their project**, because one daemon supervises every project and task
  names are unique only within a project — a bare name would eventually renew or reclaim
  the wrong row.
- **Done tasks are merged automatically** — verified work must not accumulate in
  worktrees forever.
- **A long-lived daemon holds the code it imported at startup.** The store records the
  source fingerprint of the running process; `doctor` fails on a mismatch, because a
  version string stays `0.1.0` across every commit.
- **Session busy is asked per-worktree via `GET /session?directory=`**, never per
  process (one shared server has a single cwd) and never from the global session list
  (it returned 33 sessions spanning directories going back days).
- **Alerts dedupe in the store, keyed on the request id** — one turn raises several
  requests, and alerting on "is /permission non-empty" fires repeatedly for what a human
  experiences as one decision.

## `discovery.py`

- **There is no registry file, deliberately.** The kernel already knows which processes
  exist and which sockets they hold, so a file could only go stale. `ss -ltnp` gives
  port and pid; `/proc/<pid>/cwd` gives the directory.
- **An unobservable TUI is reported, never skipped.** `observable=False` exists because
  silence that reads as health is the failure this project exists to prevent.
- **Parse `ss` explicitly.** The process field's opening double quote matters — omitting
  it silently matches nothing and reports an observable TUI as blind. That is the most
  dangerous direction to fail, so it is parsed explicitly rather than cleverly.
- **`pgrep -x opencode` (exact name).** `-f` also matches the shell running the query and
  plugin subprocesses (children of a TUI, not TUIs).

## `dispatch.py`

- **Order is the design.** Claim atomically → run `pre-task-create` from the
  *dispatcher's* worktree → create the worktree → run `post-task-create` from the *new*
  worktree → verify the gate is committed → only then dispatch. Steps 4 and 6 run
  different refs' hooks: a task branch can change its own post-create behaviour, never
  its pre-create behaviour.
- **Prune before `git worktree add`** — a worktree directory deleted by hand stays
  registered, and the add then fails on a path that is not there. Prefer checking out an
  existing branch over inventing a second name for the same work.
- **Register what git declares before claiming.** The store holds runtime state only;
  it is not the list of tasks. An ad-hoc `--prompt` dispatch is still registered, so it
  gets a lease and stays observable.
- **Distinguish the refusal cases.** "Someone else has it" and "it is already finished"
  call for opposite responses; a blurred message sends people looking for a dispatcher
  that does not exist.
- **A precondition failure is not a task failure.** When nothing was created, the claim
  is released — marking the task `failed` made it permanently unclaimable until someone
  edited the database by hand.
- **Deliberately not reclaimed on failure.** The worktree may be half-initialised;
  handing it to the next dispatcher would hide that.
- **No gate is a refusal, not a warning.** Absence of a gate is not a pass (invariant 8);
  a dispatcher that prints a note and proceeds has already broken it.
- **Quote the finding's own detail in refusals.** The causes are genuinely different — a
  missing router vs an uncommitted hook — and a summary that blurs them sends people to
  fix the wrong file.
- **Dispatch is detached** (`start_new_session`): it must survive the shell that started
  it and must not die from a Ctrl-C meant for the CLI.
- **`--format json` is what makes the first leg of a run machine-readable**, but the
  dispatch exit code is never task completion (see daemon).

## `doctor.py`

- **Every check exists because its failure mode is invisible.** An unobservable agent
  looks exactly like an idle one; a project with no gate looks exactly like one whose
  checks passed. Loud verification is the only defence.
- **A missing lifecycle hook may be fine** (a project with no database has no
  post-task-create work); **a missing gate is never fine.**
- **Orchestration must be tracked.** Untracked orchestration is the exact failure that
  motivated this project.
- **A live-but-deaf server is worse than a dead one.** Every task on the machine runs
  inside it; a `kill -0` check calls it healthy while dispatch appears to succeed and
  nothing can be observed afterwards.
- **No daemon is a FAIL, not a note.** Everything appears to work — dispatch, agents,
  in-loop gates — until you ask for a result, because nothing is ever verified.
- **Linger is checked, because it is easy to miss and silently defeats the point.** A
  user unit without linger dies at logout, so an unattended reboot comes back supervising
  nothing while `systemctl status` looked fine when you set it up.
- **Router freshness is separate from drift.** Plugins are imported once per directory
  and cached, so editing the router changes nothing for a directory the server has
  already touched — even a worktree deleted and recreated at the same path. Cost a full
  test cycle here.
- **The zshrc wrapper stays as a courtesy, not a mechanism.** `--help` claims
  `--port [default: 0]`, but omitting the flag starts no server at all — verified on
  1.18.13. The wrapper is what stops new hand-opened TUIs from being born blind.
- **Degraded tooling is reported here, not in the gate.** The shipped gate exits 0 when
  shellcheck is missing (a gate that fails because a linter is absent gets disabled by
  the first person it annoys); the weakened coverage surfaces in `doctor`.

## `gate.py`

- **The gate that runs is the one committed on the base branch, never the working-tree
  copy** — because a dispatched agent was observed hitting the fail-closed scaffold,
  being handed the failure as instructions, rewriting the hook into a gate tailored to
  its own work, and being reported `passed`. The agent authored the criterion it was
  judged by. `config.json` is the same hole by a quieter route: `gate.exclude: ["**"]`
  means no change ever looks worth checking, and a large `retryCap` turns a bounded loop
  into a billable one.
- **Two separate requirements, conflating them hid a bug.** The router must load from
  the worktree's files on disk (what makes a gate able to fire *here*), and it must also
  be committed on the base branch (dispatched worktrees are created from it and inherit
  only what is committed). A worktree created before the plugin was committed satisfies
  the second and fails the first — valid gate, `orch gate` ok, nothing fired.
- **`inspect` only reports.** `doctor` surfaces findings, `task start` refuses on
  blocking ones. That split matters because the failure is silent.
- **`run` exists so the daemon can reach its own verdict.** The router also runs gates,
  but it is a plugin inside the agent's server; a `passed` from there is a self-report.
- **DIVERGED does not disqualify.** The committed copy is what ran, so its verdict is
  sound; it is flagged because an agent editing its own gate is worth a human's
  attention. Conflating the two would train people to ignore the flag.
- **NO_GATE is deliberately not a pass and not an error.** Nothing went wrong, and
  nothing verified the work either.
- **A hanging gate fails, never "unknown".** A gate that never finishes has not approved
  anything; inconclusive-therefore-fine is how a wedged suite starts passing work.
- **The staged gate copy lives outside the worktree**, for two reasons that both bit
  earlier: a copy inside changes the fingerprint the gate is meant to describe, and it
  shows up in `git status` as if the agent had left it there.
- **`baseBranch` is the one setting read from the working tree** — it names the ref
  everything else is read from (chicken and egg). It names a ref rather than granting
  anything.
- **The router contract is a number, not a byte diff.** Projects own their copy, so a
  byte diff cannot separate a deliberate customisation from a router that predates a
  guarantee the supervisor depends on. Contract 2 is the floor because the supervisor
  needs `running` reported before a gate executes. Below the floor: `doctor` fails and
  `task start` refuses.
- **Both plugin directory spellings load** (`.opencode/plugin/` and `.opencode/plugins/`),
  verified by installing the same plugin under each.
- **Only `hooks/` are gates.** Anything else in `hooks/`-adjacent directories is a
  lifecycle script whose authority comes from the dispatcher, not the router.
- **A gate deleted or unreadable locally is itself a divergence.**

## `harness.py`

- **All harness knowledge lives on the Python side.** The TS plugin deliberately knows
  none of the HTTP vocabulary. Read-only helpers are used by the observer (which must
  never write); mutating calls are for the daemon only.
- **Unreachable is a normal condition, not an error** — a TUI can exit between discovery
  and query. Callers get `None` and must degrade, never lie.
- **Two health endpoints are not interchangeable.** `/api/health` returns only
  `{"healthy": true}`; `/global/health` carries the version. Version negotiation uses the
  global one.
- **Use `/permission`, not `/api/permission/request`** — the latter returned empty
  against a session that was demonstrably blocked. Similarly avoid `/api/session/active`.
- **`GET /session` is global, not per-server.** One shared server returned 33 sessions
  spanning every directory ever used, including long-dead ones. The `?directory=` filter
  is the only usable join from a task to its state.
- **Read SSE with `readline()`, not `for line in r`.** Iterating an `HTTPResponse`
  buffers, so an idle stream yields nothing until enough bytes accumulate — for a
  low-traffic event feed that means appearing to receive nothing at all.
- **Two permission generations coexist.** v1 carries `permission`/`patterns`/`always`,
  v2 carries `action`/`resources`/`save`; a reader that assumes one silently sees
  nothing on a server speaking the other.
- **`server.heartbeat` (≈ every 8–10s) separates "stream alive, agent quiet" from
  "stream dead"** — the exact absence problem the watchdog has to solve. Missing
  heartbeats mean the transport failed, not that work finished. It is undocumented in the
  OpenAPI schema (see `docs/events.md`).
- **`step.update`/`progress` give exact tool-call boundaries**, so a long-running tool
  can prove it is alive; `step.ended` carries cost/tokens, the cheap proxy for loop
  detection.
- **Inject via `prompt_async`, never `/tui/append-prompt` + `/tui/submit-prompt`** —
  driving the TUI text box destroys whatever a human was typing, requires a TUI, and
  races with the user.
- **The supported floor is 1.18.11** (the box's pin); newer is a bonus and must not
  become a requirement by accident. Every version seen is recorded in `harness_seen`.

## `init.py`

- **A bare target is refused, not silently written beside.** `orch init` cannot write
  into a bare repo; there is no working tree, and that is exactly how a dispatcher ends
  up living in an untracked parent directory.
- **`--force` must never clobber a gate** — the most valuable file in the project, its
  loss unrecoverable, and overwriting it with the fail-closed scaffold silently replaces
  real verification with none. Learned by doing exactly that here. `--force-hooks` is a
  separate, explicit act.
- **A router below the contract floor is upgraded even without `--force`.** The router
  is orch's mechanism, not project knowledge, and the supervisor depends on a declared
  contract from it; a stale one makes the daemon mark tasks `needs-review` while their
  gates are still running. A router at or above the floor is left alone — projects
  legitimately customise their copy.
- **No `.gitignore` entry for `.orchestration/.state/` is appended any more.** That state
  now lives in the one database; if a project still has the old entry it is harmless.

## `merge.py`

- **The merge lands in the base worktree** (the one checked out on the base branch). If
  it is missing, the merge cannot proceed without creating one — and creating one
  silently is a side effect beyond this tool's scope.
- **Commit any uncommitted work in the task worktree before merging.** The gate verified
  the working tree; `git merge` only moves commits, so uncommitted work would silently
  disappear. Agents are not reliable committers — observed writing correct files and
  passing the gate without a single `git add`.
- **Push the branch ref to the bare repo first.** Worktrees linked to a bare repo share
  the ref namespace, so the push is usually a no-op; if it fails, the merge still works
  when the ref is visible from the base worktree.
- **A conflict aborts the merge, leaving the base worktree un-conflicted.** The task
  stays `done` (or moves to `needs-review`) and the worktree + branch survive for a
  human to resolve and re-run `orch task merge <name>`. A conflict is the normal outcome
  of two concurrent tasks touching the same file — the work is verified, the merge is
  what is blocked.
- **`pre-task-cleanup` runs from the task worktree; `pre-branch-delete` from the base.**
  Either may block to preserve the worktree or branch for review.
- **Branch delete uses `-d`, never `--force`.** If `-d` refuses, the branch is not
  merged — something is wrong, and losing the branch ref would lose the work. Leave it.

## `observe.py`

- **Reports facts and never verdicts.** Judgement — budgets, dedup, notification — is
  the daemon's policy. Keeping judgement out of the observer is what stops the silence
  logic being split across two components, and an observer that never writes can never
  make a bad situation worse, which is what makes it safe to run standalone when the
  daemon itself is broken.
- **States are mutually exclusive and ordered by how much attention they demand.**
- **The unit of observation is a session, not a process** — one shared server has a
  single cwd and many sessions in many directories. A process with no session in the
  observed directory is genuinely different from an idle one (never dispatched, or
  dispatched into a server that has been replaced); collapsing the two would let a task
  that was never started look finished.
- **Subagents are excluded.** A child going idle is not the task finishing, and a child
  being busy is not evidence the parent is progressing.
- **The shared server is relabelled by recorded pid**, not guessed from its cwd — the
  daemon spawned it, so this is knowledge, not inference.
- **Known ambiguity, corroborated elsewhere:** "alive, no port" is also what a *dying*
  TUI looks like, because teardown is not atomic. A `(deleted)` cwd or a missing tmux
  session is a hint the process is a corpse; the daemon must corroborate before acting.

## `server.py`

- **One headless server per machine hosts every worktree.** Safe for project hooks
  because plugin loading is per-directory and lazy. Why headless rather than a TUI per
  task: a server always has a port (observability becomes structural), 362 MB vs 947 MB
  RSS, and `serve` announces its port on stdout. The TUI is demoted to `opencode attach`.
- **Identity is recorded pid plus start time, never a command-line pattern.**
  `pkill -f 'opencode serve'` matches the very shell that invokes it — it killed one of
  these commands mid-flight during development. Start time (`/proc/<pid>/stat` field 22,
  whose comm can contain spaces/parens so it is parsed by position) distinguishes a
  recycled pid, so a stale pid file can never kill a stranger's process.
- **`current()` distinguishes three degrees of wrongness**: no record, a record whose
  process is gone (cleanup), and a live process that no longer answers (the only one
  worth alarming about — a half-dead shared server silently breaks every task).
- **The server starts in a neutral directory that is not a project.** Its own cwd
  becomes its default project; starting it inside a real repo would make that repo's
  config and plugins the ambient default for anything dispatched without an explicit
  `--dir`.
- **`orch_bin` prefers the checkout this module was imported from** over whatever
  `orch` PATH resolves to, so a development copy's state is never written by a stale
  global install standing on a different schema.
- **The port is read from the line `serve` prints on stdout**, not guessed or scanned
  for; `--port 0` is passed explicitly so both launches share one idiom.
- **Stop is SIGTERM then SIGKILL, by recorded pid, no tmux teardown.** Killing the
  process ends its tmux session by itself, verified directly; tmux is a host for a
  process, never a handle on its lifetime.
- **Failure to record the harness version never blocks a start.**
- **Plugins are imported once per directory and cached**; a live process that does not
  answer is the interesting failure, and the supervisor must not read it as "up" just
  because the pid exists.

## `store.py`

- **Why SQLite rather than a lock file**: a claim is one atomic statement under
  `BEGIN IMMEDIATE` (no external lock), leases expire so a crashed agent's task becomes
  reclaimable (git can express state but not a lease — that, not the race, is the real
  gap), and you must never `flock` a tracked file (git replaces inodes, so the lock stops
  referring to the file everyone else sees). One database per machine, mirroring
  opencode's own global store.
- **`sync_intent` mirrors git, never reads back.** The checkbox is a rendered view.
- **`record_launch` runs even when the daemon is unreachable**, so a daemon that starts
  later can still reconcile children it never saw.
- **Expiry alone is never sufficient to reclaim.** The caller must also confirm the port
  is unreachable and the tmux session is gone — only all three together mean definitely
  dead. A busy agent whose renewal broke is not the same thing.
- **The gate baseline fingerprint is persisted deliberately.** Held in plugin memory, a
  TUI restart re-baselines and can skip the first check entirely.
- **Verdicts append, never overwrite** — the previous answer is evidence.
- **`note_harness` records that the version changed between runs**, not the current
  value — the only warning that behaviour may have moved under us.
- **Alerts dedupe by request id**, because one turn can raise several requests and
  alerting on "is /permission non-empty" fires repeatedly for one human decision.
- **`by_worktree` is the only join a plugin can make.** A plugin knows the directory it
  was loaded for and nothing else — not the task name, not the project.

## Router plugin (`plugin/orch-router.ts`)

- **Named after mechanism, not policy.** An event arrives; if
  `.orchestration/hooks/<event-name>` exists it runs; the outcome is reported. What
  "verify" means lives in the hook, never here — adding a new event should cost
  configuration, not code.
- **`ROUTER_CONTRACT` is a number and is deliberately NOT exported.** opencode requires
  every export of a plugin file to be a function and rejects the whole file otherwise
  (`Plugin export is not a function`); exporting the number silently disabled the entire
  router. `orch` reads it by pattern.
- **Config is read from the committed base branch, never the working tree** — it is as
  security-critical as the gate (`exclude: ["**"]` switches the gate off without touching
  a hook). `baseBranch` is the one exception that must come from the working tree,
  because it names the ref everything else is read from.
- **The gate is the committed one, staged in a temp dir outside the worktree** so it is
  neither fingerprinted nor visible to `git status`. An uncommitted gate is not a gate.
- **Fingerprint is content, not mtime** (checkout/touch/stash rewrite timestamps without
  changing code), and asks git for tracked+modified files rather than walking the tree —
  orchestration must never hash `node_modules`.
- **State is reached through `orch`'s CLI, not an HTTP call** — SQLite WAL already
  serialises writers, and "the daemon was unreachable" must never let a gate pass.
- **The skip decision fails closed.** Anything short of a known baseline that matches
  exactly — state unavailable, no baseline, empty string — runs the gate. An unreachable
  CLI returns `null`, which makes the gate run, never skip.
- **An in-memory retry counter backs the store's**, because if `orch` cannot be reached
  the cap would be unenforceable and a failing gate would retry forever; attempts are
  `max(local, reported)` so a reachable CLI can never reset the cap from below.
- **The plugin decides nothing about who is told.** It produces a result; routing is the
  daemon's job, which is why there is no `report_back` message.
- **`no-gate-configured` is reported, never `passed`** — reporting a pass would
  manufacture confidence in work nothing checked.
- **`gate-modified` is surfaced, not vetoing** — the committed gate ran, so its verdict
  stands, but an agent editing its own gate is exactly what a human should look at.
- **Announce `running` before executing.** From outside, a running gate is
  indistinguishable from an agent that stopped without anything ever checking it.
- **Only primary sessions are gated** — a subagent going idle is not a task finishing,
  and running a full suite per subagent would thrash the machine.
- **Inject failures via the session API**, never `tui.appendPrompt` + `submitPrompt`.

## Verification of this removal

Every claim above was present in the source as comments or docstrings at the time this
file was written, alongside the code it describes. If a decision is missing here, check
`git log` for the original wording; the commit that deleted the comments is the record.
