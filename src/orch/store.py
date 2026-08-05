from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LEASE_SECONDS = 900

MIN_HARNESS = (1, 18, 11)

TODO = "todo"
RUNNING = "running"
DONE = "done"
MERGED = "merged"
STUCK = "stuck"
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

-- Gate verdicts, append-only.
--
-- A history rather than a column, because "did this ever pass, and what did the run
-- before it say" is the question a human actually asks when deciding whether to trust
-- a branch. `source` records who ran the gate: `daemon` verdicts are the only ones
-- that may promote a task to `done`, since the router runs inside the agent's own
-- server and its report is a self-report.
CREATE TABLE IF NOT EXISTS verdict (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    name       TEXT NOT NULL,
    event      TEXT NOT NULL,
    outcome    TEXT NOT NULL,          -- passed | failed | no-gate-configured
    detail     TEXT,
    diverged   INTEGER NOT NULL DEFAULT 0,
    seconds    REAL,
    source     TEXT NOT NULL DEFAULT 'daemon',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS verdict_task ON verdict(project, name, id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Which harness this database has been operated against.
--
-- The floor is 1.18.11 because that is the version the box is pinned to; anything
-- above it is a bonus and minor releases are assumed API-compatible. That assumption
-- is recorded rather than trusted: every version actually seen is written here, so
-- when a release does change behaviour there is a history saying when the change
-- arrived and what it replaced, instead of a guess. A harness is a dependency like
-- any other, and this is its lockfile evidence.
CREATE TABLE IF NOT EXISTS harness_seen (
    version    TEXT PRIMARY KEY,      -- dotted, e.g. "1.18.13"
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    below_min  INTEGER NOT NULL DEFAULT 0
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
    branch: str | None = None
    last_outcome: str | None = None
    fingerprint: str | None = None

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

    def sync_intent(self, project: str, names: list[str]) -> int:
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

    def claim(
        self,
        project: str,
        name: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> str | None:
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
        now = int(time.time())
        sql = ("SELECT * FROM task WHERE status='running' AND lease_until IS NOT NULL"
               " AND lease_until<?")
        args: list[object] = [now]
        if project:
            sql += " AND project=?"
            args.append(project)
        return [self._task(r) for r in self.db.execute(sql, args)]

    def reclaim(self, project: str, name: str, reason: str) -> None:
        self.db.execute(
            "UPDATE task SET status='todo', lease_until=NULL, port=NULL, pid=NULL,"
            " tmux=NULL, last_outcome=?, updated_at=? WHERE project=? AND name=?",
            (f"reclaimed: {reason}", int(time.time()), project, name),
        )

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
        self.db.execute(
            "UPDATE task SET fingerprint=?, updated_at=? WHERE project=? AND name=?",
            (fp, int(time.time()), project, name),
        )

    def fingerprint(self, project: str, name: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM task WHERE project=? AND name=?", (project, name)
        ).fetchone()
        return row["fingerprint"] if row else None

    def record_verdict(self, project: str, name: str, event: str, outcome: str,
                       detail: str = "", diverged: bool = False,
                       seconds: float = 0.0, source: str = "daemon") -> None:
        self.db.execute(
            "INSERT INTO verdict(project,name,event,outcome,detail,diverged,seconds,"
            "source,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project, name, event, outcome, detail[:4000], int(diverged), seconds,
             source, int(time.time())),
        )
        self.db.execute(
            "UPDATE task SET last_outcome=?, updated_at=? WHERE project=? AND name=?",
            (f"{event}: {outcome}", int(time.time()), project, name),
        )

    def last_verdict(self, project: str, name: str,
                     source: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM verdict WHERE project=? AND name=?"
        args: list[object] = [project, name]
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT 1"
        return self.db.execute(sql, args).fetchone()

    def verdicts(self, project: str, name: str, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM verdict WHERE project=? AND name=?"
            " ORDER BY id DESC LIMIT ?", (project, name, limit)))

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def note_harness(self, version: tuple[int, ...], below_min: bool) -> str | None:
        dotted = ".".join(map(str, version))
        now = int(time.time())
        previous = self.get_meta("harness_version")
        self.db.execute(
            "INSERT INTO harness_seen(version,first_seen,last_seen,below_min)"
            " VALUES(?,?,?,?) ON CONFLICT(version) DO UPDATE SET last_seen=excluded.last_seen",
            (dotted, now, now, int(below_min)),
        )
        self.set_meta("harness_version", dotted)
        self.set_meta("harness_min", ".".join(map(str, MIN_HARNESS)))
        return previous if previous and previous != dotted else None

    def harness_history(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM harness_seen ORDER BY first_seen"))

    def note_alert(self, request_id: str, session_id: str, worktree: str | None,
                   kind: str, detail: str) -> bool:
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

    def get(self, project: str, name: str) -> Task | None:
        row = self.db.execute(
            "SELECT * FROM task WHERE project=? AND name=?", (project, name)
        ).fetchone()
        return self._task(row) if row else None

    def by_worktree(self, worktree: str) -> Task | None:
        row = self.db.execute(
            "SELECT * FROM task WHERE worktree=? ORDER BY updated_at DESC LIMIT 1",
            (str(worktree).rstrip("/"),),
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
            retries=row["retries"], branch=row["branch"],
            last_outcome=row["last_outcome"], fingerprint=row["fingerprint"],
        )
