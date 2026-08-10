# Event vocabulary

Extracted from a live server's OpenAPI document (`GET /doc`, 93 named event
schemas) and cross-checked against a real SSE stream. Both matter: the spec is
complete but not entirely accurate, and the stream emits at least one type the spec
does not name.

## Reading the stream

`GET /event` is Server-Sent Events. Lines arrive as `data: {json}` with the shape
`{"id": "evt_…", "type": "…", "properties": {…}}`.

**Read it with `readline()`, not by iterating the response object.** Iterating an
`HTTPResponse` buffers, so a low-traffic feed appears to deliver nothing at all
until enough bytes accumulate. This cost an hour; `curl -sN` worked while the
Python equivalent looked broken.

## Undocumented but real

Observed on the wire, absent from the schema list:

| Type | Why it matters |
| --- | --- |
| `server.connected` | Emitted immediately on connect. Cheap proof the stream is live. |
| `server.heartbeat` | Periodic keepalive, roughly every 8-10s. |

`server.heartbeat` is the one to care about. It separates **"the stream is alive and
the agent is quiet"** from **"the stream is dead"** — which is precisely the
absence-detection problem the daemon has to solve. Without it, a dropped
connection is indistinguishable from an idle agent. With it, missing heartbeats
mean the transport failed, and quiet-with-heartbeats means the agent is genuinely
doing nothing.

## What the daemon uses

### Liveness and state

| Type | Properties |
| --- | --- |
| `session.idle` | `sessionID` |
| `session.status` | `sessionID`, `status` — observed `{"type":"idle"}` |
| `session.error` | `sessionID`, `error` |
| `session.compacted` | `sessionID` |
| `session.diff` | `sessionID`, `diff` |

Note `/session/status` returns `{}` for idle while the event reports
`{"type":"idle"}`. Same fact, two encodings.

### Tool boundaries — exact, not inferred

| Type | Properties |
| --- | --- |
| `session.next.tool.called` | `timestamp`, `sessionID`, `assistantMessageID`, `callID`, `tool`, `input`, `provider` |
| `session.next.tool.progress` | `timestamp`, `sessionID`, `callID`, `structured`, `content` |
| `session.next.tool.success` | `timestamp`, `sessionID`, `callID`, `result`, `outputPaths`, … |
| `session.next.tool.failed` | `timestamp`, `sessionID`, `callID`, `error`, `result` |
| `session.next.shell.started` | `timestamp`, `sessionID`, `callID`, `command` |
| `session.next.shell.ended` | `timestamp`, `sessionID`, `callID`, `output` |

This is what makes per-state budgets possible rather than guesswork. A `called`
without a matching `success`/`failed` for the same `callID` **is** a long-running
tool, so "busy running a test suite" is directly observed. `progress` lets a slow
tool prove it is alive, so a generous budget can still be bounded.

### Cost and progress — loop detection for free

| Type | Properties |
| --- | --- |
| `session.next.step.started` | `timestamp`, `sessionID`, `agent`, `model`, `snapshot` |
| `session.next.step.ended` | `timestamp`, `sessionID`, `finish`, **`cost`**, **`tokens`**, `snapshot`, `files` |
| `session.next.step.failed` | `timestamp`, `sessionID`, `error` |
| `session.next.retried` | `timestamp`, `sessionID`, **`attempt`**, `error` |
| `todo.updated` | `sessionID`, `todos` |

`step.ended` carries cost and tokens per step, and `todo.updated` carries the todo
list. Cost climbing while todos stay static is the cheap proxy for a runaway loop,
and it needs no extra plumbing. `retried` exposes the harness's own retries, which
would otherwise look like unexplained slowness.

### Blocking on a human — two generations

| Type | Properties |
| --- | --- |
| `permission.asked` | `id`, `sessionID`, `permission`, `patterns`, `metadata`, `always`, `tool` |
| `permission.replied` | `sessionID`, `requestID`, `reply` |
| `permission.v2.asked` | `id`, `sessionID`, `action`, `resources`, `save`, `metadata`, `source` |
| `permission.v2.replied` | `sessionID`, `requestID`, `reply` |
| `question.asked` / `question.v2.asked` | `id`, `sessionID`, `questions`, `tool` |
| `question.replied` / `question.v2.replied` | `sessionID`, `requestID`, `answers` |
| `question.rejected` / `question.v2.rejected` | `sessionID`, `requestID` |

**Both generations exist in the same server.** v1 carries
`permission`/`patterns`/`always`; v2 carries `action`/`resources`/`save`. A reader
that handles only one will silently see nothing when the server speaks the other —
silence that reads as "nobody is blocked". Handle both.

The live pending request observed earlier used the **v1** shape
(`permission: external_directory`, `patterns: ["/tmp/opencode/*"]`, plus `always`),
so v1 is what 1.18.13 emitted in practice.

## Endpoint gotchas found while building this

| Endpoint | Reality |
| --- | --- |
| `/api/health` | Returns only `{"healthy":true}` — **no version** |
| `/global/health` | Returns `{"healthy":true,"version":"1.18.13"}` — use this for version negotiation |
| `/api/permission/request` | Returned empty against a demonstrably blocked session |
| `/api/session/active` | Returned empty against a demonstrably busy session |
| `/permission`, `/question`, `/session/status` | Correct. Use these. |
| `/api/location` | `{"directory": …, "project": {"id": …, "directory": …}}` — maps a port to a worktree |
| `--port` | `--help` claims `[default: 0]`, but omitting it starts **no server** |

## Full list

90 event types, with their property names, in `src/orch/opencode_events.py` — the
single source of truth. The `/doc` OpenAPI lists 93 schemas, but the stream does
not emit every one, and it does emit `server.heartbeat`, which has no schema; the
map holds the emitted vocabulary.
