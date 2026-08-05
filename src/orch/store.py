"""Runtime state: claims, leases, retries.

One authority per question. Git holds **intent** - which tasks exist and what they
mean, in `TASKS.md` and `tasks/*/task.md`, human-authored and reviewable. This
module holds **runtime** - what is actually running, since when, where, on which
port.

`- [-]` in TASKS.md becomes a rendered convenience: written back best-effort for a
human reading the file, and never read as authority. That kills two problems at
once. A dispatch that dies after flipping the checkbox currently leaves a task
indistinguishable from one still running, and two files that both claim to hold
state will eventually disagree.

Why SQLite rather than a lock file:

- A claim is one atomic statement under `BEGIN IMMEDIATE`, so no external lock is
  needed at all.
- Leases expire, so a crashed agent's task becomes reclaimable. Git can express
  state but not a lease, and that - not the race - is the real gap.
- Never `flock` a tracked file: git replaces inodes, so the lock silently stops
  referring to the file everyone else sees. And git's own `index.lock` *fails*
  rather than queueing, so it cannot serialize anything.

One database per machine, mirroring how opencode keeps a single global store rather
than one per project.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LEASE_SECONDS = 900

TODO = "todo"
RUNNING = "running"
DONE = "done"
STUCK = "stuck"
# The agent stopped, but nothing told the store whether the gate passed. Distinct
# from `done` on purpose: only a gate verdict justifies `done`, and the component
# that would report one (the daemon endpoint) does not exist yet.
NEEDS_REVIEW = "needs-review"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,          -- bare repo path: the unit of a project
    name         TEXT NOT NULL,          -- kebab-case task name
    status       TEXT NOT NULL DEFAULT 'todo',
    seq          INTEGER NOT NULL DEFAULT 0,
    lease_until  INTEGER,                -- unix seconds; NULL when not running
    worktree     TEXT,
    branch       TEXT,
    port         INTEGER,
    pid          INTEGER,
    tmux         TEXT,
    parent_session TEXT,                 -- owned here, not inferred from the harness
    child_session  TEXT,
    retries      INTEGER NOT NULL DEFAULT 0,
    fingerprint  TEXT,                   -- persisted so a TUI restart cannot re-baseline
    last_outcome TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    UNIQUE(project, name)
);
CREATE INDEX IF NOT EXISTS task_status ON task(status, project);

-- Alert state, so a notification can be keyed on the permission request id and
-- deduplicated. An agent can block several times in one turn with a new id each
-- time: two were observed 27 seconds apart for the same pattern.
CREATE TABLE IF NOT EXISTS alert (
    request_id   TEXT PRIMARY KEY,
    session_id   TEXT,
    worktree     TEXT,
    kind         TEXT,
    detail       TEXT,
    first_seen   INTEGER NOT NULL,
    notified_at  INTEGER,
    resolved_at  INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def default_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "orch" / "orch.db"


@dataclass
class Task:
    id: int
    project: str
    name: str
    status: str
    lease_until: int | None
    worktree: str | None
    port: int | None
    pid: int | None
    retries: int

    @property
    def lease_expired(self) -> bool:
        return (
            self.status == RUNNING
            and self.lease_until is not None
            and self.lease_until < int(time.time())
        )


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ intent in
    def sync_intent(self, project: str, names: list[str]) -> int:
        """Register tasks discovered in git. Intent lives in git; this only mirrors
        the list so claims have something to point at. Existing rows keep their
        runtime state."""
        now = int(time.time())
        added = 0
        for i, name in enumerate(names):
            cur = self.db.execute(
                "INSERT OR IGNORE INTO task(project,name,seq,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                (project, name, i, now, now),
            )
            added += cur.rowcount or 0
        return added

    # -------------------------------------------------------------------- claiming
    def claim(
        self,
        project: str,
        name: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> str | None:
        """Atomically claim a task and return its name, or None if none available.

        `BEGIN IMMEDIATE` takes the write lock before reading, which is what makes
        the read-then-update safe against a concurrent claimer.
        """
        now = int(time.time())
        until = now + lease_seconds
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if name:
                row = self.db.execute(
                    "SELECT name FROM task WHERE project=? AND name=?"
                    " AND (status='todo' OR (status='running' AND lease_until<?))",
                    (project, name, now),
                ).fetchone()
            else:
                row = self.db.execute(
                    "SELECT name FROM task WHERE project=?"
                    " AND (status='todo' OR (status='running' AND lease_until<?))"
                    " ORDER BY seq LIMIT 1",
                    (project, now),
                ).fetchone()
            if not row:
                self.db.execute("COMMIT")
                return None
            self.db.execute(
                "UPDATE task SET status='running', lease_until=?, updated_at=?"
                " WHERE project=? AND name=?",
                (until, now, project, row["name"]),
            )
            self.db.execute("COMMIT")
            return str(row["name"])
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def record_launch(
        self,
        project: str,
        name: str,
        worktree: str | None = None,
        branch: str | None = None,
        port: int | None = None,
        pid: int | None = None,
        tmux: str | None = None,
        parent_session: str | None = None,
    ) -> None:
        """Write the lease details. Called even when the daemon is unreachable, so
        that a daemon which starts later can still reconcile children it never
        saw."""
        self.db.execute(
            "UPDATE task SET worktree=COALESCE(?,worktree), branch=COALESCE(?,branch),"
            " port=COALESCE(?,port), pid=COALESCE(?,pid), tmux=COALESCE(?,tmux),"
            " parent_session=COALESCE(?,parent_session), updated_at=?"
            " WHERE project=? AND name=?",
            (worktree, branch, port, pid, tmux, parent_session,
             int(time.time()), project, name),
        )

    def renew(self, project: str, name: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        now = int(time.time())
        self.db.execute(
            "UPDATE task SET lease_until=?, updated_at=? WHERE project=? AND name=?",
            (now + lease_seconds, now, project, name),
        )

    def finish(self, project: str, name: str, status: str,
               outcome: str | None = None) -> None:
        self.db.execute(
            "UPDATE task SET status=?, last_outcome=COALESCE(?,last_outcome),"
            " lease_until=NULL, updated_at=? WHERE project=? AND name=?",
            (status, outcome, int(time.time()), project, name),
        )

    def expired(self, project: str | None = None) -> list[Task]:
        """Tasks whose lease has lapsed.

        Expiry alone is not permission to reclaim. The caller must also confirm the
        port is unreachable and the tmux session is gone - only all three together
        mean *definitely dead*. A busy agent whose lease lapsed because renewal
        broke is not the same thing.
        """
        now = int(time.time())
        sql = ("SELECT * FROM task WHERE status='running' AND lease_until IS NOT NULL"
               " AND lease_until<?")
        args: list[object] = [now]
        if project:
            sql += " AND project=?"
            args.append(project)
        return [self._task(r) for r in self.db.execute(sql, args)]

    def reclaim(self, project: str, name: str, reason: str) -> None:
        """Return a task to available after it was proven dead."""
        self.db.execute(
            "UPDATE task SET status='todo', lease_until=NULL, port=NULL, pid=NULL,"
            " tmux=NULL, last_outcome=?, updated_at=? WHERE project=? AND name=?",
            (f"reclaimed: {reason}", int(time.time()), project, name),
        )

    # --------------------------------------------------------------------- retries
    def bump_retry(self, project: str, name: str) -> int:
        self.db.execute(
            "UPDATE task SET retries=retries+1, updated_at=? WHERE project=? AND name=?",
            (int(time.time()), project, name),
        )
        row = self.db.execute(
            "SELECT retries FROM task WHERE project=? AND name=?", (project, name)
        ).fetchone()
        return int(row["retries"]) if row else 0

    def reset_retry(self, project: str, name: str) -> None:
        self.db.execute(
            "UPDATE task SET retries=0, updated_at=? WHERE project=? AND name=?",
            (int(time.time()), project, name),
        )

    def set_fingerprint(self, project: str, name: str, fp: str) -> None:
        """Persisted deliberately: holding the gate baseline in plugin memory means
        a TUI restart re-baselines and can skip the first check entirely."""
        self.db.execute(
            "UPDATE task SET fingerprint=?, updated_at=? WHERE project=? AND name=?",
            (fp, int(time.time()), project, name),
        )

    def fingerprint(self, project: str, name: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM task WHERE project=? AND name=?", (project, name)
        ).fetchone()
        return row["fingerprint"] if row else None

    # ---------------------------------------------------------------------- alerts
    def note_alert(self, request_id: str, session_id: str, worktree: str | None,
                   kind: str, detail: str) -> bool:
        """Record a pending approval. Returns True if this id is newly seen.

        Keyed on the request id because one turn can raise several requests, each
        with its own id. Alerting on "is /permission non-empty" instead would fire
        repeatedly for what a human experiences as a single decision.
        """
        now = int(time.time())
        cur = self.db.execute(
            "INSERT OR IGNORE INTO alert(request_id,session_id,worktree,kind,detail,"
            "first_seen) VALUES(?,?,?,?,?,?)",
            (request_id, session_id, worktree, kind, detail, now),
        )
        return bool(cur.rowcount)

    def mark_notified(self, request_id: str) -> None:
        self.db.execute("UPDATE alert SET notified_at=? WHERE request_id=?",
                        (int(time.time()), request_id))

    def resolve_alert(self, request_id: str) -> None:
        self.db.execute("UPDATE alert SET resolved_at=? WHERE request_id=?",
                        (int(time.time()), request_id))

    def unresolved_alerts(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM alert WHERE resolved_at IS NULL ORDER BY first_seen"))

    # ----------------------------------------------------------------------- reads
    def get(self, project: str, name: str) -> Task | None:
        row = self.db.execute(
            "SELECT * FROM task WHERE project=? AND name=?", (project, name)
        ).fetchone()
        return self._task(row) if row else None

    def tasks(self, project: str | None = None,
              status: str | None = None) -> list[Task]:
        sql = "SELECT * FROM task WHERE 1=1"
        args: list[object] = []
        if project:
            sql += " AND project=?"
            args.append(project)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY seq, name"
        return [self._task(r) for r in self.db.execute(sql, args)]

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], project=row["project"], name=row["name"],
            status=row["status"], lease_until=row["lease_until"],
            worktree=row["worktree"], port=row["port"], pid=row["pid"],
            retries=row["retries"],
        )
