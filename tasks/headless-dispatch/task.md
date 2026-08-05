# Dispatch Headlessly via `run --attach`, and Derive Completion From the Session

## Context

Agents are headless servers, not TUIs. One `opencode serve` per machine hosts every
worktree; the TUI becomes an optional viewer launched with `opencode attach <url>`.

This deleted three problems rather than solving them: port discovery, the `--port`
contract, and the whole `unobservable` state. It is also 2.6× cheaper — 362 MB for
a shared `serve` against 947 MB per TUI — so memory stops being the concurrency
ceiling on the 8 GB box and CPU is the only remaining limit.

One shared server is safe for hooks because plugin loading is per-directory and
lazy: a server started in one directory, handed `?directory=<other>`, imported the
other directory's plugin and passed it `directory`/`worktree` pointing there.

## Requirements

- `orch task start` dispatches
  `opencode run --attach $URL --dir <worktree> --format json [-m model] "<prompt>"`
  as its own child process, after `post-task-create` has run.
- **Completion comes from the session, never from the exit code.** Measured: a run
  exited 0 at 07:25:53 and the session then performed 24 more tool calls until
  07:26:29, because the router injected a follow-up prompt. Anything that can inject
  a prompt — router, daemon, a human on a phone — outlives the dispatcher. Treat
  `run`'s exit as "my dispatch command returned", and derive doneness from
  `session.idle` with no gate work outstanding.
- Record pid, session id and `--dir` in the lease. Kill by **recorded pid**, never
  by pattern: `pkill -f 'opencode serve'` matched its own invoking shell and killed
  the command mid-flight, and would have taken an unrelated live task with it had
  the string matched. tmux needs no teardown — killing the process ends the session
  by itself.
- The gate must be committed before dispatch (see `gate-not-agent-writable`).

## Verification

Partly done. Verified: dispatch reaches a shared server in a different directory,
exits 0, streams JSON events, the target worktree's plugin loads with correct
context, and the gate fires. Free models (`opencode/deepseek-v4-flash-free`) report
`cost: 0`, so this is testable without spending money.

Outstanding: `orch task start` itself, and a completion signal derived from session
state rather than exit code — the part that is currently just a decision.

## Prohibited Patterns

- Using `run`'s exit code as task completion. Measured false.
- `pkill`/`pgrep -f` with any pattern that can match the caller. Both have already
  misfired here: one killed a command mid-run, the other reported a phantom agent.
- Treating tmux as a handle on an agent's lifetime.
- Reintroducing port scanning as a mechanism. The daemon spawns the server, so it
  knows the port, and `serve` announces it on stdout regardless. Scanning is for
  spotting stray hand-started TUIs, nothing more.
