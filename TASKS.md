# Project Tasks

## Agent Protocol

This file is managed via **Optimistic Concurrency**.

### Executing Tasks

1. **Read**: Parse this file for the next available task (`[ ]`). If a task is
   marked `🔒 Blocked by: [...]`, verify the blocking task is `[x]` first.
2. **Lock**: Change the status to `[-]` (In Progress) and **commit immediately**.
   This is the lock. `bin/task-runner.sh` does this for you.
3. **Initialize**: Create the worktree via the task runner from the integration
   worktree. This creates the branch, spawns a detached tmux session, and threads
   `OPENCODE_PARENT_SESSION_ID` for reporting.
4. **Execute**: Follow the task's own `task.md`. Every task here is shell or
   TypeScript tooling — there is no schema/registry pipeline in this repo.
5. **Verify**: A task is not done because the code exists. It is done when the
   behaviour is observed against a **live opencode TUI**, and the task's
   Verification section says how. Claims about endpoints that were not exercised
   do not count.
6. **Commit**: Commit to the feature branch. Do **not** merge to `main` and do
   **not** delete the worktree. The user handles merging and cleanup.
7. **Complete**: Set the status to `[x]`.
8. **Report**: Notify the parent session:
   ```bash
   opencode --session $OPENCODE_PARENT_SESSION_ID run "Done with [task-name]. [summary]"
   ```

### Adding New Tasks

1. Create a directory in `tasks/` named after the task (kebab-case).
2. Write a `task.md` inside it: Context, Requirements, Verification, and
   Prohibited Patterns.
3. Add a `[ ]` entry **at the top** of the Backlog, linking to that file.
   Append `🔒 Blocked by: [Task Name]` if it has a dependency.

## Status Legend

- `[ ]` **Todo**: available to be picked up.
- `[-]` **In Progress**: currently being executed (this is what the runner writes).
- `[x]` **Done**: completed and verified against a live TUI.
- `[?]` **Indeterminate**: blocked or needs review.

## Backlog

Seeded in execution order rather than reverse-chronological; subsequent tasks go
at the top per the protocol above.

- [ ] [Add `--port` to Every TUI Launch So Agents Are Observable](tasks/add-port-to-tui-launch/task.md)
- [ ] [Subscribe to the Event Stream to Catch State Transitions](tasks/event-stream-listener/task.md) 🔒 Blocked by: [Add `--port` to Every TUI Launch So Agents Are Observable]
- [ ] [Watchdog for a TUI That Has Gone Silent Too Long](tasks/silence-watchdog/task.md) 🔒 Blocked by: [Subscribe to the Event Stream to Catch State Transitions]
- [ ] [Key Approval Notifications on Request ID and Survive Bursts](tasks/permission-burst-keying/task.md) 🔒 Blocked by: [Subscribe to the Event Stream to Catch State Transitions]
- [ ] [Pre-Seed `always` Permission Rules So Unattended Agents Do Not Stall](tasks/preseed-always-permissions/task.md)
- [-] [Version the Orchestrator and Cut `rdk-template` Over to It](tasks/version-the-orchestrator/task.md)
- [ ] [Detect an Agent That Has Looped Itself](tasks/detect-agent-self-loop/task.md) 🔒 Blocked by: [Watchdog for a TUI That Has Gone Silent Too Long]
