from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import gate, harness, server
from .discovery import discover

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
        out.append(Check(WARN, "config.json missing", "the diff gate has no file set to watch"))
    else:
        try:
            cfg = json.loads(cfg_path.read_text())
            gate = cfg.get("gate", {})
            if not gate.get("include"):
                out.append(Check(WARN, "config.json declares no gate.include",
                                 "every change will look irrelevant, so the gate never runs"))
            else:
                out.append(Check(OK, "config.json parses",
                                 f"retryCap={gate.get('retryCap', 'unset')}"))
            if not gate.get("retryCap"):
                out.append(Check(WARN, "gate.retryCap unset",
                                 "a failing gate would re-prompt the agent forever"))
        except (json.JSONDecodeError, OSError) as e:
            out.append(Check(FAIL, "config.json is unreadable", str(e)))

    hooks_dir = orch_dir / "hooks"
    present = {p.name for p in hooks_dir.iterdir()} if hooks_dir.is_dir() else set()

    if "session-idle" not in present:
        out.append(
            Check(FAIL, "no session-idle hook",
                  "absence of a gate is not a pass. Without it every task reports "
                  "no-gate-configured, and treating that as success would manufacture "
                  "confidence in unchecked work")
        )
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
            "nothing settles tasks or runs gates, so no task will ever "
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
                         "settles tasks, runs gates, retriggers agents, merges "
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


def check_gates(cwd: Path) -> list[Check]:
    out: list[Check] = []
    code = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if code.returncode != 0:
        return out
    top = Path(code.stdout.strip())
    findings = gate.inspect(top)
    if not findings:
        out.append(Check(
            FAIL, "no gate configured",
            "nothing verifies work in this project, so every task would finish as "
            "`no-gate-configured` - which reads like a note and means nothing was "
            "checked",
        ))
        return out
    for f in findings:
        if f.state == gate.OK:
            out.append(Check(OK, f"gate {f.name}", f.detail))
        elif f.state == gate.DIVERGED:
            out.append(Check(WARN, f"gate {f.name} diverges from the base branch",
                             f.detail))
        else:
            out.append(Check(FAIL, f"gate {f.name}: {f.state}", f.detail))
    return out


def check_launch() -> list[Check]:
    out: list[Check] = []
    tuis = discover()
    if not tuis:
        out.append(Check(OK, "no hand-started TUIs", ""))
    for t in tuis:
        if t.observable:
            out.append(Check(OK, f"pid {t.pid} observable", f"port {t.port} {t.cwd}"))
        else:
            out.append(
                Check(WARN, f"hand-started pid {t.pid} has no --port",
                      f"{t.cwd} cannot be inspected. Not an orchestration problem "
                      f"any more - dispatched agents are headless servers and always "
                      f"listen - but you cannot see this one without relaunching it")
            )

    zshrc = Path.home() / ".zshrc"
    try:
        body = zshrc.read_text()
        if "opencode()" in body and "--port 0" in body:
            out.append(Check(OK, "shell wrapper injects --port 0", str(zshrc)))
        else:
            out.append(
                Check(WARN, "no shell wrapper for opencode",
                      "hand-opened TUIs will have no port. --help says "
                      "'--port [default: 0]' but omitting it starts no server")
            )
    except OSError:
        out.append(Check(WARN, "could not read ~/.zshrc", ""))
    return out


def check_harness() -> list[Check]:
    out: list[Check] = []
    exe = shutil.which("opencode")
    out.append(
        Check(OK if exe else FAIL, "opencode on PATH", exe or "not found")
    )
    for t in discover():
        if not t.observable or t.port is None:
            continue
        caps = harness.capabilities(t.port)
        if caps.too_old:
            out.append(Check(WARN, f"pid {t.pid} version", "; ".join(caps.notes)))
        elif caps.version:
            out.append(
                Check(OK, f"pid {t.pid} version",
                      ".".join(map(str, caps.version)))
            )
    return out


def check_tooling() -> list[Check]:
    out: list[Check] = []
    for tool, why in (("shellcheck", "session-idle degrades to syntax-only without it"),
                      ("tmux", "optional now: dispatch is headless, tmux only hosts TUIs you open"),
                      ("ss", "only needed to spot stray hand-started TUIs now"),
                      ("git", "required for everything")):
        found = shutil.which(tool)
        out.append(Check(OK if found else WARN, tool, found or why))
    return out


def run(cwd: Path | None = None) -> tuple[list[Check], int]:
    cwd = cwd or Path.cwd()
    checks: list[Check] = []
    checks += check_harness()
    checks += check_server()
    checks += check_daemon()
    checks += check_gates(cwd)
    checks += check_launch()
    checks += check_tooling()
    checks += check_project(cwd)
    worst = 0
    for c in checks:
        if c.status == FAIL:
            worst = 2
        elif c.status == WARN and worst < 1:
            worst = 1
    return checks, worst


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
