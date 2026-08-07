from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import envfilter

ORCH_DIR = ".orchestration"
HOOKS_REL = f"{ORCH_DIR}/hooks"
CONFIG_REL = f"{ORCH_DIR}/config.json"

GATE_EVENTS = ("session-idle",)

OK = "ok"
UNCOMMITTED = "uncommitted"
DIVERGED = "diverged"
MISSING = "missing"

PASSED = "passed"
FAILED = "failed"
NO_GATE = "no-gate-configured"

DEFAULT_TIMEOUT_S = 900


def _git(worktree: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, p.stdout


def show(worktree: Path, ref: str, rel: str) -> str | None:
    code, out = _git(worktree, "show", f"{ref}:{rel}")
    return out if code == 0 else None


def base_branch(worktree: Path) -> str:
    import json
    try:
        cfg = json.loads((worktree / CONFIG_REL).read_text())
        b = cfg.get("baseBranch")
        return str(b) if b else "main"
    except (OSError, ValueError, AttributeError):
        return "main"


@dataclass
class GateFinding:
    name: str
    state: str
    detail: str

    @property
    def trustworthy(self) -> bool:
        return self.state in (OK, DIVERGED)


def inspect(worktree: Path, base: str | None = None) -> list[GateFinding]:
    base = base or base_branch(worktree)
    findings: list[GateFinding] = []

    for name in GATE_EVENTS:
        rel = f"{HOOKS_REL}/{name}"
        committed = show(worktree, base, rel)
        local_path = worktree / rel
        local = None
        try:
            local = local_path.read_text()
        except OSError:
            pass

        if committed is None and local is None:
            continue
        if committed is None:
            findings.append(GateFinding(
                name, UNCOMMITTED,
                f"exists in the worktree but is not committed on {base}, so the "
                f"daemon reports no-gate-configured and nothing will verify "
                f"this work",
            ))
            continue
        if local is None:
            findings.append(GateFinding(
                name, OK,
                f"committed on {base} and absent locally, which is fine - the "
                f"committed copy is what runs",
            ))
            continue
        if local != committed:
            findings.append(GateFinding(
                name, DIVERGED,
                f"the working-tree copy differs from {base}. The committed one runs, "
                f"so the verdict is still sound, but review the diff: an agent "
                f"editing its own gate is exactly what this flag is for",
            ))
            continue
        findings.append(GateFinding(name, OK, f"committed on {base} and unmodified"))

    cfg_committed = show(worktree, base, CONFIG_REL)
    cfg_local_exists = (worktree / CONFIG_REL).exists()
    if cfg_committed is None and cfg_local_exists:
        findings.append(GateFinding(
            "config.json", UNCOMMITTED,
            f"not committed on {base}, so the daemon falls back to defaults and any "
            f"include/exclude/retryCap here is silently ignored",
        ))
    elif cfg_committed is not None and cfg_local_exists:
        try:
            if (worktree / CONFIG_REL).read_text() != cfg_committed:
                findings.append(GateFinding(
                    "config.json", DIVERGED,
                    f"differs from {base}; the committed values are the ones in "
                    f"force, because exclude:['**'] would otherwise let an agent "
                    f"switch its own gate off without touching a hook",
                ))
        except OSError:
            pass

    return findings


@dataclass
class Verdict:
    event: str
    outcome: str
    detail: str
    seconds: float = 0.0
    diverged: bool = False
    exit_code: int | None = None

    @property
    def justifies_done(self) -> bool:
        return self.outcome == PASSED

    @property
    def summary(self) -> str:
        bits = [self.outcome]
        if self.diverged:
            bits.append("gate-modified")
        if self.seconds:
            bits.append(f"{self.seconds:.1f}s")
        return " ".join(bits)


def run(
    worktree: Path,
    event: str = "session-idle",
    base: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    session_id: str = "",
) -> Verdict:
    worktree = Path(worktree)
    base = base or base_branch(worktree)
    rel = f"{HOOKS_REL}/{event}"

    committed = show(worktree, base, rel)
    if committed is None or not committed.strip():
        return Verdict(
            event, NO_GATE,
            f"no {rel} committed on {base}. This is not a pass: nothing objected "
            f"because nothing looked.",
        )

    diverged = False
    try:
        diverged = (worktree / rel).read_text() != committed
    except OSError:
        diverged = True

    tmp = Path(tempfile.mkdtemp(prefix="orch-gate-"))
    exe = tmp / event
    try:
        exe.write_text(committed)
        exe.chmod(0o755)

        if envfilter.gate_env_full(worktree):
            base_env = dict(os.environ)
        else:
            base_env = envfilter.filtered_env(
                os.environ, repo_drop=envfilter.repo_drops(worktree))
        env = {
            **base_env,
            "WORKTREE_PATH": str(worktree),
            "WORKTREE_NAME": worktree.name,
            "ORCH_EVENT": event,
            "ORCH_SESSION_ID": session_id,
            "ORCH_VERIFIER": "daemon",
        }
        started = time.monotonic()
        try:
            p = subprocess.run(
                [str(exe)], cwd=str(worktree), env=env,
                capture_output=True, text=True, timeout=timeout,
            )
            secs = time.monotonic() - started
            output = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode == 0:
                return Verdict(event, PASSED, output[-2000:], secs, diverged, 0)
            return Verdict(event, FAILED, output[-2000:], secs, diverged, p.returncode)
        except subprocess.TimeoutExpired as e:
            secs = time.monotonic() - started
            partial = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
            if isinstance(partial, bytes):
                partial = partial.decode("utf8", "replace")
            return Verdict(
                event, FAILED,
                f"gate timed out after {timeout}s\n{str(partial)[-1500:]}",
                secs, diverged, None,
            )
        except OSError as e:
            return Verdict(event, FAILED, f"could not execute the gate: {e}",
                           time.monotonic() - started, diverged, None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def blocking(findings: list[GateFinding]) -> list[GateFinding]:
    return [f for f in findings if not f.trustworthy]
