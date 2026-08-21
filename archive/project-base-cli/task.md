---
wait_human_start: false
wait_human_merge: false
dependencies: [project-base-repoint-spec, project-base-backend]
---

# Task: CLI: `orch project base` command wiring in cli.py

## Metadata

- **Complexity:** Medium
- **Priority:** High
- **Status:** Ready for Handoff

## Context

Wire the backend `project.base()` function into the CLI as `bin/orch project base <branch> [-C <dir>]`. Model the parser and handler on the existing `cmd_project_scheduler` (cli.py ~1169) for project resolution via Project.from_dir and on `cmd_project_clone` (cli.py ~1055/1093) for the interactive branch picker. This slice depends on the backend slice, which provides the function it calls.

## Requirements

- [ ] A `project base` subcommand is registered in build_parser (next to create/clone/trust/untrust/list/scheduler), taking `<branch>` positionally and `-C/--directory` to select the project (default CWD).
- [ ] When no branch is given on a terminal, an interactive branch picker lists the repo's branches (reusing the clone pattern) and prompts.
- [ ] `cmd_project_base` resolves the project via `Project.from_dir`, calls the backend `project.base()` function, and prints its status lines mirroring the clone command's output.
- [ ] On a backend failure the command prints the problem to stderr and returns non-zero; on success it returns 0 and prints a `next:` style hint.
- [ ] Unit tests cover: the parser accepts `<branch>` and `-C`; `cmd_project_base` drives the backend and returns 0/1 correctly for success/unknown-branch (mirroring existing cmd-level test patterns in tests/test_project.py / tests/test_cli.py).
- [ ] All tests in tests/test_project.py and tests/test_cli.py pass.

## Verification

`python3 tests/test_project.py && python3 tests/test_cli.py` pass, including the new cmd-level tests asserting the parser shape and that `cmd_project_base` calls the backend and returns 0 on success / 1 on unknown branch.

## Prohibited Patterns

- Do NOT add a separate parser flag that auto-creates branches; the branch must already exist.
- Do NOT change behavior of existing project subcommands.
- Do NOT print trust/notification noise the new command does not cause.
- Do NOT add comments unless the surrounding style already uses them.
