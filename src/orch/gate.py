"""Whether a project's gates can be trusted.

The Python mirror of what `plugin/orch-router.ts` enforces at runtime: the gate that
runs is the one **committed on the base branch**, never the working-tree copy.

The reason is not tidiness. A dispatched agent was observed hitting the fail-closed
scaffold, being handed the failure as instructions, rewriting
`.orchestration/hooks/session-idle` into a gate tailored to its own work, and then
being reported as `passed`. Every component behaved as designed - which is the
problem, because the agent authored the criterion it was judged by. That particular
rewrite was honest; nothing structurally stopped it from being `exit 0`.

`config.json` is the same hole by a quieter route: `gate.exclude: ["**"]` means no
change ever looks worth checking, and a large `retryCap` turns a deliberately
bounded loop into a billable one. So both come from the committed ref.

This module only *reports*. `doctor` surfaces it, `task start` refuses on it. That
split matters because the failure is silent: an uncommitted gate degrades to
`no-gate-configured`, which reads like a configuration note rather than "nothing
verified this work".
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

ORCH_DIR = ".orchestration"
HOOKS_REL = f"{ORCH_DIR}/hooks"
CONFIG_REL = f"{ORCH_DIR}/config.json"

# Both spellings load; opencode accepts either. Verified by installing the same
# plugin under each and watching both import.
ROUTER_RELS = (
    ".opencode/plugins/orch-router.ts",
    ".opencode/plugin/orch-router.ts",
)

# Only these are gates. Anything else in hooks/ is a lifecycle script that the
# dispatcher runs directly, and its authority comes from the dispatcher, not the
# router.
GATE_EVENTS = ("session-idle", "session-error")

OK = "ok"
UNCOMMITTED = "uncommitted"
DIVERGED = "diverged"
MISSING = "missing"


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
    """Contents of `rel` at `ref`, or None when it is not committed there."""
    code, out = _git(worktree, "show", f"{ref}:{rel}")
    return out if code == 0 else None


def base_branch(worktree: Path) -> str:
    """Read `baseBranch` from the working-tree config.

    This is the one setting that must come from the working tree, because it names
    the ref everything else is read from. Chicken and egg, so it is deliberately the
    only trusted-from-disk value - and it names a ref rather than granting anything.
    """
    import json
    try:
        cfg = json.loads((worktree / CONFIG_REL).read_text())
        b = cfg.get("baseBranch")
        return str(b) if b else "main"
    except (OSError, ValueError, AttributeError):
        return "main"


@dataclass
class GateFinding:
    name: str          # e.g. "session-idle"
    state: str         # OK | UNCOMMITTED | DIVERGED | MISSING
    detail: str

    @property
    def trustworthy(self) -> bool:
        """Whether a verdict from this gate means anything.

        DIVERGED still counts: the committed version is what runs, so its verdict is
        sound. It is flagged because an agent editing its own gate is worth a human's
        attention, not because the result is invalid. Conflating the two would train
        people to ignore the flag.
        """
        return self.state in (OK, DIVERGED)


def inspect(worktree: Path, base: str | None = None) -> list[GateFinding]:
    base = base or base_branch(worktree)
    findings: list[GateFinding] = []

    # The router first, because without it nothing else here matters.
    #
    # Caught by dispatching a task whose gate was perfectly committed into a
    # worktree created before the plugin was committed. The agent did the work, the
    # gate was valid, `orch gate` reported ok - and no gate ran, because the
    # worktree had no router to fire it. Silence in the one place it looks like
    # success.
    # Two separate requirements, and conflating them hid a bug:
    #
    # - The router loads from the worktree's files **on disk**. That is what makes a
    #   gate able to fire *here*.
    # - It must also be committed on the base branch, because dispatched worktrees
    #   are created from that branch and inherit only what is committed.
    #
    # A worktree created before the plugin was committed satisfies the second and
    # fails the first: the gate is valid, `orch gate` said ok, and nothing fired.
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
    elif not router_committed:
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
            continue  # this project simply does not gate this event
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


def blocking(findings: list[GateFinding]) -> list[GateFinding]:
    """Findings that should stop a dispatch rather than merely be reported."""
    return [f for f in findings if not f.trustworthy]


def uncommitted_paths(worktree: Path, base: str | None = None) -> list[str]:
    """Orchestration files that differ from the base branch, for a commit prompt."""
    base = base or base_branch(worktree)
    code, out = _git(worktree, "status", "--porcelain", "--", ORCH_DIR)
    if code != 0:
        return []
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            p = line[3:].strip()
            if "/.state/" not in p:  # runtime state is not intent, and is gitignored
                paths.append(p)
    return paths
