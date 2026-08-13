from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import dispatch, harness, hooks, opencode_events, server, sse, state, verify

OK = "ok"
WARN = "warn"
FAIL = "fail"

TASK_HOOKS = [
    "pre-task-create",
    "post-task-create",
    "pre-task-cleanup",
    "post-task-cleanup",
    "pre-branch-delete",
]
AGENT_HOOKS = ["session-idle"]


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


def _find_orchestration(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        cand = d / ".orchestration"
        if cand.is_dir():
            return cand
        if (d / ".git").exists() and d != start:
            break
    return None


def check_project(cwd: Path) -> list[Check]:
    out: list[Check] = []
    orch_dir = _find_orchestration(cwd)

    if orch_dir is None:
        out.append(
            Check(
                FAIL,
                ".orchestration/ not found",
                "run `orch init`. Hook definitions must live inside a worktree and "
                "be tracked - a bare repo has no working tree, so anything placed "
                "beside it is outside version control by construction",
            )
        )
        return out

    out.append(Check(OK, ".orchestration/ found", str(orch_dir)))

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(orch_dir / "README.md")],
        capture_output=True, text=True, cwd=str(orch_dir.parent), check=False,
    )
    if tracked.returncode != 0:
        out.append(
            Check(WARN, ".orchestration/ is not tracked by git",
                  "it has no history and no backup, and cannot differ per branch")
        )
    else:
        out.append(Check(OK, ".orchestration/ is tracked", ""))

    cfg_path = orch_dir / "config.json"
    if not cfg_path.is_file():
        out.append(Check(WARN, "config.json missing", "the verification hook config is absent"))
    else:
        try:
            cfg = json.loads(cfg_path.read_text())
            vc = cfg.get("verify", {})
            out.append(Check(OK, "config.json parses",
                             f"retryCap={vc.get('retryCap', 'unset')}"))
            if not vc.get("retryCap"):
                out.append(Check(WARN, "verify.retryCap unset",
                                 "a failing verification hook would re-prompt the agent forever"))
        except (json.JSONDecodeError, OSError) as e:
            out.append(Check(FAIL, "config.json is unreadable", str(e)))

    hooks_dir = orch_dir / "hooks"
    present = {p.name for p in hooks_dir.iterdir()} if hooks_dir.is_dir() else set()

    for name in AGENT_HOOKS + TASK_HOOKS:
        p = hooks_dir / name
        if name not in present:
            continue
        if not os.access(p, os.X_OK):
            out.append(Check(FAIL, f"hook {name} is not executable", str(p)))
        else:
            out.append(Check(OK, f"hook {name}", "executable"))

    missing_task = [h for h in TASK_HOOKS if h not in present]
    if missing_task:
        out.append(
            Check(OK, "task hooks absent (fine)",
                  ", ".join(missing_task) + " - a project with no database needs none")
        )
    samples = sorted(p.name for p in hooks_dir.glob("*.sample"))
    if samples:
        shown = ", ".join(samples[:8])
        extra = f" and {len(samples) - 8} more" if len(samples) > 8 else ""
        out.append(
            Check(OK, "hook samples present (inert)",
                  shown + extra + " - copy one to its real name to enable it")
        )
    return out


def check_server() -> list[Check]:
    out: list[Check] = []
    st = server.status()
    if not st.get("running"):
        out.append(Check(
            WARN, "no shared server",
            f"{st.get('reason')}. `orch server start`, or let `orch task start` "
            f"bring one up",
        ))
        return out

    if st.get("unresponsive"):
        out.append(Check(
            FAIL, f"shared server pid {st['pid']} is alive but answers nothing",
            f"port {st['port']} does not respond to /global/health. "
            f"`orch server restart`, then re-dispatch anything that was running",
        ))
        return out

    out.append(Check(
        OK, f"shared server pid {st['pid']}",
        f"{st['url']} version {st.get('version') or '?'} up {st.get('uptime_s')}s",
    ))
    out.extend(check_harness_version(st))
    return out


def check_daemon() -> list[Check]:
    from . import daemon as daemon_mod

    info = daemon_mod.unit_status()
    if not info.get("installed"):
        return [Check(
            FAIL, "no supervisor installed",
            "nothing settles tasks or runs verification hooks, so no task will ever "
            "reach done. `orch install`",
        )]
    out: list[Check] = []
    if info.get("ActiveState") != "active":
        out.append(Check(
            FAIL, f"supervisor is {info.get('ActiveState')}/{info.get('SubState')}",
            "installed but not running. `systemctl --user status orch-daemon` and "
            "`journalctl --user -u orch-daemon`",
        ))
    else:
        out.append(Check(OK, f"supervisor running pid {info.get('ExecMainPID')}",
                         "settles tasks, runs verification hooks, retriggers agents, merges "
                         "and archives"))
    out.extend(_check_daemon_freshness(info))
    if info.get("linger") is False:
        out.append(Check(
            FAIL, "linger is off, so the supervisor dies with your session",
            f"an unattended reboot comes back with nothing supervising. "
            f"`loginctl enable-linger {os.environ.get('USER', '$USER')}`",
        ))
    return out


def _check_daemon_freshness(info: dict) -> list[Check]:
    if info.get("ActiveState") != "active":
        return []
    pid = info.get("ExecMainPID")
    if not pid or str(pid) in ("-", "0"):
        return [Check(WARN, "cannot find the supervisor's pid", "")]
    try:
        p = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=10)
        elapsed = int(p.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return [Check(WARN, "cannot read the supervisor's start time", "")]
    started_at = int(time.time()) - elapsed
    src = Path(__file__).resolve().parent
    newest = max((f.stat().st_mtime for f in src.glob("*.py")
                  if f.is_file()), default=0)
    if newest > started_at:
        return [Check(
            FAIL, "the running supervisor predates the installed code",
            "a long-lived process holds what it imported at startup, so it is "
            "still enforcing the older policy and nothing in its logs will say "
            "so. `systemctl --user restart orch-daemon`",
        )]
    return [Check(OK, "supervisor runs the installed code", "")]


def check_harness_version(st: dict) -> list[Check]:
    raw = st.get("version")
    if not raw:
        return [Check(WARN, "harness version unknown",
                      "cannot compare against the supported floor "
                      f"{'.'.join(map(str, harness.MIN_VERSION))}")]
    try:
        v = tuple(int(x) for x in str(raw).split(".")[:3])
    except ValueError:
        return [Check(WARN, f"harness version {raw} is unparseable", "")]
    floor = ".".join(map(str, harness.MIN_VERSION))
    if v < harness.MIN_VERSION:
        return [Check(FAIL, f"harness {raw} is below the supported floor {floor}",
                      "the box is pinned to that floor, so anything older is "
                      "untested here - treat it as an unsatisfied dependency")]
    return [Check(OK, f"harness {raw} meets the floor {floor}",
                  "newer is a bonus, not a requirement")]


def check_verification(cwd: Path) -> list[Check]:
    out: list[Check] = []
    code = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if code.returncode != 0:
        return out
    top = Path(code.stdout.strip())
    findings = verify.inspect(top)
    if not findings:
        out.append(Check(
            OK, "no verification hook",
            "tasks are accepted without verification. To verify, copy "
            "hooks/session-idle.sample to hooks/session-idle, make it executable, "
            "and commit it on the base branch",
        ))
        return out
    for f in findings:
        if f.state == verify.OK:
            out.append(Check(OK, f"verify hook {f.name}", f.detail))
        elif f.state == verify.DIVERGED:
            out.append(Check(WARN, f"verify hook {f.name} diverges from the base branch",
                             f.detail))
        else:
            out.append(Check(FAIL, f"verify hook {f.name}: {f.state}", f.detail))
    return out


def check_supervised_roots() -> list[Check]:
    roots = state.default_roots()
    if "/" in roots:
        return [Check(
            WARN, "ORCH_ROOTS includes /",
            "discovery spans the whole filesystem. Authority is trust-bound, "
            "but supervision should still be narrowed",
        )]
    return [Check(OK, "supervised roots", ", ".join(roots))]


def check_opencode_deny() -> list[Check]:
    from . import daemon as daemon_mod

    p = daemon_mod.opencode_config_path()
    if not p.exists():
        return [Check(
            FAIL, "no global opencode deny for .orchestration/",
            "an agent could edit its own verification hook through the file tools. "
            "`orch install` writes the deny rules",
        )]
    try:
        cfg = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return [Check(FAIL, "global opencode config is unreadable", str(p))]
    edit = ((cfg or {}).get("permission") or {}).get("edit") or {}
    missing = [k for k in daemon_mod.OPENCODE_DENY_RULES
               if edit.get(k) != "deny"]
    if missing:
        return [Check(
            FAIL, "global opencode config does not deny .orchestration/**",
            "missing deny for: " + ", ".join(missing) + ". `orch install`",
        )]
    return [Check(OK, "agents cannot edit .orchestration/", str(p))]


def check_trust(cwd: Path) -> list[Check]:
    _top, common, err = dispatch.project_of(cwd)
    if err or common is None:
        return []
    if state.is_trusted(common):
        return [Check(OK, "project is trusted by the daemon", str(common))]
    return [Check(
        FAIL, "project is not trusted",
        "the daemon will not verify, merge or dispatch here. "
        "`orch project trust` (from any worktree of this project)",
    )]


def check_task_dependencies(cwd: Path) -> list[Check]:
    """Every task's `dependencies:` front matter must point at a slug that is
    either declared or archived. A typo'd dependency can never be archived, so
    the scheduler would hold the task forever."""
    out: list[Check] = []
    _top, common, err = dispatch.project_of(cwd)
    if err or common is None:
        return out
    proj = state.Project.from_common(common)
    if proj is None:
        return out
    tasks = proj.scan()
    declared = {t.name for t in tasks if t.declared}
    archived = {t.name for t in tasks if t.archived}
    known = declared | archived
    broken: list[str] = []
    for t in tasks:
        if not t.declared:
            continue
        for dep in t.dependencies:
            if dep not in known:
                broken.append(f"{t.name} -> {dep}")
    if broken:
        out.append(Check(
            FAIL, "task dependencies reference nothing",
            "these edges can never be archived, so the scheduler holds them "
            "forever. Fix the slug in the front matter (or declare/archive the "
            "target): " + ", ".join(broken),
        ))
    else:
        out.append(Check(OK, "task dependencies resolve",
                         "every dependencies: slug is declared or archived"))
    return out


def check_harness() -> list[Check]:
    out: list[Check] = []
    exe = shutil.which("opencode")
    out.append(
        Check(OK if exe else FAIL, "opencode on PATH", exe or "not found")
    )
    return out


def check_tooling() -> list[Check]:
    out: list[Check] = []
    for tool, why in (("shellcheck", "session-idle degrades to syntax-only without it"),
                      ("git", "required for everything")):
        found = shutil.which(tool)
        out.append(Check(OK if found else WARN, tool, found or why))
    return out


def check_global_hooks() -> list[Check]:
    out: list[Check] = []
    d = hooks.hooks_dir()
    found = sorted(
        p for p in d.iterdir()
        if p.is_file() and os.access(p, os.X_OK)
    ) if d.exists() else []
    if not found:
        out.append(Check(OK, "global hooks",
                         "none configured (opt-in); `orch init` writes "
                         ".example templates to " + str(d)))
        return out
    for p in found:
        if p.name in hooks.GLOBAL_HOOKS:
            out.append(Check(OK, f"global hook {p.name}",
                             f"fired on {hooks.GLOBAL_HOOKS[p.name]}"))
        elif p.name in {sse.hook_name(e) for e in opencode_events.OPENCODE_EVENTS}:
            out.append(Check(OK, f"global hook {p.name}",
                             "an opencode event hook: fired by the SSE consumer "
                             "when the server emits that event"))
        else:
            out.append(Check(
                WARN, f"global hook {p.name} is not a known event",
                "only names in hooks.GLOBAL_HOOKS and the opencode event "
                "vocabulary fire; rename it to match"))
    return out


def run(cwd: Path | None = None) -> tuple[list[Check], int]:
    cwd = cwd or Path.cwd()
    checks: list[Check] = []
    checks += check_harness()
    checks += check_server()
    checks += check_daemon()
    checks += check_opencode_deny()
    checks += check_supervised_roots()
    checks += check_verification(cwd)
    checks += check_tooling()
    checks += check_project(cwd)
    checks += check_trust(cwd)
    checks += check_task_dependencies(cwd)
    checks += check_global_hooks()
    checks += check_custom_tools()
    worst = 0
    for c in checks:
        if c.status == FAIL:
            worst = 2
        elif c.status == WARN and worst < 1:
            worst = 1
    return checks, worst


def check_custom_tools() -> list[Check]:
    from . import custom_tools

    problems = custom_tools.check()
    if problems:
        out = []
        for msg in problems:
            out.append(Check(FAIL, "custom tools", msg))
        return out
    out = [Check(OK, "custom tools",
                 "create_task/update_task/archive_task/delete_task installed and current")]
    info = server.current()
    newest = 0
    try:
        for p in custom_tools.TOOLS_DIR.glob("*.ts"):
            newest = max(newest, int(p.stat().st_mtime))
    except OSError:
        pass
    if info is not None and newest and info.started_at < newest:
        out.append(Check(
            WARN, "custom tools", "the shared server predates the installed tool "
            "files - restart it (`orch server restart`) so sessions can call them"))
    return out


_MARK = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}
def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_MARK[c.status]} {c.title}")
        if c.detail:
            lines.append(f"        {c.detail}")
    n_fail = sum(1 for c in checks if c.status == FAIL)
    n_warn = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    lines.append(f"{len(checks)} checks | {n_fail} failed | {n_warn} warnings")
    return "\n".join(lines)
