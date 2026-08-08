# Task: Distinguish an Agent That Finished From One That Merely Stopped

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Done
- **Dependencies:** none

## Context

Three states were collapsed into two, and the daemon acted on the wrong one: it ran the
gate for **any** agent that had gone quiet.

The first draft of this task assumed the missing piece was a *claim by the agent* —
something asking it whether it had completed the work. That was wrong, and would have
added a new untrustworthy input for no gain.

`finished` is a property of the **in-loop gate loop**, not of the agent's opinion, and it
was already observable in data the store held. Going idle is what *fires* the gate; a
failing gate injects a prompt and the session becomes busy again. So an agent only stays
stopped once the loop can no longer move it — the gate passed, or the retry cap was
spent. The router's last outcome says which.

## Requirements

- [x] `stopped` and `finished` are distinct, and the daemon acts only on `finished`.
- [x] Classify: settled on a pass, retry cap spent, still retrying, never gated.
- [x] A task with no in-loop verdict is never verified and never reaches `done`.
- [x] An unrecognised router outcome falls to the cautious side, not to `finished`.
- [x] A retry loop that has stopped driving the agent is escalated, not renewed forever.
- [x] `orch task list` shows the last verdict and who reached it.

## Outcome

`daemon.stop_reason()` maps the router's last outcome onto four states, and `decide()`
branches on them:

| Router's last word | State | Action |
| --- | --- | --- |
| `passed` | finished, loop settled on a pass | verify out-of-loop; `passed` → `done` |
| `stuck` | finished, cap spent | verify, but never `done` |
| `failed` / `gate-modified` | still retrying | renew; escalate past the idle budget |
| nothing, or unrecognised | never gated | `needs-review`, do not verify |

Two rules came out of this that were not in the original requirements:

**No in-loop verdict means no verification.** If nothing gated the work while the agent
was present, one lucky out-of-loop run must not substitute for the gate that never ran.
This closes the silent case found earlier — a worktree created before the router was
committed, where the gate was valid and simply never fired.

**A pass after the cap was spent is not `done`.** A suite that failed to the cap and then
passes untouched has raised a flakiness question rather than earned confidence. The gate
still runs and the verdict is still recorded.

## Verification

Performed live: a task whose work was genuinely correct, and whose gate passes on demand
(`orch gate-run` → `passed`), still landed on `needs-review` once its in-loop verdicts
were removed. The honest path was re-confirmed end to end under systemd, unattended:
`router:passed` → daemon verified → `done` in about twenty seconds.

Eleven unit tests cover the classifier, each branch, the escalation budget, and the
flakiness rule.

## Not solved by this

**Completeness.** A gate answers "is this broken", not "is this the task", so an agent
that did a fifth of the work and left a green suite behind still passes. That is a matter
of what a project's gate checks — project knowledge, not infrastructure — and it is why
`done` means "checked and passed" rather than "the task was accomplished".
