# Task: Distinguish an Agent That Finished From One That Merely Stopped

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** none

## Context

Three states are currently collapsed into two, and the missing one is the one that
matters.

| Term | Means | Established by |
| --- | --- | --- |
| stopped | the session is idle and its dispatch exited | observation |
| finished | the agent considers the task complete | a claim by the agent — **not captured** |
| done | the work was checked and passed | a gate the daemon ran |

Because `finished` is never captured, the daemon treats `stopped` as if it were
`finished` and runs the gate on any agent that has gone quiet. An agent stops for many
reasons: it completed the work, it gave up, it hit a wall, it asked a question nobody
answered, it ran out of context, it crashed.

The consequence is specific. A gate answers *"is this broken"*, not *"is this the
task"*. An agent that did a fifth of the work and quit leaves a green suite behind, so
the gate passes and the task is marked `done`. Completeness is never checked by
anything.

This is not the same problem as the gate being trustworthy. That one is solved: the
gate is read from the base branch and only the daemon's own run can promote a task.
This is about what the passing gate *means*.

## Requirements

- [ ] A stopped agent's task does not reach `done` on gate evidence alone.
- [ ] Some record of whether the agent believes it completed the task. It is a claim,
      so it must be stored as a claim — attributed, like router verdicts are, and
      never sufficient on its own.
- [ ] Distinguish at minimum: completed, gave up, blocked and unanswered, died.
- [ ] `orch observe` and `orch task list` show which of those happened.

## Verification

Dispatch a task the agent cannot complete, and confirm the outcome is not `done` even
when the gate passes. Then dispatch one it can, and confirm the two are distinguishable
without reading a log.

## Prohibited Patterns

- Do **not** trust the claim on its own. An agent asserting completion is the same
  class of evidence as an agent asserting a gate passed.
- Do **not** infer completion from the absence of activity. That is the current bug.
