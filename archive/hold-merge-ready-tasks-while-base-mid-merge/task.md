---
wait_human_start: false
wait_human_merge: false
dependencies: []
---

# Task: Hold a Clean Merge-Ready Task While the Base Is Mid-Merge

## Metadata

- **Complexity:** Low
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

Split out of the conflict-resolution fix (6ecda25) during a code review: the working tree carried an uncommitted daemon change that held a clean, diffing task while the base branch was mid-merge (an unresolved conflict from another task). Without the hold, such a task gets verified and merged into a base that is still in a conflicted merge — the branch was built from the pre-conflict base, so the collision is only resolvable once the base is healthy. The intended behavior: hold (no verify, no merge, no failure backoff), notify once, resume settling once the base resolves.

## Requirements

- [ ] In daemon.decide, after the conflicted-task branch and before the wait_until backoff check, add: if state._base_merging(Path(t.project), t.base) and t.status in (state.MERGE_READY, state.NOOP): hold the task - emit a one-time NOTE 'held: base branch is mid-merge' (tracked in a new LoopState set, e.g. merge_held_notified, keyed by (project, name)), reset the loop, and continue. No VERIFY, no MERGE, no failure backoff while held.
- [ ] The NOTE is emitted only once per (project, name) until the hold clears; subsequent sweeps while still held emit nothing and never push to verify or merge.
- [ ] Once the base merge resolves (state._base_merging returns None), the same clean merge-ready task resumes its normal settle-and-verify path.
- [ ] The behavior must not apply to the task whose own merge is the conflict (that one is already handled by the conflicted branch above).

## Verification

Run the daemon test suite (python3 tests/test_daemon.py). A test builds a conflicted repo, adds a second clean merge-ready task (task-b), and asserts: while the base is mid-merge the task emits only a one-time NOTE (no VERIFY/MERGE), is not re-notified on the next sweep, and resumes to VERIFY after the base conflict is resolved.

## Prohibited Patterns

- Do not reapply the hold to the conflicted task itself - its branch already runs the conflicted resolution path.
- Do not add a failure backoff (wait_until) to a held task: a clean, already-merged-style task must stay eligible to settle the moment the base is healthy.
