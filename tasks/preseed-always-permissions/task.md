---
hitl: true
---

# Task: Pre-Seed `always` Permission Rules So Unattended Agents Do Not Stall

## Metadata

- **Complexity:** Low
- **Priority:** Medium
- **Status:** Ready for Handoff
- **Dependencies:** None

## Context

Every pending approval is a stalled agent. Locally that is a minor annoyance —
the TUI is on screen and gets answered in seconds. On the box it is fatal: the
agent runs in a detached tmux session that nobody is looking at, so it waits
indefinitely for an answer that will never come.

The observed repeat prompts for `/tmp/opencode/*` are the clearest example. The
permission payload includes an `always` field naming exactly the grant that would
have prevented the second prompt, so the fix is available without guessing.

This is the difference between an agent that finishes unattended and one that
looks busy while doing nothing.

## Requirements

- [ ] Enumerate the approval prompts that recur in normal operation. Derive them
      from observed requests, not from imagination.
- [ ] Decide per pattern whether it is safe to pre-grant, and write down why.
      `/tmp/opencode/*` is a scratch directory; a pattern reaching outside the
      worktree into real source or credentials is not the same question.
- [ ] Configure the safe grants so a fresh worktree starts with them in place.
- [ ] Leave everything else prompting. A blanket allow-all defeats the purpose of
      the approval mechanism and of the branch firewall.
- [ ] Ensure a still-blocked unattended agent is surfaced by the watchdog rather
      than being silently pre-approved into doing something unreviewed.

## Verification

Run a task on the box, detached, with no human watching. It must reach completion
without waiting on an approval prompt. Then confirm that a deliberately
out-of-scope request — something touching a path outside the worktree that was
not pre-granted — still blocks and still raises an alert.

## Prohibited Patterns

- Do **not** pre-grant broadly to make the stalling go away. The goal is that
  known-safe repeats stop prompting, not that nothing prompts.
- Do **not** pre-grant anything the branch firewall exists to prevent.
