---
hitl: true
---

# Task: Phone-Facing Server — Read Status and Answer Approvals Over the Tailnet

## Metadata

- **Complexity:** High
- **Priority:** Medium
- **Status:** Ready for Handoff
- **Dependencies:** `permission-burst-keying`

## Context

This is the daemon's actual purpose: a server your phone talks to. Not just to
watch, but to **act** — an unattended agent blocked on an approval is stalled until
someone answers, and answering from your phone is the difference between a run that
finishes and one that waits all night.

The payload makes this cheap. A pending request carries its `id`, `sessionID`,
`permission` type, `patterns`, and an `always` field naming exactly the grant that
would stop the prompt recurring.

## Requirements

- [ ] Read endpoints: every known TUI, its worktree, state, and what it is blocked
      on.
- [ ] Action endpoints: approve, approve-always, deny, abort a session.
- [ ] Authority lives here, not in the observer. The observer stays read-only so it
      can never worsen a bad situation.
- [ ] Reachable over the tailnet only. Not the public internet.
- [ ] Rebuild all state on restart from SQLite plus `ss -ltnp`, holding nothing
      important in memory.
- [ ] Show the requested pattern and the worktree in anything a human sees. "An
      agent wants permission" is not actionable.

## Verification

From a phone on the tailnet: list agents, see a real pending approval, approve it,
and watch the agent proceed. Restart the daemon while a child is running and
confirm it re-attaches without losing the child. Approve-always and confirm the
same request stops recurring.

## Prohibited Patterns

- Do **not** give the observer write access to make this easier.
- Do **not** expose it publicly; the tailnet already solves reachability.
- Do **not** hold session state only in memory — a restart must not orphan running
  children.
