# Event vocabulary

Extracted from the running OpenCode 2 server binary and cross-checked against a
live `GET /api/event` stream. The V2 stream is authenticated and its envelope
changed from V1: properties now live under `data`, and the type names changed too.

## Reading the stream

`GET /api/event` is Server-Sent Events, authenticated with the service password
(HTTP Basic, user `opencode`). Lines arrive as `data: {json}` with the shape:

```json
{"id": "evt_…", "created": 1790184689133, "type": "session.tool.success",
 "location": {"directory": "/path/to/worktree"}, "data": {…}}
```

`server.heartbeat` arrives as an SSE comment line, `: heartbeat`, not a `data:`
event. **Read the stream with `readline()`, not by iterating the response
object.** Iterating an `HTTPResponse` buffers, so a low-traffic feed appears to
deliver nothing at all until enough bytes accumulate; a comment line read that way
also stalls. `harness._parse_sse_data` ignores non-`data:` lines, so both
heartbeats and the odd non-JSON line are skipped.

Routing: a session-scoped event's `data.sessionID` maps to the worktree that owns
the session (via the location registry); the global hook fires for every event.

## What the daemon uses

The daemon decides task state from the REST endpoints and the worktree, never from
the stream — the stream is a trigger for advisory hooks only. The REST facts are:

| Fact | Endpoint |
| --- | --- |
| server info / version | `GET /api/info` (`{version, pid, urls, paths}`) |
| sessions in a worktree | `GET /api/session?directory=<worktree>` |
| busy sessions | `GET /api/session/active` (`{data: {ses_…: {type: "running"}}}`) |
| pending permission asks | `GET /api/permission/request?location[directory]=<worktree>` |
| pending questions (forms) | `GET /api/form?location[directory]=<worktree>` |
| a session's turn outcome | `GET /api/session/{id}` (`outcome: succeeded\|failed\|interrupted`) |
| recent messages | `GET /api/session/{id}/message?limit=N` |

V2 records the turn outcome on the session itself, which replaces V1's
message-record archaeology for detecting an interrupted turn. A pending ask whose
`source.messageID` was aborted is still **orphaned** — opencode does not
garbage-collect the ask when the turn that created it is stopped.

## Endpoint gotchas found while building this

| Endpoint | Reality |
| --- | --- |
| `/session`, `/permission`, `/event` (V1 paths) | gone from the API; V2 uses `/api/*` and requires the password |
| `/api/info` | authenticated; the only place the version comes from now (there is no unauthenticated `/global/health`) |
| `/api/permission/request`, `/api/form`, `/api/location` | location-scoped by an **object** query, `location[directory]=<path>`; `?directory=` is rejected |
| `/api/session` | list scopes by a plain `?directory=`; **create** takes `location: {directory}` in the body |
| `/api/session/{id}/prompt` | the V2 send; `delivery` is `steer` or `queue` |
| reply to a permission | session-scoped: `POST /api/session/{sid}/permission/{rid}/reply` `{decision, message}` |
| reply to a question | it is a form: `POST /api/session/{sid}/form/{fid}/reply` `{answer: {field: value}}` |
| `/api/event` | envelope `{id, created, type, location, data}`; heartbeat is a `: heartbeat` comment |

## Full list

The event types are in `src/orch/opencode_events.py` — the single source of truth
for the vocabulary (extracted from the running server binary). The property names
are not enumerated: the whole event JSON is on stdin, so a hook never needs to
know them ahead of time.

## Hooks for these events

`orch init` installs one inert hook template per event type —
`~/.config/orch/hooks/<event>.example`, dots mapped to dashes
(`session.idle` → `session-idle`). The daemon's SSE consumer reads
`GET /api/event` and fires the copied hook for each event, with the event's JSON on
stdin and `ORCH_EVENT=<dashed-name>` set. A session-scoped event fires its
project hook in the worktree that owns the session; a copied executable global
hook fires for every event. The vocabulary lives in `src/orch/opencode_events.py`
and all templates come from one source, so they cannot drift. On init the
project-relevant `.example` references (session-idle, the lifecycle hooks, and one
per opencode server event) are also scaffolded into the project's
`.orchestration/hooks/`, gitignored and non-executable, matching the global set;
copy one to its real name, make it executable, and commit it to enable it. One
asymmetry: `session.idle` is the verification hook's name, so it keeps the
verification template instead of a generic event one; globally, where there is no
verification hook, it still gets an event example.
