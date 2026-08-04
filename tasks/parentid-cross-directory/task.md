# Task: Test Whether `parentID` Works Across Differing `directory`

## Metadata

- **Complexity:** Low
- **Priority:** Low
- **Status:** DONE — result recorded below
- **Dependencies:** None

## Context

Every endpoint accepts `?directory=` and `POST /session` accepts `parentID`, but
nobody has checked whether a parent/child pair is accepted when the two are in
different worktrees.

The likely answer is already visible in the data. `GET /project/current` reports
the **bare repo** as `worktree` and lists worktrees as `sandboxes`, and worktrees
of one bare repo share a session list — verified, `reeeally-main` and
`reeeally-preview` return identical session IDs while `cloud-agent-infra` differs.
So parentage probably works across worktrees of one bare repo and cannot work
across projects.

This used to be load-bearing. It no longer is: the architecture owns the
parent↔child mapping in its own DB precisely because the harness's parentage stops
at the project boundary. Run the test to know, not to unblock.

## Requirements

- [ ] Create a session in one worktree, then a child with `parentID` set and
      `directory` pointing at a different worktree of the same bare repo.
- [ ] Confirm via `GET /session/{parent}/children` whether the relationship holds.
- [ ] Repeat across two different bare repos and record the failure mode.
- [ ] Write the result into `docs/architecture.md` under verified vs assumed.

## Result (measured)

Run in an isolated lab: one bare repo, two worktrees, a single server on an
ephemeral port.

| Child created with | Outcome |
| --- | --- |
| `?directory=` the other worktree, same bare repo | parentage **accepted**, same `projectID` |
| `?directory=` a worktree of a **different** bare repo | parentage **accepted**, *different* `projectID` |

Both children carried `parentID` in their own record, and
`GET /session/{parent}/children` returned each one with its differing `directory`.

**The prediction in the Context above was wrong.** Cross-project parentage was
expected to fail and does not. Two further facts fell out: a single server can
create a session in any directory on the machine, and `projectID` is derived from
the target directory rather than from the server's own location.

## Verification

The recorded result either way. A negative is as useful as a positive here.

## Prohibited Patterns

- Do **not** build cross-worktree coordination on the harness's parentage even if
  the test passes. Treat it as cosmetic; the DB is the truth.
