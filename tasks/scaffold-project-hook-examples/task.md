---
wait_human_start: false
wait_human_merge: false
dependencies: []
---

# Task: Scaffold non-executable hook .example templates into .orchestration/hooks/ on project init

## Metadata

- **Complexity:** Medium
- **Priority:** Medium
- **Status:** Ready for Handoff

## Context

When orch initializes a project (orch project clone on a repo without .orchestration/, project create, or orch init), init_mod.scaffold() writes only config.json, README.md, .gitignore, and .version into .orchestration/ and installs hook templates only machine-level at ~/.config/orch/hooks/*.example. The project's .orchestration/hooks/ directory comes out empty. The user observed this when cloning the reeeally-main project and expects the project hook directory to be populated with the non-executable .example templates (each carrying its inner env-var parameter contract), matching the global directory, and those samples must stay gitignored (not committed). Decision confirmed with the user: copy only the project-relevant set (~95 files: session-idle + 5 task-lifecycle hooks + 90 opencode-event hooks, deduped), NOT the 10 machine-level GLOBAL_HOOKS advisory hooks. The pre-orchestrated clone path (has_orch branch) should also drop these .example references into .orchestration/hooks/ without committing.

## Requirements

- [ ] When a project is initialized (project create, project clone on a repo without .orchestration/, and orch init), copy the project-relevant hook .example templates into the project's .orchestration/hooks/ directory.
- [ ] The copied set is exactly: session-idle.example, the five task-lifecycle .example files (pre-task-create, post-task-create, pre-task-cleanup, post-task-cleanup, pre-branch-delete), and one .example per opencode server event (opencode_events hook_name, excluding session-idle). The ten GLOBAL_HOOKS advisory templates are NOT copied.
- [ ] Each copied .example file is non-executable (mode like -rw-r--r--), matching the machine-level .example files; enabling a hook still requires copy-to-real-name, chmod +x, and commit.
- [ ] Copied .example files remain gitignored: the existing .orchestration/.gitignore already ignores *.sample and *.example, so a freshly initialized project's git status is clean and the samples are not committed.
- [ ] Copying is idempotent: files already present are skipped (a second init/clone does not overwrite them).
- [ ] The already-orchestrated clone path (clone of a repo that ships .orchestration/) also drops the project-relevant .example references into .orchestration/hooks/ as ignored, non-executable files, without committing them and without altering trust behavior.
- [ ] project create and clone report the created .example copies in their step output (e.g. created .orchestration/hooks/session-idle.example).
- [ ] Docs/comments that currently claim nothing template-shaped is scaffolded into a repo are updated to reflect that inert .example references are now provided in .orchestration/hooks/, while real hooks remain a copy-to-real-name + chmod +x + commit action.

## Verification

Run the test harness (python tests/test_init.py, python tests/test_hooks.py, python tests/test_project.py) and a manual scenario: orch project clone of a repo without .orchestration/ produces .orchestration/hooks/ containing session-idle.example, the five task-lifecycle .example files, and ~90 event .example files, all non-executable (no +x), all ignored (git status --porcelain is clean), and git ls-files shows none of them tracked. Clone of a repo WITH .orchestration/ also gets these .example references dropped in, uncommitted and untrusted by default. A second init/clone leaves them unchanged.

## Prohibited Patterns

- Do NOT copy the ten GLOBAL_HOOKS advisory templates (alert, needs-review, restart-server, merged, removed, dispatched, dispatch-failed, resolve-failed, reset, archived) into the project hook dir - they are machine-level and fire for every project.
- Do NOT make the copied .example files executable - they are inert references; enabling requires a real copy with chmod +x and a commit.
- Do NOT commit the .example samples to the base branch - they stay ignored per the existing .orchestration/.gitignore.
- Do NOT change the pre-orchestrated clone trust behavior - a clone that ships .orchestration/ stays untrusted by default regardless of the dropped references.
