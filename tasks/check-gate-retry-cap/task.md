# Task: Bounded Retries on the Check Gate, Then Halt

## Metadata

- **Complexity:** Low
- **Priority:** Medium
- **Status:** Ready for Handoff
- **Dependencies:** `event-hook-router`

## Context

The gate creates a loop on purpose: checks fail → the child is told → it edits →
goes idle → checks run again. `auto-check.ts` has no attempt counter anywhere, so
an agent that cannot fix a type error retries forever and bills you for it.

This is the self-inflicted version of the loop problem, and unlike a genuine
runaway it is bounded and knowable, so it should simply be capped.

## Requirements

- [ ] Retry counter per task, persisted in SQLite alongside the lease.
- [ ] Configurable cap, per project.
- [ ] On exceeding the cap: stop re-prompting, halt the task, emit `stuck`.
- [ ] Reset the counter when the checks pass.
- [ ] Include the last failure output in the `stuck` outcome — whoever picks it up
      needs to know what it could not fix.

## Verification

Point a project's `session-idle` hook at a check that cannot pass. Confirm the
child is prompted exactly `cap` times, then halts, and that `stuck` is routed per
config rather than looping silently.

## Prohibited Patterns

- Do **not** keep the counter in plugin memory. A TUI restart would reset it and
  the loop becomes unbounded again.
- Do **not** conflate this with runaway-loop detection. This bound is known; that
  one is not.
