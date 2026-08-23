---
wait_human_start: true
wait_human_merge: false
dependencies: [global-orch-install]
---

# Task: `orch install` copies the entire repo to the canonical location

## Metadata

- **Complexity:** Medium
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

First deliverable of the global-orch-install effort (spec anchor): `orch install` makes orch available globally as a physical copy of the entire repo at one canonical path, with a real launcher on PATH. No update yet - that is the separate `orch-update` slice. The copy is `.git`-free so it stays a static snapshot; update will later swap the whole tree, so update does not need `.git` in the install. The repo-local `bin/orch` stays usable for development.

## Requirements

- [ ] `orch install` (also runnable as `bin/orch install` from a checkout) copies the entire repo tree minus `.git` to the canonical location `$XDG_DATA_HOME/orch` (default `~/.local/share/orch`), preserving the `bin/` + `src/` sibling layout so `server.orch_bin()` and `custom_tools._tool_bin()` resolve inside the copy.
- [ ] It writes a real (non-symlink) launcher file at `~/.local/bin/orch` (dir created if missing) that imports `orch.cli.main` from the installed `src`; it refuses to overwrite an existing non-orch file there.
- [ ] After install, `command -v orch` resolves and `orch version` prints the installed version with no checkout present.
- [ ] PATH check: if the launcher directory is not on `$PATH`, it prints `export PATH="$HOME/.local/bin:$PATH"` and notes the current shell needs a restart/re-source.
- [ ] Idempotent: reports `created` / `updated` / `already current`.
- [ ] `orch uninstall` removes the launcher and the canonical install directory, and never touches any project's `.orchestration/`.
- [ ] The install works without the source checkout: deleting the checkout afterwards leaves `orch` functional.

## Verification

From a fresh checkout, `bin/orch install` succeeds; `command -v orch` resolves and `orch version` prints. Delete the checkout directory and `orch version` still works. In a PATH without `~/.local/bin`, install prints the export line. `orch uninstall` removes the launcher and `~/.local/share/orch`, after which `command -v orch` no longer resolves. Run `for t in tests/test_*.py; do python3 "$t"; done` and see all tests pass, including a new install/uninstall round-trip test.

## Prohibited Patterns

- Do NOT symlink the launcher to a repo checkout - the launcher must be a real file and the installed copy a physical tree.
- Do NOT copy `.git` (or any of it) into the canonical install location.
- Do NOT touch any project's `.orchestration/` during install or uninstall.
- Do NOT require sudo/admin or a package manager.
