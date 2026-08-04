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
