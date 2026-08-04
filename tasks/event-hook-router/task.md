# Task: Generic Event→Hook Router Plugin

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff
- **Dependencies:** `orch-cli-skeleton`

## Context

Hooks are named after **what happened**, not what to do. The plugin is a router,
not a feature: an event arrives, the router decides whether anything changed, and
if so it invokes `.orchestration/hooks/<event-name>` and hands the result to the
orchestrator.

`rdk-template`'s existing `auto-check.ts` is this pattern with one project's policy
welded in. It already hooks `session.idle`, fingerprints changed files, runs
`pnpm check && pnpm test`, and injects failures back into the child. The work here
is to keep the mechanism and evict the policy — `pnpm check && pnpm test` becomes
`.orchestration/hooks/session-idle`.

The payoff is that new events cost configuration, not code.

## Requirements

- [ ] Route any harness event to a same-named hook if one exists; do nothing if
      not.
- [ ] Preserve the existing guards: skip when `session.parentID` is set (subagent)
      and when the session errored.
- [ ] Diff gate before invoking: hash **contents** of a project-declared file set,
      not mtimes. `find -printf "%T@ %p"` false-fires on `git checkout`, `touch`,
      and stash.
- [ ] **Persist** the fingerprint. Today the baseline lives in plugin memory, so a
      TUI restart re-baselines and can skip the first check entirely.
- [ ] Which files matter is project config, including exclusions such as markdown
      on a non-blog site.
- [ ] Inject results with `prompt_async`, never `tui.appendPrompt` +
      `submitPrompt`.
- [ ] Acquire a concurrency slot from the daemon before running a hook.
- [ ] Report the outcome to the orchestrator. The plugin does not decide who is
      told.

## Verification

Install into a scratch project with a deliberately failing `session-idle` hook.
Confirm: no invocation when nothing changed; invocation on a real content change;
failure text arriving in the child session without touching the prompt box; and a
`passed` outcome reaching the orchestrator when the hook exits 0.

## Prohibited Patterns

- Do **not** name the plugin or its hooks after what they do. `session-idle`, not
  `auto-check`.
- Do **not** drive the TUI text box as an IPC channel. `report_back` currently
  calls `clearPrompt()` on the parent, which erases whatever the human was typing.
- Do **not** treat `session.idle` as "task complete". It also means confused, out
  of context, or ending a turn with a question.
