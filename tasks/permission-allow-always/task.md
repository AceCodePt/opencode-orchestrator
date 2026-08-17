---
wait_human_start: true
wait_human_merge: false
dependencies: []
---

# Task: Support "remember" via opencode's native Allow-always TUI, and document it as the sanctioned exception to one-time approval

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff

## Context

The user wants orch to support "remember like in opencode" (approve-always) for permission asks. Investigation confirmed the capability already exists and needs NO harness/CLI production code: orch drives headless sessions on one shared `opencode serve` (server.py), and `orch attach` already hands the user opencode's own interactive TUI pointed at the blocked task or primary-agent session (cli.py:934). opencode's server permission reply vocabulary is once|always|reject and `harness.reply_permission` already accepts "always" (harness.py:187); opencode's TUI natively offers "Allow always" (permission.tsx: options once:"Allow once", always:"Allow always", reject:"Reject", and a confirm screen "This will allow the following patterns until OpenCode is restarted"). The user explicitly asked: not per-session, must be equivalent to opencode (i.e. server-memory grants until the shared server restarts), and implemented as the TUI's own approve-always rather than a new harness/CLI verb or a second persistence layer.

The work is therefore: (1) prove the TUI path actually works against a live orch-blocked headless session (that is the open question the user left: "if you can"); (2) update the docs that currently claim permissions are strictly allow/deny and one-time (README.md ~150-193 + security bullet ~360, docs/architecture.md rows ~234/242 and security bullet ~517, docs/decisions.md cli.py section ~361) to teach the TUI Allow-always surface as the sanctioned remember path, scoped until `orch server restart`.

This deliberately avoids re-implementing persistence in orch's harness/CLI (which would drift from opencode semantics and widen the one-time security model) and is distinct from the left-behind task `preseed-always-permissions` (config-level rules so unattended agents never stall) and `phone-server` (approve-always from a phone), which stay separate. The user has confirmed the scope statement and asked to proceed (the CLI discoverability-hint question was left out).

## Requirements

- [ ] Verify live, against the running shared server (opencode 1.18.16, provider configured), that `orch attach` to a headless session blocked on a permission presents opencode's native prompt with all three options: `Allow once`, `Allow always`, `Reject` (options appear in that shape; Allow always present).
- [ ] Verify selecting `Allow always` in that TUI unblocks the blocked session (the agent proceeds past the ask).
- [ ] Verify at least one repeat of the same ask is auto-allowed (no second prompt) while the shared server keeps running.
- [ ] Verify the scope is opencode-equivalent, not per-session and not persistent: after `orch server restart`, a fresh matching ask is NOT auto-allowed (grants are server-memory), confirming the documented scope.
- [ ] Repeat the attach-and-allow-always check once for the primary agent (`orch attach` from the base worktree yielding the `primary-agent@<base>` session) as well as a task agent.
- [ ] Document, in README.md's Permission approval section and security bullet, that remember = opencode's native Allow-always in the attached TUI (orch attach), scoped to the shared server's lifetime (cleared by orch server restart), and that orch's own `permissions` walk / `task answer` remain one-time by design.
- [ ] Document the same in docs/architecture.md (the `orch permissions`/`task answer` rows and the 'Permissions are one-time' security bullet) and add a decision entry to docs/decisions.md's cli.py section recording why orch does not re-implement persistence in the harness/CLI and defers to the TUI's own Allow-always.
- [ ] No production code change to harness reply-permission routing and no new CLI verb / walk option for persistence; existing tests in tests/test_*.py continue to pass unchanged.
- [ ] make no change outside docs/ and the verification procedure (no src/ edits unless an actual defect is found; a defect found is reported to the human, not silently patched).

## Verification

Runnable against the live box: (1) `python3 -m compileall src` and `for t in tests/test_*.py; do python3 \"$t\"; done` all pass with no src/ changes. (2) A blocked headless task session (raised by a real agent via `orch task start`) and the primary agent session each, when attached to with `orch attach`, render opencode's permission prompt showing `Allow always`; choosing it unpends the session and a second identical ask is auto-allowed, and after `orch server restart` the ask is raised again (not remembered). (3) `grep -n "Allow always\\|allow-always\\|Allow-always" README.md docs/architecture.md docs/decisions.md` shows the remember surface and its server-restart scope documented in all three, and no README/docs text still asserts permissions are strictly one-time/allow-deny without that qualification.

## Prohibited Patterns

- Do NOT add a 'remember'/'always' verb to the orch permissions walk, `task answer`, or one-shot, and do NOT change harness reply-permission routing to teach persistence.
- Do NOT introduce a persistent allow/deny state store (global/project/task policy tiers) - the design deliberately has none, and opencode's TUI already owns remember.
- Do NOT scope remember per-session or per-task; it is server-memory until the shared server restarts, matching opencode.
- Do NOT silently paper over a live verification failure with a doc-only change; if the TUI path does not present Allow always, stop and report to the human.
- Do NOT edit src/ production code unless a genuine defect is found (and then only to fix that defect with a test).
