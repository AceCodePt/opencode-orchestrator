---
hitl: true
---

# Task: Measure Concurrent Check Runs and Decide the Semaphore Limit

## Metadata

- **Complexity:** Low
- **Priority:** Medium
- **Status:** Ready for Handoff — measurement first, design after
- **Dependencies:** `event-hook-router`

## Context

Neither of us knows whether N worktrees running checks at once thrashes, and it is
cheaper to measure than to argue. `auto-check.ts`'s `isRunning` guard only protects
one plugin instance; nothing coordinates across worktrees.

Starting numbers from the box: `e2-standard-2`, 2 vCPU / 8 GB, roughly 1000 MB per
working agent, CPU-bound at about two concurrent agents. So the interesting
question is what happens at N=2 and N=4 with real check runs layered on top.

Only the global orchestrator knows about every worktree, so if a limit is needed,
the semaphore belongs there.

## Requirements

- [ ] Measure wall-clock time of a single check run alone, then with 2 and 4
      running concurrently, on the box and locally.
- [ ] Record memory and load, and whether anything gets OOM-killed.
- [ ] Decide from the data whether a semaphore is needed, and at what limit.
- [ ] If needed: a daemon-held slot the router acquires before invoking a hook,
      with a queue rather than a failure.
- [ ] Write the numbers down. A limit chosen without recorded measurements will be
      re-litigated later.

## Verification

The measurements themselves, plus — if a limit lands — proof that N+1 concurrent
idle events queue instead of running, and that a crashed holder releases its slot.

## Prohibited Patterns

- Do **not** pick a limit by intuition. If vertical scaling turns out to be the
  cheaper answer, the data should say so.
- Do **not** fail a hook because a slot is busy. Queue it; the work is still valid.
