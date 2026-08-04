"""`orch doctor` - verify the contracts that fail silently.

Every check here exists because its failure mode is invisible. An unobservable
agent looks exactly like an idle one; a project with no gate looks exactly like a
project whose checks passed. Loud verification is the only defence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import harness
from .discovery import discover

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Hooks the CLI fires. A missing one is not necessarily wrong - a project with no
# database legitimately has no post-task-create.
TASK_HOOKS = [
    "pre-task-create",
    "post-task-create",
    "pre-task-cleanup",
    "post-task-cleanup",
    "pre-branch-delete",
]
# Hooks the router plugin fires. `session-idle` is the verification gate, and its
# absence must never be reported as success.
AGENT_HOOKS = ["session-idle", "session-error"]


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

    # It must be tracked. Untracked orchestration is the exact failure that
    # motivated this project.
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


def check_launch() -> list[Check]:
    """Is every running TUI observable, and will new ones be?"""
    out: list[Check] = []
    tuis = discover()
    if not tuis:
        out.append(Check(OK, "no TUIs running", ""))
    for t in tuis:
        if t.observable:
            out.append(Check(OK, f"pid {t.pid} observable", f"port {t.port} {t.cwd}"))
        else:
            out.append(
                Check(FAIL, f"pid {t.pid} has no --port",
                      f"{t.cwd} cannot be inspected at all; only relaunching helps")
            )

    # The wrapper is what stops new hand-opened TUIs from being born blind.
    # `--help` claims `--port [default: 0]`, but omitting the flag starts no
    # server - verified on 1.18.13.
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
    """Tools the gate degrades without.

    The shipped session-idle hook exits 0 when shellcheck is missing, warning on
    stderr. That is a deliberate compromise - a gate that fails because a linter is
    absent gets disabled by the first person it annoys - but the weakened coverage
    has to surface somewhere, and this is that place.
    """
    out: list[Check] = []
    for tool, why in (("shellcheck", "session-idle degrades to syntax-only without it"),
                      ("tmux", "required to dispatch task worktrees"),
                      ("ss", "required for TUI discovery"),
                      ("git", "required for everything")):
        found = shutil.which(tool)
        out.append(Check(OK if found else WARN, tool, found or why))
    return out


def run(cwd: Path | None = None) -> tuple[list[Check], int]:
    cwd = cwd or Path.cwd()
    checks: list[Check] = []
    checks += check_harness()
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
