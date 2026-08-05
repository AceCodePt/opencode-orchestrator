# Project Tasks

Architecture: `docs/architecture.md`. Read it before picking anything up — most of
these tasks are one component of a design that only makes sense whole.

## Agent Protocol

This file is managed via **Optimistic Concurrency**.

### Executing Tasks

1. **Read**: Parse this file for the next available task (`[ ]`). If a task is
   marked `🔒 Blocked by: [...]`, verify the blocking task is `[x]` first.
2. **Lock**: Change the status to `[-]` (In Progress) and **commit immediately**.
   That commit is the lock.
3. **Initialize**: Create the worktree from the integration worktree. Note that
   this repo currently has no dispatcher of its own — bootstrapping is manual
   until `orch-cli-skeleton` lands. Recursion comes later, deliberately.
4. **Execute**: Follow the task's own `task.md`. Everything here is tooling; there
   is no schema/registry pipeline in this repo.
5. **Verify**: A task is done when the behaviour is observed against a **live
   headless `opencode serve`**, in the way the task's Verification section
   describes. Code that exists is not a completed task, and an endpoint that was
   never exercised is not evidence.
6. **Commit**: Commit to the feature branch. Do **not** merge to `main` and do
   **not** delete the worktree.
7. **Complete**: Set the status to `[x]`.

### Adding New Tasks

1. Create a directory in `tasks/` named after the task (kebab-case).
2. Write a `task.md`: Context, Requirements, Verification, Prohibited Patterns.
3. Add a `[ ]` entry **at the top** of the Backlog, linking to it. Append
   `🔒 Blocked by: [Task Name]` where there is a dependency.

## Status Legend

- `[ ]` **Todo**: available to be picked up.
- `[-]` **In Progress**: currently being executed.
- `[x]` **Done**: completed and verified against a live agent.
- `[?]` **Indeterminate**: blocked or needs review.

## Backlog

Seeded in rough execution order. Later additions go at the top per the protocol.

**Foundations**

- [x] [Dispatch Headlessly via `run --attach`, and Derive Completion From the Session](tasks/headless-dispatch/task.md) — a task reached `done` live, from a gate the daemon ran itself after the session stopped
- [x] [Survive Death of the Shared Server by Re-Dispatching From Leases](tasks/shared-server-recovery/task.md) — the daemon restarts it and escalates affected tasks; re-dispatch/resume is still a human decision
- [x] [Build the `orch` CLI Skeleton With `init`, `task start`, `observe`, `doctor`](tasks/orch-cli-skeleton/task.md) — plus server, attach, gate, task list/reset, daemon
- [x] [Add `--port` to Every TUI Launch So Agents Are Observable](tasks/add-port-to-tui-launch/task.md) — superseded: agents are headless servers, which always have a port. The zshrc wrapper stays as a convenience for hand-opened TUIs.
- [x] [Claims, Leases and the Intent/Runtime Split in SQLite](tasks/claims-and-leases/task.md) — src/orch/store.py, 9 tests green

**Sensing**

- [x] [Subscribe to the Event Stream and Capture the Event Vocabulary](tasks/event-stream-listener/task.md) — 93 types + 2 undocumented, see docs/events.md
- [x] [Keep the Supervisor Alive Across Logout and Reboot](tasks/supervisor-under-systemd/task.md) — systemd user unit, machine-wide, Restart=always, KillMode=process, linger on; `doctor` fails when nothing is supervising
- [x] [Distinguish an Agent That Finished From One That Merely Stopped](tasks/completion-signal/task.md) — `finished` is the in-loop gate loop terminating, not a claim by the agent; no in-loop verdict now means no verification
- [x] [Watchdog for a TUI That Has Gone Silent Too Long](tasks/silence-watchdog/task.md) — per-state budgets and the reclaim rule are implemented and tested in `orch daemon`; still poll-only, so SSE-driven absence detection remains

**Acting**

- [x] [Route Plugin State Through the Daemon Instead of `.orchestration/.state/`](tasks/plugin-state-via-daemon/task.md) — state moved into the one database, reached through the CLI rather than an HTTP endpoint; see the deviation note in the task file. Unblocked from the phone server as a result.

- [x] [Generic Event→Hook Router Plugin](tasks/event-hook-router/task.md) — verified end to end against a live headless agent: event → hook → fail → prompt injection → agent fix → pass
- [x] [Bounded Retries on the Check Gate, Then Halt](tasks/check-gate-retry-cap/task.md) — observed `failed: attempt 1/3` → `passed` with the counter reset, and `stuck: gave up after 1 attempts` at the cap
- [x] [Take the Gate Out of the Agent's Writable Surface](tasks/gate-not-agent-writable/task.md) — the committed gate is the only one that runs and only the daemon's own run can promote a task; routing `gate-modified` to a human remains, and it is now the last piece
- [ ] [Outcome Routing With a Per-Event Human-in-the-Loop Toggle](tasks/outcome-routing/task.md) — unblocked; verdicts now land in the store with a source, so there is something to route
- [ ] [Key Approval Notifications on Request ID and Survive Bursts](tasks/permission-burst-keying/task.md) — unblocked; dedup by request id is implemented in the store, the notification itself is not
- [ ] [Pre-Seed `always` Permission Rules So Unattended Agents Do Not Stall](tasks/preseed-always-permissions/task.md)
- [ ] [Phone-Facing Server: Read Status and Answer Approvals Over the Tailnet](tasks/phone-server/task.md) 🔒 Blocked by: [Key Approval Notifications on Request ID and Survive Bursts]

**Measurement and migration**

- [ ] [Measure Concurrent Check Runs and Decide the Semaphore Limit](tasks/concurrency-semaphore/task.md) 🔒 Blocked by: [Generic Event→Hook Router Plugin]
- [x] [Test Whether `parentID` Works Across Differing `directory`](tasks/parentid-cross-directory/task.md) — both intra- and cross-project parentage work; prediction falsified
- [ ] [Migrate `rdk-template` to `.orchestration/` and Delete the Untracked Originals](tasks/migrate-rdk-template/task.md) 🔒 Blocked by: [Generic Event→Hook Router Plugin]

**Deferred**

- [ ] [Detect an Agent That Has Looped Itself](tasks/detect-agent-self-loop/task.md) 🔒 Blocked by: [Watchdog for a TUI That Has Gone Silent Too Long]
