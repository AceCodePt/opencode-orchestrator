from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ORCH_DIR = ".orchestration"
HOOKS_REL = f"{ORCH_DIR}/hooks"
CONFIG_REL = f"{ORCH_DIR}/config.json"

ROUTER_RELS = (
    ".opencode/plugins/orch-router.ts",
    ".opencode/plugin/orch-router.ts",
)

MIN_ROUTER_CONTRACT = 2
_CONTRACT_RE = re.compile(r"ROUTER_CONTRACT\s*=\s*(\d+)")


def router_contract(worktree: Path) -> tuple[int | None, str | None]:
    for rel in ROUTER_RELS:
        p = worktree / rel
        try:
            text = p.read_text()
        except OSError:
            continue
        m = _CONTRACT_RE.search(text)
        return (int(m.group(1)) if m else 1), str(p)
    return None, None

GATE_EVENTS = ("session-idle", "session-error")

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

    router_committed = any(show(worktree, base, rel) is not None for rel in ROUTER_RELS)
    router_local = any((worktree / rel).exists() for rel in ROUTER_RELS)

    if not router_local:
        findings.append(GateFinding(
            "router plugin", MISSING,
            f"no {ROUTER_RELS[0]} on disk in this worktree, so NO gate can fire here "
            f"however well the gate is written"
            + (
                f". It is committed on {base}, so this worktree predates that commit "
                f"- rebase or merge {base} into it."
                if router_committed else
                f", and it is not committed on {base} either. Run `orch init` and "
                f"commit the result."
            ),
        ))
    else:
        version, path = router_contract(worktree)
        if version is not None and version < MIN_ROUTER_CONTRACT:
            findings.append(GateFinding(
                "router contract", MISSING,
                f"{path} is contract {version}, and {MIN_ROUTER_CONTRACT} is required. "
                f"It does not report that a gate has started, so a slow gate is read as "
                f"an agent nothing checked. Run `orch init --force` and commit it",
            ))

    if router_local and not router_committed:
        findings.append(GateFinding(
            "router plugin", UNCOMMITTED,
            f"{ROUTER_RELS[0]} is on disk here but not committed on {base}, so gates "
            f"work in this worktree and will silently not fire in any worktree "
            f"dispatched from {base}",
        ))

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
                f"router will report no-gate-configured and nothing will verify "
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
            f"not committed on {base}, so the router falls back to defaults and any "
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

        env = {
            **os.environ,
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


def uncommitted_paths(worktree: Path, base: str | None = None) -> list[str]:
    base = base or base_branch(worktree)
    code, out = _git(worktree, "status", "--porcelain", "--", ORCH_DIR)
    if code != 0:
        return []
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            p = line[3:].strip()
            if "/.state/" not in p:
                paths.append(p)
    return paths
