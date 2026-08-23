---
wait_human_start: false
wait_human_merge: false
dependencies: [global-orch-install, orch-install-copy]
---

# Task: `orch update` pulls, swaps, and reconfigures the global install

## Metadata

- **Complexity:** High
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

Second deliverable of the global-orch-install effort (spec anchor): `orch update` refreshes the globally installed copy. Decided with the user: no path argument - there is exactly one canonical install path; and update never pulls inside the installed `.git`-free snapshot, it clones the canonical repo into a temp dir, verifies, swaps the tree into the canonical location, then runs the machine reconfiguration (systemd daemon unit if its template changed, custom-tool bridges + shared server restart if they changed, hook templates). The canonical source is the repo's own origin: git@github.com:AceCodePt/opencode-orchestrator.git.

## Requirements

- [ ] A new `orch update` subcommand refreshes the canonical global install; it accepts no path argument.
- [ ] It fetches the canonical repo (git@github.com:AceCodePt/opencode-orchestrator.git) into a temp dir (shallow clone is enough), verifies the fetched tree, then swaps it into the canonical install location (e.g. atomic rm+mv), discarding the temp `.git`.
- [ ] After the swap it runs the necessary machine reconfiguration: regenerate/reload the systemd daemon unit if its template changed, re-ensure the custom-tool bridges and restart the shared server if they changed, and refresh hook templates if the installer changed them.
- [ ] Idempotent: when the installed copy is already current, it reports so and changes nothing.
- [ ] Safe failure: a fetch or verification error leaves the current install completely untouched.
- [ ] The one canonical path is a constant; update always targets it.

## Verification

With the installed copy behind upstream, `orch update` refreshes it, `orch version` reports the newer version, the systemd daemon unit is regenerated/reloaded when its template changed, and the shared server restarts only if the custom tools changed. Passing a path argument is refused. A simulated fetch failure leaves the previous install intact. Run `for t in tests/test_*.py; do python3 "$t"; done` and see all tests pass, including an update round-trip test.

## Prohibited Patterns

- Do NOT accept a path argument for the update target - there is exactly one canonical install path.
- Do NOT mutate the installed copy in place with git (e.g. `git pull` inside `~/.local/share/orch`); fetch into a temp dir and swap.
- Do NOT leave a failed fetch partially applied - a verification failure must leave the current install untouched.
- Do NOT touch any project's `.orchestration/` during update.
- Do NOT commit secrets or environment-specific paths.
