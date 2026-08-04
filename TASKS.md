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
   opencode TUI**, in the way the task's Verification section describes. Code that
   exists is not a completed task, and an endpoint that was never exercised is not
   evidence.
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
- `[x]` **Done**: completed and verified against a live TUI.
- `[?]` **Indeterminate**: blocked or needs review.

## Backlog

Seeded in rough execution order. Later additions go at the top per the protocol.

**Foundations**

- [-] [Build the `orch` CLI Skeleton With `init`, `task start`, `observe`, `doctor`](tasks/orch-cli-skeleton/task.md) — observe/doctor/init/events/version done; `task start` outstanding
- [ ] [Add `--port` to Every TUI Launch So Agents Are Observable](tasks/add-port-to-tui-launch/task.md)
- [x] [Claims, Leases and the Intent/Runtime Split in SQLite](tasks/claims-and-leases/task.md) — src/orch/store.py, 9 tests green

**Sensing**

- [x] [Subscribe to the Event Stream and Capture the Event Vocabulary](tasks/event-stream-listener/task.md) — 93 types + 2 undocumented, see docs/events.md
- [ ] [Watchdog for a TUI That Has Gone Silent Too Long](tasks/silence-watchdog/task.md) 🔒 Blocked by: [Subscribe to the Event Stream and Capture the Event Vocabulary]

**Acting**

- [ ] [Route Plugin State Through the Daemon Instead of `.orchestration/.state/`](tasks/plugin-state-via-daemon/task.md) 🔒 Blocked by: [Phone-Facing Server: Read Status and Answer Approvals Over the Tailnet]

- [-] [Generic Event→Hook Router Plugin](tasks/event-hook-router/task.md) — plugin/orch-router.ts written, untested against a live agent
- [ ] [Bounded Retries on the Check Gate, Then Halt](tasks/check-gate-retry-cap/task.md) 🔒 Blocked by: [Generic Event→Hook Router Plugin]
- [ ] [Outcome Routing With a Per-Event Human-in-the-Loop Toggle](tasks/outcome-routing/task.md) 🔒 Blocked by: [Claims, Leases and the Intent/Runtime Split in SQLite]
- [ ] [Key Approval Notifications on Request ID and Survive Bursts](tasks/permission-burst-keying/task.md) 🔒 Blocked by: [Subscribe to the Event Stream and Capture the Event Vocabulary]
- [ ] [Pre-Seed `always` Permission Rules So Unattended Agents Do Not Stall](tasks/preseed-always-permissions/task.md)
- [ ] [Phone-Facing Server: Read Status and Answer Approvals Over the Tailnet](tasks/phone-server/task.md) 🔒 Blocked by: [Key Approval Notifications on Request ID and Survive Bursts]

**Measurement and migration**

- [ ] [Measure Concurrent Check Runs and Decide the Semaphore Limit](tasks/concurrency-semaphore/task.md) 🔒 Blocked by: [Generic Event→Hook Router Plugin]
- [x] [Test Whether `parentID` Works Across Differing `directory`](tasks/parentid-cross-directory/task.md) — both intra- and cross-project parentage work; prediction falsified
- [ ] [Migrate `rdk-template` to `.orchestration/` and Delete the Untracked Originals](tasks/migrate-rdk-template/task.md) 🔒 Blocked by: [Generic Event→Hook Router Plugin]

**Deferred**

- [ ] [Detect an Agent That Has Looped Itself](tasks/detect-agent-self-loop/task.md) 🔒 Blocked by: [Watchdog for a TUI That Has Gone Silent Too Long]
