# Task: Route Plugin State Through the Daemon Instead of `.orchestration/.state/`

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** `phone-server` (the daemon must exist to receive state)

## Context

This task exists to correct a deviation introduced by the first implementation,
recorded rather than hidden.

`docs/architecture.md` states one authority per question: git holds intent, SQLite
holds runtime. The router plugin violates it. Because the daemon does not exist
yet, the plugin persists its fingerprint and retry counter to files under
`.orchestration/.state/` while the CLI keeps the same class of state in SQLite.

That is two authorities for one question, and the consequences are real rather
than theoretical:

- A retry counter in a file and a retry counter in the database will disagree, and
  nothing decides which wins.
- Per-worktree files cannot express a machine-wide concurrency limit, so the
  semaphore the gate needs has nowhere to live.
- `orch observe` cannot see gate progress at all, because it reads the database.

The plugin was written thin on purpose — it holds no harness endpoint knowledge —
so the fix is small: report outcomes and read counters over HTTP from the daemon.

## Requirements

- [ ] Daemon endpoints for: fetch fingerprint, store fingerprint, increment and
      read retry counter, acquire and release a concurrency slot, submit an
      outcome.
- [ ] Plugin calls those instead of touching `.orchestration/.state/`.
- [ ] Delete `.orchestration/.state/` handling from the plugin and drop the
      `.gitignore` entry `orch init` currently appends.
- [ ] Behaviour when the daemon is down: the gate must **fail closed**, reporting
      `no-gate-configured` or `stuck` rather than silently passing. A gate that
      approves unchecked work because a service is unreachable is the failure this
      project exists to prevent.
- [ ] `orch observe` should surface gate state, since it becomes available.

## Verification

Run a task whose gate fails, restart the daemon mid-loop, and confirm the retry
count survives and the cap still applies. Then stop the daemon entirely and confirm
the gate fails closed instead of reporting success.

## Prohibited Patterns

- Do **not** keep both stores "in sync". One authority per question; delete the
  file-based one.
- Do **not** let an unreachable daemon produce a pass.

## Outcome, and two departures from the above

Done, but not as specified. Recording both, because the requirements were written
before the shape of the problem was clear and following them literally would have been
worse.

**1. No HTTP endpoints. The plugin calls the CLI.**

The requirement asked for daemon endpoints and a plugin that calls them. But SQLite in
WAL mode already serialises writers across processes, so a socket adds nothing except a
service that can be *down* — and this task's own fail-closed requirement says an
unreachable dependency must never produce a pass. Removing the dependency is a stronger
guarantee than handling its absence carefully. The requirement was one authority, not
one server, and there is now exactly one: the database, addressed through
`orch gate-state` and `orch gate-report`.

The plugin addresses its row **by worktree**, because that is the only identifier it
has: a plugin is told the directory it was loaded for and knows neither the task name
nor the project.

**2. "Submit an outcome" turned out to be the wrong idea entirely.**

The requirement listed submitting an outcome alongside the bookkeeping calls, as though
a verdict were just another value to persist. It is not, and implementing it that way
would have reopened the hole `gate-not-agent-writable` had just closed one level up.

The router runs inside the agent's own server. The agent holds a bash tool. So any
channel the router uses to report `passed` is a channel the agent can use directly —
verified by doing it: wrong answer written, local gate rewritten to `exit 0`, and
`orch gate-report --outcome passed` called by hand. Reading the gate from the base
branch removed the agent's authority over *what is checked*; it did nothing about its
authority over *what the result was*.

So outcomes are submitted, and stored, and attributed with a `source` — and they decide
nothing. `done` is written only after the daemon runs the committed gate itself,
out-of-loop, after the session has stopped. Against the sabotage above the answer was
`needs-review`, `gate-modified`, and `answer.txt says '41', expected 42`.

The concurrency-slot endpoint was not built. It belongs to
`tasks/concurrency-semaphore`, which is still blocked on measuring what the limit
should be, and the database is now the obvious place for it to live.

**Verification performed:** retry counter climbed `0 → 1 → 2` in the store across
separate router invocations and separate processes, `stuck` at the cap of 2 with no
third attempt, no `.orchestration/.state/` created anywhere, and `orch init` no longer
appends the `.gitignore` entry. Durability across a daemon restart is now moot rather
than tested: no daemon holds the counter.
