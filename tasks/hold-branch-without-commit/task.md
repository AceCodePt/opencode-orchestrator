---
wait_human_start: false
wait_human_merge: false
dependencies: []
---

# Task: Hold (do not harvest) a task whose branch committed nothing since its claim

## Metadata

- **Complexity:** Medium
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

orch conflates 'the agent is not busy' with 'the agent is done'. When a dispatched agent stops on its first turn without committing - a provider error, a quota stop, a crash, an unused/halted session - the task branch tip is still base's tip, so `state._diff` is false and `daemon.decide` routes the task to REMOVE: it removes the worktree, force-deletes the branch, and archives the spec. The work and the evidence are destroyed, and a legitimate empty run is indistinguishable from a dead one. Concrete evidence: shast/html-conditional-attributes (session ses_f28df150cffeQ72SNqRJuQvH5m) died on turn 1 with provider.quota 429 and was archived 33s after dispatch, as was interactably/html-language-server (ses_f28dedde0ffem4lRl0CdOVRhPS) at the same time. The branch tip of such a run is contained in main (git rev-list --count main..branch == 0), which is exactly the fact the harness already computes as `t.diff`. The fix is to change the disposition of that fact, not to add new detection.

## Requirements

- [ ] In `daemon.decide`, the settle no-op close at the `if not t.dirty and not t.diff:` branch (src/orch/daemon.py, currently appending Action(REMOVE, ..., "no-op: clean branch identical to base")) must instead append Action(NEEDS_REVIEW, ...) and must NOT call merge.remove_task or otherwise touch the worktree/branch.
- [ ] The task must remain claimed: its worktree and branch survive untouched, and the spec stays in tasks/ (not moved to archive/).
- [ ] The needs-review action must be emitted at most once per REVIEW_BACKOFF_S window for the same (project, task), matching the other needs-review emissions in the same function (worktree-gone, merge-conflict, stalled): gate on `snap.now >= mem.wait_until.get(key, 0)`, set `mem.wait_until[key] = snap.now + REVIEW_BACKOFF_S`, and use `_reset_loop(mem, key, keep=("wait_until",))` before `continue`.
- [ ] There must be no escape hatch: no auto-recovery, no retry back to todo, no deliberate-empty archive, and no auto-archive path for this case. The task stays needs-review until a human acts on it.
- [ ] The change applies ONLY to the first path (`daemon.decide`). The post-verify `if not state._diff(...)` branch in `daemon._verify` (src/orch/daemon.py, which calls merge.remove_task) must be left exactly as is.
- [ ] `daemon._verify` must remain the only remaining caller that archives a diff-less branch, so the existing REMOVE behaviour is preserved there.

## Verification

Write focused tests for `decide` (pure function over a Snapshot) covering: (1) a claimed task with dirty=False and diff=False produces a NEEDS_REVIEW action and no REMOVE action; (2) the same task does not re-emit before REVIEW_BACKOFF_S elapses and does re-emit after; (3) a claimed task with diff=True still proceeds to the verify/merge path (no NEEDS_REVIEW); (4) a claimed task with dirty=True is unchanged (its existing needs-review/held path is untouched). Then run the repo's test suite (pytest) and `orch task list` on a scratch repo after dispatching an agent that is killed before committing, confirming the task shows as needs-review with its branch and worktree intact rather than archived.

## Prohibited Patterns

- Do not read the opencode session, its messages, or its error/outcome fields from the daemon to decide this case - the whole point is a git-branch-only rule.
- Do not match commit messages, task names, or slugs to classify the run - names and messages change.
- Do not use a lookback window (e.g. 'last 100 commits') or a stored claim SHA/ref marker; `state._diff` / `git rev-list --count base..branch` is the required test.
- Do not modify `daemon._verify`'s diff-less archive path.
- Do not add an escape hatch, retry-to-todo, or auto-archive for the held case.
- Do not gate on the session state (BUSY/BLOCKED/INTERRUPTED/IDLE) - idle means 'not busy', not 'done', and that conflation is the bug.
