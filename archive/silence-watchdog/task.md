# Task: Watchdog for a TUI That Has Gone Silent Too Long

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** `event-stream-listener`

## Context

The event stream catches transitions. It cannot catch **absence**, because "no
event" is not an event. A TUI that has heard nothing for an unreasonable amount
of time is a fact worth knowing, and the only way to know it is to poll and
compare against a clock.

This is why the design keeps both mechanisms rather than picking one. SSE answers
"what just changed", polling answers "what has stopped changing".

The naive version of this is wrong. A single global timeout either fires during
every legitimate long tool call — a test suite, a Playwright run, an install — or
is set high enough that real stalls go unnoticed for a long time. The budget has
to depend on what the agent is supposedly doing.

## Requirements

- [ ] Poll `/session/status` per TUI on an interval, alongside the SSE listener.
- [ ] Track the timestamp of the last event seen per session, sourced from the
      event stream.
- [ ] Implement **per-state budgets**, not one global timer. At minimum
      distinguish:
      - `busy` with an active long-running tool call — generous budget, this is
        healthy silence
      - `busy` with no output at all — tighter budget, this is suspicious
      - blocked on `/permission` or `/question` — alert almost immediately; this
        state never self-resolves without a human
      - `idle` with an incomplete task — should not sit unnoticed
- [ ] Make the budgets configurable, and record the reasoning for the defaults
      chosen.
- [ ] Emit a distinguishable alert per state, so "stalled" and "waiting for you"
      are not the same notification.

## Verification

Three cases must be demonstrated against a live TUI:

1. A genuinely long tool call does **not** trigger an alert.
2. A TUI blocked on an approval alerts promptly.
3. A TUI killed mid-task (`SIGSTOP` it, or block its provider) is reported as
   stalled rather than silently forgotten.

## Prohibited Patterns

- Do **not** use one global timeout for all states.
- Do **not** infer liveness from the `session.cost` or token columns alone; those
  update on completion, not continuously.
- Do **not** treat this as a substitute for loop detection. A looping agent is
  loud, not silent, and this watchdog will never see it. That is a separate task.
