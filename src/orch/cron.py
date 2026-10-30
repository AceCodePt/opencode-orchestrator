"""Cron-scheduled opencode sessions, declared in `.orchestration/config.json`.

The daemon already wakes every second and reads each project's **committed**
config. This module turns a `cron` block into due-session decisions:

    "cron": {
      "jobs": [
        {"name": "qa", "schedule": "0 9 * * *", "prompt": "Run the QA suite ..."},
        {"name": "security", "schedule": "0 * * * *",
         "promptFile": ".orchestration/prompts/security.md"}
      ]
    }

Only `name`, `schedule` and `prompt` (or `promptFile`) are required. `model` and
`agent` are optional and ride through to opencode's session-create body.
Schedules are standard five-field cron expressions - minute hour day-of-month
month day-of-week - evaluated in the daemon's local timezone.

A job is fired at most once per due slot. The session title carries the slot
(`orch:cron:<name>:<epoch>`), so a daemon restart, a duplicated tick, or a lost
state file cannot replay it: the server's session list is the record.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dispatch, state, verify

TITLE_PREFIX = "orch:cron:"

# A missed window is searched backwards at most this far. It bounds the work a
# long daemon outage can put on one tick; a job that was due within the last
# week still catches up, an old occurrence does not replay.
HORIZON_DAYS = 8

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class CronError(ValueError):
    """A schedule string that cannot be parsed."""


@dataclass(frozen=True)
class CronSpec:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    day_any: bool
    weekday_any: bool

    def matches(self, epoch: int) -> bool:
        """True when the local time at `epoch` falls on this schedule. Standard
        cron day semantics: when both day-of-month and day-of-week are
        restricted, either matching is enough; when one is `*`, only the other
        constrains the match."""
        tm = time.localtime(int(epoch))
        if (tm.tm_min not in self.minute
                or tm.tm_hour not in self.hour
                or tm.tm_mon not in self.month):
            return False
        dom = tm.tm_mday in self.day
        # tm_wday is Monday=0..Sunday=6; cron is Sunday=0..Saturday=6.
        dow = ((tm.tm_wday + 1) % 7) in self.weekday
        if self.day_any and self.weekday_any:
            return True
        if self.day_any:
            return dow
        if self.weekday_any:
            return dom
        return dom or dow


def _number(text: str, names: dict[str, int] | None) -> int:
    raw = text.strip().lower()
    if names is not None and raw in names:
        return names[raw]
    if not raw.lstrip("+").isdigit():
        raise CronError(f"{text!r} is not a number")
    return int(raw)


def _field(text: str, lo: int, hi: int,
           names: dict[str, int] | None = None) -> frozenset[int]:
    """Expand one cron field: `*`, `n`, `a-b`, `*/n`, `a-b/n`, comma lists, and
    month/weekday names. A bare `n/step` starts at n and runs to the field's
    top, which is what cron implementations commonly do."""
    if not text.strip():
        raise CronError("empty field")
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError("empty list item")
        step = 1
        base = part
        if "/" in part:
            base, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) <= 0:
                raise CronError(f"bad step in {part!r}")
            step = int(raw_step)
        if base.strip() == "*":
            start, end = lo, hi
        elif "-" in base:
            left, _, right = base.partition("-")
            start = _number(left, names)
            end = _number(right, names)
            if start > end:
                raise CronError(f"reversed range in {part!r}")
        else:
            start = _number(base, names)
            end = hi if step > 1 else start
        if start < lo or end > hi:
            raise CronError(f"{part!r} is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    # Cron accepts both 0 and 7 for Sunday.
    return frozenset(0 if v == 7 else v for v in values)


def parse(expr: str) -> CronSpec:
    """Parse a five-field cron expression. Raises CronError."""
    parts = str(expr).split()
    if len(parts) != 5:
        raise CronError(
            "a cron schedule has five fields: minute hour day month weekday")
    return CronSpec(
        minute=_field(parts[0], 0, 59),
        hour=_field(parts[1], 0, 23),
        day=_field(parts[2], 1, 31),
        month=_field(parts[3], 1, 12, _MONTHS),
        weekday=_field(parts[4], 0, 7, _WEEKDAYS),
        day_any=parts[2].strip() == "*",
        weekday_any=parts[4].strip() == "*",
    )


def due_slot(spec: CronSpec, prev: int, now: int,
             horizon_days: int = HORIZON_DAYS) -> int | None:
    """The latest scheduled minute in `(prev, now]`, or None.

    `prev` is the last time the daemon *checked* the job, not the last time it
    fired: a tick that lands a little late still sees the occurrence it
    crossed. Minutes at or before `prev` are excluded, so a job fires once per
    slot even though the daemon ticks every second."""
    end = int(now // 60) * 60
    start = max(int(prev // 60) * 60, end - horizon_days * 86400)
    stamp = end
    while stamp > start:
        if spec.matches(stamp):
            return stamp
        stamp -= 60
    return None


@dataclass(frozen=True)
class CronJob:
    project: str
    name: str
    schedule: str
    spec: CronSpec
    prompt: str
    model: Any = None
    agent: str | None = None
    overlap: str = "skip"
    worktree: str = ""

    @property
    def title_prefix(self) -> str:
        return f"{TITLE_PREFIX}{self.name}:"


def _prompt(entry: dict, base_wt: Path | None, base: str
            ) -> tuple[str | None, str | None]:
    inline = entry.get("prompt")
    if isinstance(inline, str) and inline.strip():
        return inline, None
    rel = entry.get("promptFile") or entry.get("prompt_file")
    if not isinstance(rel, str) or not rel.strip():
        return None, "has no prompt (or promptFile)"
    rel = rel.strip()
    if base_wt is None:
        return None, f"promptFile {rel!r} needs a worktree on {base}"
    # Only the committed copy is authoritative, the same rule as config.json
    # and the verification hook; an uncommitted prompt file is still readable
    # from the working tree as a convenience.
    committed = verify.show(base_wt, base, rel)
    if committed is not None and committed.strip():
        return committed, None
    path = Path(base_wt) / rel
    if path.is_file():
        try:
            return path.read_text(), None
        except OSError as e:
            return None, f"cannot read promptFile {rel!r}: {e}"
    return None, f"promptFile {rel!r} is not committed on {base}"


def jobs_from_config(cfg: dict | None, project: str,
                     base_wt: Path | None, base: str
                     ) -> tuple[list[CronJob], list[str]]:
    """Read the `cron` block into jobs plus human-readable config errors.

    `enabled: false` disables the whole block (or one entry). Invalid entries
    are skipped and reported, never fired: a bad schedule must not take the
    daemon's reconcile loop down."""
    raw = (cfg or {}).get("cron")
    if not isinstance(raw, dict) or raw.get("enabled") is False:
        return [], []
    entries = raw.get("jobs")
    if entries is None:
        entries = raw.get("sessions")
    if entries is None:
        return [], []
    if not isinstance(entries, list):
        return [], [f"{project}: cron.jobs must be a list"]

    jobs: list[CronJob] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        where = f"{project}: cron.jobs[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} is not an object")
            continue
        if entry.get("enabled") is False:
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            errors.append(f"{where} has no name")
            continue
        where = f"{project}: cron job {name!r}"
        if name in seen:
            errors.append(f"{where} is declared twice")
            continue
        schedule = str(entry.get("schedule") or "").strip()
        if not schedule:
            errors.append(f"{where} has no schedule")
            continue
        try:
            spec = parse(schedule)
        except CronError as e:
            errors.append(f"{where}: {e}")
            continue
        prompt, problem = _prompt(entry, base_wt, base)
        if problem:
            errors.append(f"{where}: {problem}")
            continue
        model = entry.get("model")
        if not isinstance(model, (str, dict)):
            model = None
        agent = entry.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            agent = None
        overlap = str(entry.get("overlap") or "skip").strip().lower()
        if overlap not in ("skip", "allow"):
            overlap = "skip"
        seen.add(name)
        jobs.append(CronJob(
            project=project,
            name=name,
            schedule=schedule,
            spec=spec,
            prompt=prompt,
            model=model,
            agent=agent,
            overlap=overlap,
            worktree=str(base_wt) if base_wt is not None else "",
        ))
    return jobs, errors


# --- editing the committed config -------------------------------------------
#
# `orch cron add|update|remove` (and the machine-global custom tools) edit the
# `cron.jobs` block in the base worktree's `.orchestration/config.json`. The
# daemon only ever reads the committed copy, so a change is armed only when it
# is committed. When the file already had uncommitted edits we refuse to sweep
# them into an orch commit: the write still lands, but the commit is left to
# the human who made those edits.

CONFIG_REL = verify.CONFIG_REL


class CronToolError(ValueError):
    """A job edit that cannot be applied (bad field, unknown job, ...)."""


def _git(cwd: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _project(dir_: Path) -> tuple[Path, Path, str]:
    """The base worktree the config lives in, its common git dir, and the base
    branch. A session may call this from a task worktree; the edit always lands
    in the base worktree, because that is the copy the daemon reads."""
    if not dir_.is_dir():
        raise CronToolError(f"{dir_} is not a directory")
    top, common, err = dispatch.project_of(dir_)
    if err or top is None or common is None:
        raise CronToolError(err or "not inside a git worktree")
    if not (top / verify.ORCH_DIR).is_dir():
        raise CronToolError(
            f"{top} is not an orch project (no {verify.ORCH_DIR}/); run `orch init`")
    common = Path(common)
    base = verify.base_branch(top)
    base_wt = state._base_worktree(common, base)
    if base_wt is None:
        raise CronToolError(
            f"no worktree is checked out on the base branch {base!r}; check it "
            f"out and try again")
    return base_wt, common, base


def _load_config(base_wt: Path) -> dict:
    try:
        cfg = json.loads((base_wt / CONFIG_REL).read_text())
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _save_config(base_wt: Path, cfg: dict) -> None:
    path = base_wt / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)


def _entries(cfg: dict) -> tuple[dict, list]:
    """The cron block and its job list. `jobs` is the key; `sessions` is the
    short-lived old name and is migrated on the next write."""
    raw = cfg.get("cron")
    if not isinstance(raw, dict):
        raw = {}
    entries = raw.get("jobs")
    if entries is None:
        entries = raw.get("sessions")
    if not isinstance(entries, list):
        entries = []
    return raw, entries


def _find(entries: list, name: str) -> tuple[int, dict] | None:
    slug = dispatch.slug(name)
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if dispatch.slug(str(entry.get("name") or "")) == slug:
            return i, entry
    return None


def _dirty(base_wt: Path) -> bool:
    rc, out = _git(base_wt, "status", "--porcelain", "--", CONFIG_REL)
    return rc == 0 and bool(out.strip())


def _commit(base_wt: Path, message: str) -> tuple[str | None, str | None]:
    rc, out = _git(base_wt, "add", "--", CONFIG_REL)
    if rc != 0:
        return None, out[:300]
    rc, out = _git(
        base_wt, "-c", "user.name=orch", "-c", "user.email=orch@localhost",
        "commit", "-q", "-m", message, "--", CONFIG_REL,
    )
    if rc != 0:
        low = (out or "").lower()
        if "nothing to commit" in low or "no changes added" in low:
            return None, None
        return None, out[:300]
    _rc, sha = _git(base_wt, "rev-parse", "--short", "HEAD")
    return (sha.strip() or "HEAD"), None


def _validate_prompt_file(base_wt: Path, base: str, job: dict) -> None:
    rel = job.get("promptFile") or job.get("prompt_file")
    if not rel:
        return
    if verify.show(base_wt, base, str(rel)) is not None:
        return
    if (base_wt / str(rel)).is_file():
        return
    raise CronToolError(
        f"promptFile {rel!r} is not committed on {base} and not in the worktree")


def clean_job(args: dict, existing: dict | None = None) -> dict:
    """Validate and merge one job. `existing` makes every field optional: an
    update only changes what was passed, exactly like update_task."""
    out = dict(existing or {})

    if "name" in args or not out.get("name"):
        slug = dispatch.slug(str(args.get("name") or out.get("name") or ""))
        if not slug:
            raise CronToolError("a cron job needs a name")
        out["name"] = slug

    if "schedule" in args or not out.get("schedule"):
        schedule = str(args.get("schedule") or "").strip()
        if not schedule:
            raise CronToolError(
                "a cron job needs a schedule (minute hour day month weekday)")
        try:
            parse(schedule)
        except CronError as e:
            raise CronToolError(str(e)) from e
        out["schedule"] = schedule

    prompt = args.get("prompt")
    prompt_file = args.get("promptFile", args.get("prompt_file"))
    if "prompt" in args or "promptFile" in args or "prompt_file" in args:
        if isinstance(prompt, str) and prompt.strip():
            out["prompt"] = prompt
            out.pop("promptFile", None)
            out.pop("prompt_file", None)
        elif isinstance(prompt_file, str) and prompt_file.strip():
            out["promptFile"] = prompt_file.strip()
            out.pop("prompt", None)
            out.pop("prompt_file", None)
        else:
            raise CronToolError(
                "a cron job needs a prompt: pass inline prompt or promptFile")
    elif not (str(out.get("prompt") or "").strip()
              or str(out.get("promptFile") or out.get("prompt_file") or "").strip()):
        raise CronToolError(
            "a cron job needs a prompt: pass inline prompt or promptFile")

    if "model" in args:
        model = args.get("model")
        if model is None or (isinstance(model, str) and not model.strip()):
            out.pop("model", None)
        elif isinstance(model, (str, dict)):
            out["model"] = model.strip() if isinstance(model, str) else model
        else:
            raise CronToolError("model must be a string like provider/model")
    if "agent" in args:
        agent = args.get("agent")
        if agent is None or (isinstance(agent, str) and not agent.strip()):
            out.pop("agent", None)
        elif isinstance(agent, str):
            out["agent"] = agent.strip()
        else:
            raise CronToolError("agent must be a string")
    if "overlap" in args:
        overlap = str(args.get("overlap") or "skip").strip().lower()
        if overlap not in ("skip", "allow"):
            raise CronToolError("overlap must be 'skip' or 'allow'")
        out["overlap"] = overlap
    if "enabled" in args:
        if not isinstance(args["enabled"], bool):
            raise CronToolError("enabled must be a boolean")
        out["enabled"] = args["enabled"]
    return out


def _result(base_wt: Path, base: str, job: dict, sha: str | None,
            commit: bool, dirty: bool, verb: str) -> dict:
    if sha:
        message = (f"{verb} cron job {job['name']!r} and committed on {base} "
                   f"(sha {sha})")
    elif dirty:
        message = (f"{verb} cron job {job['name']!r} in {CONFIG_REL}, but the "
                   f"config already had uncommitted changes; review and commit "
                   f"it on {base}")
    elif not commit:
        message = (f"{verb} cron job {job['name']!r} in {CONFIG_REL}; "
                   f"uncommitted. Commit it on {base} to arm it.")
    else:
        message = f"{verb} cron job {job['name']!r} (nothing to commit)"
    return {
        "path": CONFIG_REL,
        "job": job,
        "message": message,
        "committed": bool(sha),
        "commitSha": sha,
    }


def _entry_valid(entry: dict, name: str) -> bool:
    """Whether this raw entry would fire if the block were enabled. Kept
    separate from jobs_from_config so a paused job still lists as valid."""
    if not name:
        return False
    try:
        parse(str(entry.get("schedule") or ""))
    except CronError:
        return False
    return bool(str(entry.get("prompt") or "").strip()
                or str(entry.get("promptFile")
                       or entry.get("prompt_file") or "").strip())


def list_jobs(dir_: Path) -> dict:
    """The project's cron jobs as written in the base worktree's config, with
    validation errors the daemon would report."""
    base_wt, common, base = _project(dir_)
    cfg = _load_config(base_wt)
    raw, entries = _entries(cfg)
    _jobs, errors = jobs_from_config(cfg, str(common), base_wt, base)
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = dispatch.slug(str(entry.get("name") or ""))
        prompt = entry.get("prompt")
        rows.append({
            "name": name or str(entry.get("name") or ""),
            "schedule": entry.get("schedule"),
            "agent": entry.get("agent"),
            "model": entry.get("model"),
            "promptFile": entry.get("promptFile") or entry.get("prompt_file"),
            "prompt": (str(prompt)[:200] if prompt else None),
            "overlap": entry.get("overlap", "skip"),
            "enabled": entry.get("enabled", True) is not False,
            "valid": _entry_valid(entry, name),
        })
    return {
        "path": CONFIG_REL,
        "enabled": raw.get("enabled", True) is not False,
        "jobs": rows,
        "errors": errors,
    }


def add_job(dir_: Path, args: dict, commit: bool = True) -> dict:
    base_wt, _common, base = _project(dir_)
    cfg = _load_config(base_wt)
    raw, entries = _entries(cfg)
    job = clean_job(args)
    if _find(entries, job["name"]) is not None:
        raise CronToolError(
            f"cron job {job['name']!r} already exists; use update to change it")
    _validate_prompt_file(base_wt, base, job)
    dirty = _dirty(base_wt)
    entries.append(job)
    raw["jobs"] = entries
    raw.pop("sessions", None)
    cfg["cron"] = raw
    _save_config(base_wt, cfg)
    sha: str | None = None
    if commit and not dirty:
        sha, err = _commit(base_wt, f"cron: add job {job['name']}")
        if err:
            raise CronToolError(f"could not commit the cron change: {err}")
    return _result(base_wt, base, job, sha, commit, dirty, "added")


def update_job(dir_: Path, args: dict, commit: bool = True) -> dict:
    base_wt, _common, base = _project(dir_)
    cfg = _load_config(base_wt)
    raw, entries = _entries(cfg)
    found = _find(entries, str(args.get("name") or ""))
    if found is None:
        raise CronToolError(f"no cron job named {args.get('name')!r}")
    index, existing = found
    job = clean_job(args, existing)
    _validate_prompt_file(base_wt, base, job)
    dirty = _dirty(base_wt)
    entries[index] = job
    raw["jobs"] = entries
    raw.pop("sessions", None)
    cfg["cron"] = raw
    _save_config(base_wt, cfg)
    sha: str | None = None
    if commit and not dirty:
        sha, err = _commit(base_wt, f"cron: update job {job['name']}")
        if err:
            raise CronToolError(f"could not commit the cron change: {err}")
    return _result(base_wt, base, job, sha, commit, dirty, "updated")


def remove_job(dir_: Path, args: dict, commit: bool = True) -> dict:
    base_wt, _common, base = _project(dir_)
    cfg = _load_config(base_wt)
    raw, entries = _entries(cfg)
    found = _find(entries, str(args.get("name") or ""))
    if found is None:
        raise CronToolError(f"no cron job named {args.get('name')!r}")
    index, removed = found
    removed = dict(removed)
    removed["name"] = dispatch.slug(str(removed.get("name") or ""))
    dirty = _dirty(base_wt)
    del entries[index]
    raw["jobs"] = entries
    raw.pop("sessions", None)
    cfg["cron"] = raw
    _save_config(base_wt, cfg)
    sha: str | None = None
    if commit and not dirty:
        sha, err = _commit(base_wt, f"cron: remove job {removed['name']}")
        if err:
            raise CronToolError(f"could not commit the cron change: {err}")
    return _result(base_wt, base, removed, sha, commit, dirty, "removed")


def run(op: str, args: dict, dir_: Path) -> dict:
    """Backend for the machine-global cron tools (`orch tasktool cron_*`)."""
    commit = args.get("commit", True)
    if not isinstance(commit, bool):
        raise CronToolError("commit must be a boolean")
    if op == "list":
        return list_jobs(dir_)
    if op == "add":
        return add_job(dir_, args, commit=commit)
    if op == "update":
        return update_job(dir_, args, commit=commit)
    if op == "remove":
        return remove_job(dir_, args, commit=commit)
    raise CronToolError(f"unknown cron op {op!r}")
