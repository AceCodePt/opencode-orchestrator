from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import envfilter

ORCH_DIR = ".orchestration"
HOOKS_REL = f"{ORCH_DIR}/hooks"
CONFIG_REL = f"{ORCH_DIR}/config.json"

# The project's acceptance hook: the committed hooks/session-idle the daemon
# runs on settle. It verifies and, on a pass, commits the work.
VERIFY_HOOKS = ("session-idle",)

OK = "ok"
UNCOMMITTED = "uncommitted"
DIVERGED = "diverged"
MISSING = "missing"

PASSED = "passed"
FAILED = "failed"
NO_VERIFY = "no-verify-configured"

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
class HookFinding:
    name: str
    state: str
    detail: str

    @property
    def trustworthy(self) -> bool:
        return self.state in (OK, DIVERGED)


def inspect(worktree: Path, base: str | None = None) -> list[HookFinding]:
    base = base or base_branch(worktree)
    findings: list[HookFinding] = []

    for name in VERIFY_HOOKS:
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
            findings.append(HookFinding(
                name, UNCOMMITTED,
                f"exists in the worktree but is not committed on {base}, so the "
                f"daemon reports no-verify-configured and nothing will verify "
                f"this work",
            ))
            continue
        if local is None:
            findings.append(HookFinding(
                name, OK,
                f"committed on {base} and absent locally, which is fine - the "
                f"committed copy is what runs",
            ))
            continue
        if local != committed:
            findings.append(HookFinding(
                name, DIVERGED,
                f"the working-tree copy differs from {base}. The committed one runs, "
                f"so the verdict is still sound, but review the diff: an agent "
                f"editing its own verification hook is exactly what this flag is for",
            ))
            continue
        findings.append(HookFinding(name, OK, f"committed on {base} and unmodified"))

    cfg_committed = show(worktree, base, CONFIG_REL)
    cfg_local_exists = (worktree / CONFIG_REL).exists()
    if cfg_committed is None and cfg_local_exists:
        findings.append(HookFinding(
            "config.json", UNCOMMITTED,
            f"not committed on {base}, so the daemon falls back to defaults and any "
            f"retryCap here is silently ignored",
        ))
    elif cfg_committed is not None and cfg_local_exists:
        try:
            if (worktree / CONFIG_REL).read_text() != cfg_committed:
                findings.append(HookFinding(
                    "config.json", DIVERGED,
                    f"differs from {base}; the committed values are the ones in "
                    f"force",
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
            bits.append("hook-modified")
        if self.seconds:
            bits.append(f"{self.seconds:.1f}s")
        return " ".join(bits)


@dataclass
class CommittedRun:
    """The result of running a committed project hook. Callers map it onto
    their own contracts: the lifecycle hooks fail open on absence; the
    verification hook treats absence as its own outcome (NO_VERIFY)."""

    present: bool = False
    ok: bool = False
    exit_code: int | None = None
    output: str = ""
    diverged: bool = False
    seconds: float = 0.0
    timed_out: bool = False
    error: str = ""


def run_committed_hook(
    worktree: Path,
    name: str,
    env: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    base: str | None = None,
) -> CommittedRun:
    """Execute the committed copy of a project hook from the base branch,
    staged to a temp dir outside the worktree, so the working-tree copy is
    never the judge. The one runner both dispatchers use - the lifecycle hooks
    and the verification hook are the same mechanism."""
    worktree = Path(worktree)
    base = base or base_branch(worktree)
    rel = f"{HOOKS_REL}/{name}"

    committed = show(worktree, base, rel)
    if committed is None or not committed.strip():
        return CommittedRun()

    diverged = False
    try:
        diverged = (worktree / rel).read_text() != committed
    except OSError:
        diverged = True

    tmp = Path(tempfile.mkdtemp(prefix="orch-hook-"))
    exe = tmp / name
    try:
        exe.write_text(committed)
        exe.chmod(0o755)

        if envfilter.verify_env_full(worktree):
            base_env = dict(os.environ)
        else:
            base_env = envfilter.filtered_env(
                os.environ, repo_drop=envfilter.repo_drops(worktree))
        full_env = {**base_env, **(dict(env) if env else {})}
        started = time.monotonic()
        try:
            p = subprocess.run(
                [str(exe)], cwd=str(worktree), env=full_env,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
            if isinstance(partial, bytes):
                partial = partial.decode("utf8", "replace")
            return CommittedRun(
                present=True, ok=False, output=str(partial), diverged=diverged,
                seconds=time.monotonic() - started,
                timed_out=True, error=f"timed out after {timeout}s",
            )
        except OSError as e:
            return CommittedRun(
                present=True, ok=False, diverged=diverged,
                seconds=time.monotonic() - started, error=str(e),
            )
        secs = time.monotonic() - started
        output = ((p.stdout or "") + (p.stderr or "")).strip()
        return CommittedRun(
            present=True, ok=p.returncode == 0, exit_code=p.returncode,
            output=output, diverged=diverged, seconds=secs,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

    r = run_committed_hook(
        worktree, event,
        env={
            "WORKTREE_PATH": str(worktree),
            "WORKTREE_NAME": worktree.name,
            "ORCH_EVENT": event,
            "ORCH_SESSION_ID": session_id,
            "ORCH_VERIFIER": "daemon",
        },
        timeout=timeout, base=base,
    )
    if not r.present:
        return Verdict(
            event, NO_VERIFY,
            f"no {rel} committed on {base}. This is not a pass: nothing objected "
            f"because nothing looked.",
        )
    if r.timed_out:
        return Verdict(
            event, FAILED,
            f"verification hook timed out after {timeout}s\n{r.output[-1500:]}",
            r.seconds, r.diverged, None,
        )
    if r.error:
        return Verdict(event, FAILED, f"could not execute the hook: {r.error}",
                       r.seconds, r.diverged, None)
    if r.ok:
        return Verdict(event, PASSED, r.output[-2000:], r.seconds, r.diverged,
                       r.exit_code)
    return Verdict(event, FAILED, r.output[-2000:], r.seconds, r.diverged,
                   r.exit_code)


def blocking(findings: list[HookFinding]) -> list[HookFinding]:
    return [f for f in findings if not f.trustworthy]
