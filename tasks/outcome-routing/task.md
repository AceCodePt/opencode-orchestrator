# Task: Outcome Routing With a Per-Event Human-in-the-Loop Toggle

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** `claims-and-leases`

## Context

There is no `report_back` message in this design. A child produces a **result**;
the orchestrator decides who needs to know. That is what makes "is a human in the
loop?" a configuration question instead of an architectural one — and it means the
human's role can shrink over time by editing a table.

It also fixes the reliability problem. A child that crashed, looped, or simply did
not follow an instruction never reports, and reporting is exactly what fails when
things go wrong. Outcomes that the orchestrator derives from observation do not
depend on the child's cooperation.

## Requirements

- [ ] Outcome types: `passed`, `failed`, `stuck`, `stalled`, `blocked-on-approval`.
- [ ] Routing table in config, per outcome, with destinations: parent agent, human,
      nobody.
- [ ] Deliver to a parent agent with `prompt_async`. The orchestrator is the sole
      notifier of parents, so a parent learns about children through one channel
      rather than two of differing reliability.
- [ ] Unblock dependents: `passed` should make tasks marked
      `🔒 Blocked by: [...]` eligible. This is the only purpose of parent
      notification that survives scrutiny.
- [ ] Defaults: approvals → human; stalled/stuck → both; passed → parent, human
      optional; merge → always human.
- [ ] Parents must not need a timeout. The orchestrator tells them when a child
      stalled, because it is the component holding the observer.

## Verification

Configure `passed → nobody` and confirm a completed child notifies no one while
its dependents still unblock. Flip to `passed → parent` and confirm the parent
session receives a message it did not poll for. Kill a child mid-task and confirm
the parent is told without having held a timer.

## Prohibited Patterns

- Do **not** let the child know whether a human is involved.
- Do **not** reintroduce an agent-called `report_back` as the state channel. An
  agent's summary text is welcome; an agent's claim of success is not state.
