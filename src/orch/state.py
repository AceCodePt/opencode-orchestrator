from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import observe

SESSION_BUSY = "busy"
SESSION_IDLE = "idle"
SESSION_BLOCKED = "blocked"
SESSION_INTERRUPTED = "interrupted"
SESSION_NONE = "none"

TODO = "todo"
RUNNING = "running"
NEEDS_REVIEW = "needs-review"
MERGE_READY = "ready"
NOOP = "noop"
ARCHIVED = "archived"

MAX_DEPTH = 6
PRUNE = (".git", "*.git", "node_modules", ".venv", ".local", ".cache",
         ".rustup", ".nvm", "Library", "AppData", ".stack")

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


# Where orch's own dev/e2e projects live; supervised by default so the daemon
# settles them (verifies, merges, archives) without needing ORCH_ROOTS.
DEV_ROOT = "/tmp/opencode"


def default_roots() -> list[str]:
    env = os.environ.get("ORCH_ROOTS")
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return [str(Path.home()), "/mnt", DEV_ROOT]


# --- project trust ----------------------------------------------------------
#
# The daemon acts (verifies, merges, dispatches, resolves, schedules) only on
# projects the machine owner has approved. Approval is a machine-level fact in
# ~/.local/state/orch/trusted.json, keyed by the repo's git common dir - never
# part of any project, so a repo you merely clone cannot approve itself.

def _trust_path() -> Path:
    from . import server
    return server.state_dir() / "trusted.json"


def _read_trusted() -> dict:
    try:
        raw = json.loads(_trust_path().read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_trusted(data: dict) -> None:
    p = _trust_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def trusted_projects() -> set[str]:
    return {str(k) for k, v in (_read_trusted().get("projects") or {}).items() if v}


def is_trusted(common: Path | str) -> bool:
    return str(common) in trusted_projects()


def trust_project(common: Path | str) -> bool:
    data = _read_trusted()
    projects = data.setdefault("projects", {})
    projects[str(common)] = {"trustedAt": int(time.time())}
    _write_trusted(data)
    return True


def untrust_project(common: Path | str) -> bool:
    data = _read_trusted()
    projects = data.get("projects") or {}
    if str(common) not in projects:
        return False
    projects.pop(str(common), None)
    _write_trusted(data)
    return True


def _find_orch_dirs(root: str, maxdepth: int = MAX_DEPTH) -> list[Path]:
    if not os.path.isdir(root):
        return []
    lines: list[str] = []

    fd_cmd = ["fd", "-H", "-t", "d", "-d", str(maxdepth), "-g"]
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


def committed_config(common: Path | str, base: str) -> dict:
    """The project's .orchestration/config.json as committed on the base
    branch - the only copy that is authoritative (the verification hook says so: an
    uncommitted or diverged config.json is flagged, and the committed values
    are the ones in force). Falls back to the base worktree's working copy and
    then the discovered dir, in that order, for the case where the file was
    never committed at all."""
    rc, out = _git(common, "show", f"{base}:{ORCH_DIR}/config.json")
    if rc == 0 and out:
        try:
            cfg = json.loads(out)
            if isinstance(cfg, dict):
                return cfg
        except ValueError:
            pass
    wt = _base_worktree(Path(common), base)
    if wt is not None and (wt / ORCH_DIR / "config.json").exists():
        return _read_config(wt / ORCH_DIR)
    return {}


def base_from_config(orch_dir: Path | None) -> str:
    b = _read_config(orch_dir).get("baseBranch")
    return str(b) if b else "main"


@dataclass
class SchedulerPolicy:
    enabled: bool = False
    max_concurrent: int = 1
    start: Path | None = None


def scheduler_from_config(cfg: dict) -> SchedulerPolicy:
    raw = (cfg or {}).get("scheduler") or {}
    try:
        max_conc = int(raw.get("maxConcurrent", 1))
    except (TypeError, ValueError):
        max_conc = 1
    return SchedulerPolicy(
        enabled=bool(raw.get("enabled", False)),
        max_concurrent=max_conc if max_conc > 0 else 0,
    )


def write_scheduler_policy(
    common: Path | str,
    base: str,
    enabled: bool,
    max_concurrent: int | None,
) -> tuple[bool, str]:
    """Edit the scheduler block in the base worktree's .orchestration/config.json
    (working copy, for a human to review and commit). Returns (ok, message)."""
    wt = _base_worktree(Path(common), base)
    if wt is None:
        return False, f"no worktree on {base}; cannot edit the project config"
    cfg_path = wt / ORCH_DIR / "config.json"
    if not cfg_path.exists():
        return False, f"no {cfg_path} to edit"
    cfg = _read_config(cfg_path.parent)
    sched = cfg.setdefault("scheduler", {})
    if not isinstance(sched, dict):
        sched = cfg["scheduler"] = {}
    if enabled:
        sched["enabled"] = True
        if max_concurrent is not None:
            sched["maxConcurrent"] = int(max_concurrent)
    else:
        sched["enabled"] = False
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    return True, (
        f"wrote scheduler {json.dumps(sched)} to {cfg_path}; review and commit "
        f"it on {base} - only the committed copy applies"
    )


def machine_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "orch" / "daemon.json"


def machine_budget() -> int | None:
    """The machine-wide cap on concurrently scheduled task agents, from
    ~/.config/orch/daemon.json {"scheduler": {"maxConcurrent": N}}. None means
    no machine-wide budget - only per-project scheduler.maxConcurrent applies."""
    try:
        raw = json.loads(machine_config_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = (raw.get("scheduler") or {}).get("maxConcurrent")
    if value is None:
        return None
    try:
        max_conc = int(value)
    except (TypeError, ValueError):
        return None
    return max_conc if max_conc > 0 else 0


def machine_settle_budget() -> int | None:
    """The machine-wide settle budget, in seconds - how long an idle task
    session must sit before the daemon verifies/sweeps it - from
    ~/.config/orch/daemon.json {"settleBudget": N}. Must be greater than 1
    (the daemon wakes every second, so a 1s budget would act on the very next
    tick); a value of 1 or less is ignored. None means the code default
    applies. Re-read every pass, so an edit applies on the next cycle with no
    daemon restart."""
    try:
        raw = json.loads(machine_config_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("settleBudget")
    if value is None:
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return None
    return budget if budget > 1 else None


def set_machine_budget(max_concurrent: int | None) -> dict:
    """Write the machine-wide budget. None removes the cap (unlimited)."""
    p = machine_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict):
            cfg = raw
    except (OSError, ValueError):
        cfg = {}
    if max_concurrent is None:
        sched = cfg.get("scheduler")
        if isinstance(sched, dict):
            sched.pop("maxConcurrent", None)
            if not sched:
                cfg.pop("scheduler", None)
    else:
        sched = cfg.setdefault("scheduler", {})
        if not isinstance(sched, dict):
            sched = cfg["scheduler"] = {}
        sched["maxConcurrent"] = max_concurrent
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


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
    """The task names under <rel>/ on <base>: tasks/<name>/task.md and the
    archived generations archive/<name>/task.md and archive/<name>/<ts>/task.md
    (the timestamped sub-path a re-used slug is archived to). The name is always
    the first path segment after <rel> - the timestamp is a generation
    discriminator, never part of the slug."""
    rc, out = _git(common, "ls-tree", "-r", "--name-only", base, "--", rel)
    if rc != 0:
        return set()
    names: set[str] = set()
    for line in out.splitlines():
        parts = line.strip().split("/")
        if len(parts) == 3 and parts[0] == rel and parts[2] == "task.md":
            names.add(parts[1])
        elif len(parts) == 4 and parts[0] == rel and parts[3] == "task.md":
            names.add(parts[1])
    return names


def archive_dest(base_wt: Path, base: str, name: str) -> str:
    """The archive path for <name>, relative to the base worktree. A task keeps
    the canonical archive/<name>/task.md unless an earlier generation of the
    slug already occupies it (committed on <base> or in the working tree) - a
    slug is freely reusable, so the colliding generation is archived to
    archive/<name>/<utc-ts>/task.md, retried with -1, -2, ... on a same-second
    collision. The slug stays the resolution key either way."""
    def occupied(rel: str) -> bool:
        if (base_wt / rel).exists():
            return True
        rc, _ = _git(base_wt, "show", f"{base}:{rel}")
        return rc == 0

    plain = f"{ARCHIVE_REL}/{name}/task.md"
    if not occupied(plain):
        return plain
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for i in range(1000):
        sub = ts if i == 0 else f"{ts}-{i}"
        cand = f"{ARCHIVE_REL}/{name}/{sub}/task.md"
        if not occupied(cand):
            return cand
    raise ValueError(f"archive/{name}/ is fully occupied by archived generations")


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


def _dependencies_from(meta: dict) -> list[str]:
    """The `dependencies:` front-matter value as a list of task slugs. Accepts a
    JSON list (`[a, b]`), a comma/space separated string (`a, b`), or nothing."""
    raw = str(meta.get("dependencies", "")).strip()
    if not raw or raw.lower() in ("none", "[]", "null"):
        return []
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        if not inner.strip():
            return []
        items = inner.split(",")
    else:
        items = raw.split(",")
    out: list[str] = []
    for item in items:
        slug = item.strip().strip('"').strip("'")
        if slug:
            out.append(slug)
    return out


def _flag_from(meta: dict, key: str, fallback: str | None = None) -> bool:
    """A boolean front-matter flag. Default false; `hitl` is accepted as a
    fallback for `wait_human_start` during the transition."""
    raw = meta.get(key)
    if raw is None and fallback is not None:
        raw = meta.get(fallback)
    if raw is None:
        return False
    return str(raw).strip().lower() not in ("false", "no", "0")


def _wait_human_start_from(meta: dict) -> bool:
    return _flag_from(meta, "wait_human_start", "hitl")


def _wait_human_merge_from(meta: dict) -> bool:
    return _flag_from(meta, "wait_human_merge")


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
        # The cwd is set by the kernel and is the same worktree we spawned the
        # agent in; a substring match on argv is spoofable and ambiguous.
        if _cwd(pid) == wt:
            return pid
    return None


def resolution_alive(port: int | None, base_worktree: str | None,
                     task: str) -> bool:
    """True if an agent is currently resolving <task>'s merge conflict in the
    base worktree. The resolver is a server session titled `<task>-merge-conflict`
    bound to the base worktree (started via the prompt API, not an `opencode run`
    subprocess), so it is detected by session title + directory, never by a
    `run` process — a `run`-process lookup is blind to it."""
    if not port or not base_worktree:
        return False
    marker = f"{task}-merge-conflict"
    try:
        for s in observe.harness.sessions(port, os.path.realpath(base_worktree)):
            if marker in str(s.get("title") or ""):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _cwd(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _session_of(tf: observe.TaskFact) -> str:
    if tf.state == observe.INTERRUPTED:
        return SESSION_INTERRUPTED
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
    wait_human_start: bool = False
    wait_human_merge: bool = False
    dependencies: list[str] = field(default_factory=list)
    conflicted: bool = False
    interrupted: bool = False
    orphaned: list[dict] = field(default_factory=list)
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
        if self.interrupted and (self.dirty or self.diff):
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
    cap = int(committed_config(common, base).get("verify", {}).get("retryCap") or 3)

    names = sorted(set(declared) | set(branches) | set(archived))
    merging = _base_merging(common, base)
    out: list[TaskState] = []
    for name in names:
        claimed = name in branches
        wt = wt_by_branch.get(name) if claimed else None
        # A re-created slug is the live task even when an earlier generation of
        # the same slug is archived: the open declaration wins over history, so
        # the task reads as running/todo rather than ARCHIVED and still blocks
        # its dependents until this generation settles.
        archived_now = name in archived and name not in declared
        st = TaskState(
            project=str(common), name=name, base=base, retry_cap=cap,
            declared=name in declared, archived=archived_now,
            claimed=claimed, branch=name if claimed else None,
            worktree=str(wt) if wt else None,
        )
        if name in declared:
            fm = _front_matter(common, base, name)
            st.wait_human_start = _wait_human_start_from(fm)
            st.wait_human_merge = _wait_human_merge_from(fm)
            st.dependencies = _dependencies_from(fm)
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
                    st.interrupted = tf.interrupted
                    st.orphaned = tf.orphaned
                    if tf.interrupted:
                        st.notes.append(
                            "interrupted: the task's turn was aborted (a user stop, "
                            "or the turn died); its work is held for you to verify"
                        )
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
    # Untracked files show up in `status` as just "?? name", so their content is
    # invisible to the diff. A task worktree's deliverable is usually brand new,
    # so an agent fixing it must register as a change or the retry loop mistakes
    # the fix for a stall and never re-verifies.
    rc, files = _git(worktree, "ls-files", "-o", "--exclude-standard")
    for name in sorted(x for x in files.splitlines() if x.strip()):
        try:
            h.update(name.encode())
            h.update(Path(worktree, name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()
