---
wait_human_start: true
wait_human_merge: false
dependencies: []
---

# Task: Install orch globally (physical copy at one canonical path)

## Metadata

- **Complexity:** Medium
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

Everything about orch assumes a globally installed `orch` command: CLI messages and the README spell commands `orch <cmd>`, `server.orch_bin()` and `custom_tools._tool_bin()` already fall back to `shutil.which("orch")`, and the daemon systemd unit bakes an absolute `{orch}` path into `ExecStart`. But today there is no install step: `bin/orch` is a plain repo script. This effort adds one via a physical copy of the entire repo (stdlib-only Python, so there is nothing meaningful to compile). A symlink-to-checkout was considered and rejected: moving/deleting the checkout leaves a dangling link and a generic `no such file or directory` error. Instead the install is a static, `.git`-free snapshot at one canonical path, and `orch update` refreshes it by cloning the canonical repo into a temp dir and swapping the tree in (so update never needs `.git` inside the install). Decided: fold the install into `orch install`; `orch update` takes no path argument - there is exactly one canonical path. Split into two deliverable slices: (1) `orch install` copies the repo and writes the launcher, (2) `orch update` pulls + applies + reconfigures (systemd unit, custom-tool bridges, hook templates). The user wants to discuss/review before dispatch, so this anchor stays a `wait_human_start` draft.

## Requirements

- [ ] On a machine with orch installed, `orch` resolves on PATH through a real launcher file, with no repo checkout required.
- [ ] The install is a physical copy of the entire orch repo minus `.git` at exactly one canonical location (`$XDG_DATA_HOME/orch`, default `~/.local/share/orch`); it is not a symlink to a checkout.
- [ ] There is exactly one canonical path; `orch update` never accepts or uses any other path.
- [ ] Every command documented in the README (spelled `orch <cmd>`) works through the installed copy.
- [ ] `server.orch_bin()` and `custom_tools._tool_bin()` resolve inside the installed copy.
- [ ] Install is idempotent and removable (`orch uninstall`), and never creates, modifies, or deletes anything inside any project's `.orchestration/`.
- [ ] The repo-local `bin/orch` still works from a checkout; the checkout does not require the install and the install does not require the checkout.
- [ ] `orch update` refreshes the install by fetching the canonical repo into a temp dir, swapping the verified tree into the canonical location, then running the necessary machine reconfiguration (systemd daemon unit, custom-tool bridges + server restart if they changed, hook templates).
- [ ] The mechanism and its constraints are captured in docs/decisions.md (symlink rejected and why, one canonical path, update = clone-to-temp + swap, XDG locations).

## Verification

From a checkout, `bin/orch install` installs globally; after deleting the checkout, `command -v orch` still resolves and `orch version` prints the installed version. With the installed copy behind upstream, `orch update` refreshes it, `orch version` bumps, the systemd daemon unit is regenerated/reloaded as needed, and the shared server restarts only if the custom tools changed. `orch uninstall` removes the launcher and the canonical install dir. Run `for t in tests/test_*.py; do python3 "$t"; done` and see all tests pass, including install/update/uninstall round-trip tests.

## Prohibited Patterns

- Do NOT use a symlink to a repo checkout for the global install (dangling-link fragility).
- Do NOT add a path argument to `orch update` - there is only one canonical path.
- Do NOT require a package manager or runtime that contradicts the project's stdlib-only stance without recording the decision first.
- Do NOT write, modify, or delete anything inside any project's `.orchestration/` as part of install/update/uninstall.
- Do NOT commit secrets or environment-specific paths.
