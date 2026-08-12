---
wait_human_start: true
wait_human_merge: false
---

# Task: Key Approval Notifications on Request ID and Survive Bursts

## Metadata

- **Complexity:** Low
- **Priority:** Medium
- **Status:** Ready for Handoff
- **Dependencies:** `event-stream-listener`

## Context

An agent can block **several times in one turn**, with a new request id each
time. Observed on a live TUI: `per_fcd9baecb001adoyjREdwD6WXe`, then
`per_fcda1767b0…` roughly twenty seconds later — both `external_directory`, both
for the same `/tmp/opencode/*` pattern, inside a single unit of work.

A notifier written as "alert when `/permission` is non-empty" therefore fires
repeatedly for what a human experiences as one decision, and its alerts carry no
identity, so they cannot be deduplicated or resolved.

The real payload shape, for reference:

```
id         per_fcd9baecb001adoyjREdwD6WXe
sessionID  ses_0330922baffeYA205kjqhuROJl
permission external_directory
patterns   ["/tmp/opencode/*"]
always     (the grant that would prevent a repeat)
metadata   { command: ... }
tool       { messageID, callID }
```

## Requirements

- [ ] Key every notification on the permission request `id`.
- [ ] Deduplicate: one alert per id, never re-alerted while still pending.
- [ ] Resolve: when a reply event arrives for an id, mark that alert answered so
      it stops nagging.
- [ ] Coalesce bursts — several pending ids in the same session within a short
      window should read as one notification listing them, not N notifications.
- [ ] Include the worktree and the requested pattern in the alert. "An agent
      wants permission" is not actionable; "the agent in `fix-buy-box-overflow`
      wants `/tmp/opencode/*`" is.

## Verification

Drive a session that raises two approval requests in one turn. Confirm exactly
one notification is produced, naming both patterns, and that answering both
clears it.

## Prohibited Patterns

- Do **not** key on `sessionID` alone; one session raises many requests.
- Do **not** assume one approval per task.
