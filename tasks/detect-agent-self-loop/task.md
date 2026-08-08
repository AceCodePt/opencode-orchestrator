---
hitl: true
---

# Task: Detect an Agent That Has Looped Itself

## Metadata

- **Complexity:** High
- **Priority:** Low — noted deliberately, not being solved yet
- **Status:** Not Ready — needs design
- **Dependencies:** `silence-watchdog`

## Context

An agent can loop by accident: retrying a failing command, re-editing the same
file, or re-reading the same context indefinitely. It burns tokens and money and
produces nothing.

The silence watchdog will never catch this. A looping agent is **loud** — it
reports `busy`, emits events continuously, and looks perfectly healthy by every
liveness signal. Silence detection and loop detection are opposite problems and
need different instruments.

This is filed as a known gap rather than an active task. It is written down so it
is not rediscovered later as a surprise.

## Requirements (sketch, not a spec)

- [ ] Decide what a loop looks like observably. Candidates: the same tool called
      with identical arguments N times, the same file edited repeatedly with no
      test-state change, a cost rate that keeps climbing while the todo list does
      not move.
- [ ] Evaluate the cheap proxy first: `session.cost` and the token columns exist
      in the store, so a cost-per-unit-progress alarm is available without any
      new plumbing.
- [ ] Establish a baseline for normal work before choosing thresholds. Without
      one this will be tuned to a single anecdote.
- [ ] Decide the response. Alerting is safe. Auto-aborting via
      `POST /session/{id}/abort` risks killing legitimate long work, and that
      tradeoff should be a deliberate decision, not a default.

## Verification

Not defined yet. A meaningful test needs a reproducible loop to detect, which
does not exist yet — capturing a real one when it next happens is the honest
first step.

## Prohibited Patterns

- Do **not** fold this into the silence watchdog. Opposite signals, opposite
  logic.
- Do **not** ship auto-abort on a threshold guessed from one incident.
