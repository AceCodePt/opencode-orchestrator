# Task: Add `--port` to Every TUI Launch So Agents Are Observable

## Metadata

- **Complexity:** Low
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** None — this unblocks everything else

## Context

A TUI started without `--port` is unobservable. Verified on this machine: no
listening TCP port, no unix socket, `~/.local/state/opencode/locks/` empty, and
the `permission` table in `~/.local/share/opencode/opencode.db` holds **zero
rows** — pending approvals live only in TUI memory and never reach the store.

The same TUI started with `--port` answers HTTP. Verified: `--port 0` binds 4096
when free and an ephemeral port otherwise (a second instance took 37395), so
concurrent worktrees do not collide.

Three TUIs currently running were started without a port and are therefore
permanently unobservable. Only relaunching them helps. That is the cost of not
doing this task first.

## Requirements

- [ ] Add `--port 0` to the opencode invocation in `bin/task-runner.sh` (the
      `tmux send-keys` line that launches the agent).
- [ ] Add `--port 0` to the interactive alias used for hand-opened TUIs, so
      manually started sessions are observable too. Note that `iopencode` is not
      a command — it is `i` (nvim terminal insert) followed by `opencode`.
- [ ] Discovery must require no registry file: `ss -ltnp | grep opencode` yields
      port and pid; `readlink /proc/<pid>/cwd` and `GET /api/location` map the
      port to a worktree.
- [ ] Confirm two simultaneous worktree launches both get distinct live ports.

## Verification

Start two task worktrees at once, then from a third shell:

```bash
ss -ltnp | grep opencode                  # expect two distinct ports
curl -s localhost:<port>/api/location     # expect the matching worktree path
```

Both must report `{"healthy":true,...}` on their health endpoint.

## Prohibited Patterns

- Do **not** hardcode a fixed port per worktree. `--port 0` already avoids
  collisions; a static map will drift as worktrees come and go.
- Do **not** write a registry file of port->worktree. The kernel already knows;
  a file will go stale when a TUI dies.
