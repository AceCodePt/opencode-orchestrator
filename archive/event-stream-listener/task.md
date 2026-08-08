# Task: Subscribe to the Event Stream to Catch State Transitions

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** `add-port-to-tui-launch`

## Context

Polling misses events. This is measured, not theoretical. A 2-second poll loop
against a live TUI produced:

```
19:35:19  perm=[1 external_directory:per_fcd9baecb0]
19:35:31  perm=[0 -]
19:35:40  perm=[1 external_directory:per_fcda1767b0]
19:35:46  perm=[0 -]
```

Two distinct approval requests appeared and cleared inside 27 seconds. Any
request raised and answered between two samples is invisible forever. For a
signal that gates an agent's progress, sampling is the wrong instrument.

`GET /event` is a server-sent-events stream and is the correct source for
transitions. Its exact event names have **not** been captured yet — that is the
main deliverable here, because every downstream consumer keys off them.

## Requirements

- [ ] Subscribe to `GET /event` per discovered TUI (`curl -N` is enough to
      explore; the daemon should hold a persistent connection).
- [ ] Record the real event names and payload shapes for at least:
      permission requested, permission replied, question asked, session idle,
      session error, tool call start/end.
- [ ] Write the observed names into this repo as documentation. Do not guess
      them from the endpoint list.
- [ ] **`session.idle` is already confirmed**, carrying
      `event.properties.sessionID` — `rdk-template`'s `auto-check.ts` handles it in
      production. The plugin API and the observer read the same bus, so existing
      plugins are a free partial spec for this task. Start by reading them.
- [ ] Handle reconnect: a TUI exits when its task ends, and the listener must not
      die or spin when the socket closes.
- [ ] Multiplex many TUIs into one stream of `(port, worktree, event)`.

## Verification

With a listener attached, trigger a real approval prompt in a TUI and confirm the
stream emits a request event *before* a human answers, and a reply event after.
Then kill the TUI and confirm the listener reconnects or retires that port
cleanly without a busy loop.

## Prohibited Patterns

- Do **not** use `/api/session/active` or `/api/permission/request`. They are not
  equivalent to the unprefixed routes — both returned empty while a session was
  demonstrably busy and blocked. Use `/permission`, `/question`,
  `/session/status`.
- Do **not** depend on `/experimental/*`. It is unstable by name, and opencode's
  worktree list has already drifted from `git worktree list`.
- Do **not** replace polling with SSE. The watchdog task needs both; absence of
  events is not an event.
