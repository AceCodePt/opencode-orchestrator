from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import observe

SESSION_BUSY = "busy"
SESSION_IDLE = "idle"
SESSION_BLOCKED = "blocked"
SESSION_NONE = "none"

TODO = "todo"
RUNNING = "running"
NEEDS_REVIEW = "needs-review"
MERGE_READY = "ready"
NOOP = "noop"
ARCHIVED = "archived"

MAX_DEPTH = 6
PRUNE = (".git", "node_modules", ".venv", ".local", ".cache", ".rustup",
         ".nvm", "Library", "AppData", ".stack")

ORCH_DIR = ".orchestration"
TASKS_REL = "tasks"
ARCHIVE_REL = "archive"


def _git(cwd: Path | str, *args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _cmd(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def default_roots() -> list[str]:
    env = os.environ.get("ORCH_ROOTS")
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return [str(Path.home()), "/mnt"]


def _find_orch_dirs(root: str, maxdepth: int = MAX_DEPTH) -> list[Path]:
    if not os.path.isdir(root):
        return []
    lines: list[str] = []

    fd_cmd = ["fd", "-H", "-t", "d", "-d", str(maxdepth)]
    for n in PRUNE:
        fd_cmd += ["-E", n]
    fd_cmd += [ORCH_DIR, root]
    code, out = _cmd(fd_cmd)
    if code == 0 and out:
        lines = out.splitlines()
    else:
        find_cmd = ["find", root, "-maxdepth", str(maxdepth), "-type", "d"]
        for n in PRUNE:
            find_cmd += ["-name", n, "-prune"]
        find_cmd += ["-o", "-type", "d", "-name", ORCH_DIR, "-print"]
        code, out = _cmd(find_cmd)
        if code == 0:
            lines = out.splitlines()
    return [Path(p) for p in lines if p.strip()]


def discover_projects(roots: list[str] | None = None) -> dict[str, Path]:
    roots = list(roots) if roots is not None else default_roots()
    by_common: dict[str, Path] = {}
    for root in roots:
        for orch in _find_orch_dirs(root):
            rc, common = _git(orch, "rev-parse", "--path-format=absolute",
                              "--git-common-dir")
            if rc != 0 or not common:
                continue
            by_common.setdefault(common, orch)
    return by_common


def _read_config(orch_dir: Path | None) -> dict:
    if orch_dir is None:
        return {}
    try:
        return json.loads((orch_dir / "config.json").read_text())
    except (OSError, ValueError, AttributeError):
        return {}


def base_from_config(orch_dir: Path | None) -> str:
    b = _read_config(orch_dir).get("baseBranch")
    return str(b) if b else "main"


@dataclass
class SchedulerPolicy:
    enabled: bool = False
    max_concurrent: int = 1
    start: Path | None = None


def scheduler_from_config(orch_dir: Path | None) -> SchedulerPolicy:
    raw = _read_config(orch_dir).get("scheduler") or {}
    try:
        max_conc = int(raw.get("maxConcurrent", 1))
    except (TypeError, ValueError):
        max_conc = 1
    return SchedulerPolicy(
        enabled=bool(raw.get("enabled", False)),
        max_concurrent=max_conc if max_conc > 0 else 0,
    )


@dataclass
class Project:
    common: Path
    orch_dir: Path | None = None
    base: str = "main"

    @classmethod
    def from_dir(cls, start: Path) -> "Project | None":
        from . import dispatch

        top, common, err = dispatch.project_of(start)
        if err or top is None or common is None:
            return None
        orch = top / ORCH_DIR
        base = base_from_config(orch if orch.is_dir() else None)
        return cls(Path(common),
                   orch if orch.is_dir() else None, base)

    @classmethod
    def from_common(cls, common: Path | str) -> "Project | None":
        common = Path(common)
        orch = None
        for path, _branch in _worktrees(common):
            if (path / ORCH_DIR).is_dir():
                orch = path / ORCH_DIR
                break
        base = base_from_config(orch)
        return cls(common, orch, base)

    @property
    def base_worktree(self) -> Path | None:
        return _base_worktree(self.common, self.base)

    def scan(self, port: int | None = None) -> list["TaskState"]:
        return scan_project(self, port)


def _worktrees(common: Path) -> list[tuple[Path, str | None]]:
    rc, out = _git(common, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    found: list[tuple[Path, str | None]] = []
    path: str | None = None
    branch: str | None = None
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch "):
            b = line[len("branch "):]
            branch = b.removeprefix("refs/heads/")
        elif not line.strip():
            if path:
                found.append((Path(path), branch))
            path, branch = None, None
    return found


def _base_worktree(common: Path, base: str) -> Path | None:
    for path, branch in _worktrees(common):
        if branch == base and path.is_dir():
            return path
    return None


def _base_merging(common: Path, base: str) -> str | None:
    """The sha being merged into base, if the base worktree is mid-merge (a
    conflict was left for a human). None otherwise."""
    bwt = _base_worktree(common, base)
    if bwt is None:
        return None
    rc, out = _git(bwt, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if rc != 0 or not out:
        return None
    return out.strip()


def _names_in(common: Path, base: str, rel: str) -> set[str]:
    rc, out = _git(common, "ls-tree", "-r", "--name-only", base, "--", rel)
    if rc != 0:
        return set()
    names: set[str] = set()
    for line in out.splitlines():
        parts = line.strip().split("/")
        if len(parts) == 3 and parts[0] == rel and parts[2] == "task.md":
            names.add(parts[1])
    return names


def _front_matter(common: Path, base: str, name: str) -> dict:
    """The `--- … ---` block at the top of the committed tasks/<name>/task.md."""
    rc, out = _git(common, "show", f"{base}:{TASKS_REL}/{name}/task.md")
    if rc != 0 or not out.startswith("---"):
        return {}
    body: list[str] = []
    for line in out.splitlines()[1:]:
        if line.strip() == "---":
            break
        body.append(line)
    meta: dict = {}
    for line in body:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta


def _hitl_from(meta: dict) -> bool:
    return not str(meta.get("hitl", "")).strip().lower() in ("false", "no", "0")


def _branches(common: Path) -> set[str]:
    rc, out = _git(common, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads")
    if rc != 0:
        return set()
    return {b for b in out.splitlines() if b and "/" not in b}


def _dirty(worktree: Path) -> bool:
    rc, out = _git(worktree, "status", "--porcelain", "--untracked-files=normal")
    return rc == 0 and bool(out)


def _diff(common: Path, base: str, branch: str) -> bool:
    rc, out = _git(common, "rev-list", "--count", f"{base}..{branch}")
    return rc == 0 and out.strip() not in ("", "0")


def _pids() -> list[int]:
    _rc, out = _cmd(["pgrep", "-x", "opencode"])
    return [int(x) for x in out.split() if x.isdigit()]


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _detached(pid: int) -> bool:
    """True if the process has no controlling terminal — how orch starts an
    agent (start_new_session, stdin from /dev/null), and not how a human runs
    one in a shell."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        fields = raw.rsplit(")", 1)[1].split()
        if len(fields) > 4 and fields[4].strip() == "0":
            return True
    except (OSError, IndexError, ValueError):
        pass
    try:
        if os.readlink(f"/proc/{pid}/fd/0") == "/dev/null":
            return True
    except OSError:
        pass
    return False


def dispatch_pid(worktree: str, orchestrated: bool = False) -> int | None:
    wt = os.path.realpath(worktree)
    for pid in _pids():
        cmd = _cmdline(pid)
        if " run " not in cmd:
            continue
        if orchestrated and not _detached(pid):
            continue
        if wt in cmd:
            return pid
    return None


def resolution_pid(base_worktree: str, task: str) -> int | None:
    """The pid of the agent resolving <task>'s merge conflict in the base
    worktree, if one is running. The base worktree is shared, so the match is
    on the worktree path AND the task-specific title, not just the path."""
    wt = os.path.realpath(base_worktree)
    marker = f"{task}-merge-conflict"
    for pid in _pids():
        cmd = _cmdline(pid)
        if " run " not in cmd:
            continue
        if not _detached(pid):
            continue
        if wt in cmd and marker in cmd:
            return pid
    return None


def _session_of(tf: observe.TaskFact) -> str:
    if tf.state in (observe.BLOCKED_PERMISSION, observe.BLOCKED_QUESTION):
        return SESSION_BLOCKED
    if tf.state == observe.BUSY:
        return SESSION_BUSY
    return SESSION_IDLE


@dataclass
class TaskState:
    project: str
    name: str
    base: str
    retry_cap: int = 3
    declared: bool = False
    archived: bool = False
    claimed: bool = False
    branch: str | None = None
    worktree: str | None = None
    dirty: bool = False
    diff: bool = False
    session: str = SESSION_NONE
    session_ids: list[str] = field(default_factory=list)
    blocked: list[observe.Blocked] = field(default_factory=list)
    dispatch_alive: bool = False
    dispatch_orchestrated: bool = False
    hitl: bool = False
    conflicted: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def settled(self) -> bool:
        return self.session in (SESSION_IDLE, SESSION_NONE)

    @property
    def status(self) -> str:
        if self.archived:
            return ARCHIVED
        if not self.claimed:
            return TODO
        if self.session in (SESSION_BUSY, SESSION_BLOCKED):
            return RUNNING
        if not self.worktree or not os.path.isdir(self.worktree):
            return NEEDS_REVIEW
        if self.conflicted:
            return NEEDS_REVIEW
        if self.dirty:
            return NEEDS_REVIEW
        if not self.diff:
            return NOOP
        return MERGE_READY


def scan_project(proj: Project, port: int | None = None) -> list[TaskState]:
    common = proj.common
    base = proj.base
    declared = _names_in(common, base, TASKS_REL)
    archived = _names_in(common, base, ARCHIVE_REL)
    branches = {b for b in _branches(common) if b != base}
    wt_by_branch = {b: p for p, b in _worktrees(common)}
    cap = int(_read_config(proj.orch_dir).get("gate", {}).get("retryCap") or 3)

    names = sorted(set(declared) | set(branches) | set(archived))
    merging = _base_merging(common, base)
    out: list[TaskState] = []
    for name in names:
        claimed = name in branches
        wt = wt_by_branch.get(name) if claimed else None
        st = TaskState(
            project=str(common), name=name, base=base, retry_cap=cap,
            declared=name in declared, archived=name in archived,
            claimed=claimed, branch=name if claimed else None,
            worktree=str(wt) if wt else None,
        )
        if name in declared:
            st.hitl = _hitl_from(_front_matter(common, base, name))
        if merging and claimed and name in branches:
            _rc, sha = _git(common, "rev-parse", name)
            if _rc == 0 and sha and sha.strip() == merging:
                st.conflicted = True
                st.notes.append(
                    "merge-conflict: the merge into base is in progress. Resolve "
                    "it in the base worktree (fix the files, git add, git commit). "
                    "Once merged, the daemon archives the task."
                )
        if wt is not None and wt.is_dir():
            st.dirty = _dirty(wt)
            st.diff = _diff(common, base, name)
            if port:
                tf = observe.look_task(port, str(wt))
                if tf is not None:
                    st.session = _session_of(tf)
                    st.session_ids = tf.session_ids
                    st.blocked = tf.blocked
            st.dispatch_alive = dispatch_pid(str(wt)) is not None
            st.dispatch_orchestrated = dispatch_pid(str(wt),
                                                     orchestrated=True) is not None
        elif claimed:
            st.notes.append("worktree missing")
        out.append(st)
    return out


def fingerprint(worktree: Path | str) -> str:
    h = hashlib.sha256()
    rc, out = _git(worktree, "status", "--porcelain", "--untracked-files=normal")
    h.update(out.encode())
    rc, head = _git(worktree, "rev-parse", "HEAD")
    h.update(head.encode())
    return h.hexdigest()
