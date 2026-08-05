# Survive Death of the Shared Server by Re-Dispatching From Leases

## Context

One shared `opencode serve` per machine is the chosen topology, and its one real
cost is a single failure domain: if that process dies, every running task dies with
it. The cheapness is worth it — 362 MB once instead of 947 MB per task — but only if
death is recoverable rather than silent.

This is not hypothetical. During the very session that chose this topology, the
shared server was killed twice: once deliberately, and once by an errant
`pkill -f 'opencode serve'` that also killed its own invoking shell. A design that
loses every in-flight task when one process dies needs an answer, not a caveat.

Leases already live in SQLite, so the information needed to recover exists. Nothing
consumes it yet.

## Requirements

- The daemon supervises the shared server: it spawns it, records its pid, and
  detects death by pid rather than by scanning.
- On death, restart it and reconcile: for every lease whose session belonged to the
  dead server, decide re-dispatch or escalate. A task that was mid-gate is not the
  same as a task that had barely started.
- Distinguish server death from task completion. Both end a session; only one is
  work finished, and treating them alike either loses work or double-runs it.
- Preserve the "definitely dead" discipline: reclaim automatically only when the
  lease is expired *and* the pid is gone. Restarting a server must not silently
  reclaim work an operator was still looking at.
- Re-dispatch must be idempotent with respect to the worktree, which still exists
  and may hold partial work. Re-running a task from scratch over a dirty worktree is
  a different operation from resuming it, and the difference should be explicit.

## Verification

Kill the shared server by recorded pid while a task is mid-flight, with a gate
failure already injected. The daemon should restart it, reconcile the lease, and
either resume or escalate — with the resulting state visible in `orch observe`
rather than inferred.

Second case: kill it while a task is *between* gate attempts, since the retry
counter lives in `.orchestration/.state/` and the lease lives in SQLite. Those two
authorities disagreeing is the deviation `plugin-state-via-daemon` exists to remove,
and this is the scenario that exposes it.

## Prohibited Patterns

- Pattern-based process killing anywhere in the supervision path.
- Treating "session gone" as "task done".
- Recovering by scanning for whatever server happens to be listening. The daemon
  spawned it; if it has lost track of its own child, discovery hides the bug.
- Automatically re-dispatching without recording that a re-dispatch happened. A
  task that silently ran twice is indistinguishable from one that ran once, and the
  second run may have started from a dirty worktree.
