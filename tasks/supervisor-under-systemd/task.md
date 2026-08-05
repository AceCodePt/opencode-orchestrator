# Task: Keep the Supervisor Alive Across Logout and Reboot

## Metadata

- **Complexity:** Small
- **Priority:** High
- **Status:** Done
- **Dependencies:** none

## Context

The daemon was started by hand. Without it nothing renews a lease and no stopped agent
is ever verified, so no task can reach `done` — but everything else still works, so the
absence is invisible. Dispatch succeeds, agents run, in-loop gates fire. The system
looks healthy right up to the moment you ask it for a result.

It also had to stop being per-project. There is one shared server, one database and one
set of leases per machine, so a per-project daemon must be remembered once per project
and supervises nothing for any project nobody started.

## Requirements

- [x] One daemon per machine, supervising every project.
- [x] Actions carry their project, since task names are unique only within one.
- [x] A systemd **user** unit: the daemon needs no privilege beyond the account owning
      the worktrees it checks.
- [x] Survives logout and reboot.
- [x] Restarting the unit does not kill the shared server or any running agent.
- [x] `doctor` fails — not warns — when nothing is supervising.

## Outcome

`orch daemon install` writes `~/.config/systemd/user/orch-daemon.service`, reloads,
enables, starts, and turns on linger. `orch daemon status` reports unit state and
linger separately.

Three settings are load-bearing, all of them silent when wrong:

- `Restart=always` — a supervisor that exited *cleanly* is still not supervising, and
  nothing else notices.
- `KillMode=process` — the server and agents are started with `start_new_session` and
  must survive a unit restart. Verified: unit restarted, server untouched, same pid.
- `loginctl enable-linger` — without it the unit starts at login and dies at logout, so
  an unattended reboot comes back supervising nothing while `systemctl status` looked
  correct when it was set up.

## Verification

Performed: unit active and enabled with linger on; `systemctl --user restart` left the
shared server at the same pid; `doctor` reports the supervisor running. Two unit tests
assert `Restart=always` and `KillMode=process`, and one asserts that reclaiming a task
in one project leaves an identically named task in another untouched.

Not yet performed: an actual reboot.
