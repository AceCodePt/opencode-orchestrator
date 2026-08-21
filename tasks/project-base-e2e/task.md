---
wait_human_start: false
wait_human_merge: false
dependencies: [project-base-repoint-spec, project-base-backend, project-base-cli]
---

# Task: E2E: full-loop repoint for backend and CLI

## Metadata

- **Complexity:** Medium
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

End-to-end test for the `project base` repoint feature, covering BOTH the backend path and the CLI path. Model on tests/test_e2e.py which builds a real project and runs the full loop (dispatch from the base branch, verify, merge) guarded by `_have_opencode()`. The user explicitly requires e2e for both the backend and the CLI. Runs after the backend and CLI slices are archived; the repo's doc states tests run with `for t in tests/test_*.py; do python3 "$t"; done` and e2e needs a real `opencode` on PATH.

## Requirements

- [ ] An e2e test (in a new tests/test_project_base_e2e.py or extending tests/test_e2e.py) builds a throwaway project whose base branch is `main`, then repoints it to a second existing branch via the BACKEND `project.base()` function.
- [ ] The same e2e also drives the repoint through the CLI command path (`cmd_project_base`/the `project base` subcommand) and asserts it returns 0 and leaves the same final state.
- [ ] After repoint, a task dispatched from the NEW base branch completes the full loop: branch from the new base, verification hook (committed on the new base) runs, verified work merges into the new base.
- [ ] The old base branch still exists as a normal branch afterward.
- [ ] The e2e skips gracefully (reports skipped, non-failing) when `opencode` is not installed, matching the existing e2e guard style.
- [ ] All e2e tests pass (or skip) cleanly when run with `python3 tests/test_project_base_e2e.py`.

## Verification

`python3 tests/test_project_base_e2e.py` (and the suite `for t in tests/test_*.py; do python3 "$t"; done`) passes, or reports `skipped` when `opencode` is absent; when run with opencode present it proves a task completes the verify+merge loop from the repointed base for both the backend and CLI paths, with the old base branch still present.

## Prohibited Patterns

- Do NOT skip the opencode guard; the e2e must degrade gracefully (report skipped) when `opencode` is absent, like existing e2e tests.
- Do NOT test only one path; both the backend and the CLI must be exercised.
- Do NOT add comments unless the surrounding style already uses them.
- Do NOT depend on the machine owner's real projects; build throwaway repos in a temp dir.
