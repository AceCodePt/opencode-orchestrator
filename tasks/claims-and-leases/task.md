# Task: Claims, Leases and the Intent/Runtime Split in SQLite

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** None

## Context

`TASKS.md` currently tries to be three things: human-editable, git-tracked, and
the lock. Under concurrency you can keep two. The system exists to run agents in
parallel, so the lock has to move.

Worse than the race: `- [-]` has no expiry. A dispatch that dies after flipping
the checkbox leaves a task that nothing can distinguish from one still running.
Git can express state but not a lease, and that is the actual gap.

## Requirements

- [ ] Schema for `task` (name, status, seq, `lease_until`, worktree, port, pid,
      tmux session, parent session, retry counter) plus whatever the observer needs
      for alert state.
- [ ] One global DB per machine, mirroring how opencode keeps a single
      `~/.local/share/opencode/opencode.db` rather than per-project stores.
- [ ] Atomic claim via `BEGIN IMMEDIATE` and a single `UPDATE … RETURNING`. No
      file lock.
- [ ] Lease renewal while an agent is alive; expiry makes a task reclaimable.
- [ ] `TASKS.md` becomes a rendered view: `[-]` written back best-effort, **never
      read as authority**.
- [ ] Reclaim path: expired lease *and* unreachable port *and* dead tmux session →
      return the task to available.

## Verification

Dispatch two tasks concurrently and confirm each is claimed exactly once. Then
`SIGKILL` a dispatch immediately after it claims, and confirm the task becomes
reclaimable after the lease expires rather than being stuck.

## Prohibited Patterns

- Do **not** `flock` a tracked file. Git replaces inodes, so the lock silently
  stops referring to the file others see.
- Do **not** rely on git for mutual exclusion. `index.lock` fails rather than
  queueing, so concurrent commits produce errors, not serialization.
- Do **not** read task state from two places. One authority per question.
