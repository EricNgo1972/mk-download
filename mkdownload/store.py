"""SQLite on the /data volume: jobs, the webhook outbox, file metadata, cookie metadata and settings.

One connection behind a lock: the event loop does nearly all the work, but yt-dlp's worker threads look up cookies.
"""

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL,
    is_live INTEGER NOT NULL DEFAULT 0,
    progress TEXT NOT NULL DEFAULT '{}',
    files TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    error_message TEXT,
    callback_url TEXT,
    callback_ref TEXT,
    notify_progress INTEGER NOT NULL DEFAULT 0,
    keep INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL,
    last_error TEXT,
    created REAL NOT NULL,
    delivered REAL
);
CREATE INDEX IF NOT EXISTS events_due ON events (status, next_at);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    job_id TEXT,
    created REAL NOT NULL,
    keep INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cookies (site TEXT PRIMARY KEY, browser TEXT NOT NULL, saved REAL NOT NULL);
"""

FINAL_STATES = ("completed", "failed", "cancelled")
ACTIVE_STATES = ("waiting", "downloading", "recording", "finalizing")


@dataclass
class Job:
    id: str
    kind: str  # video, torrent, ed2k, clip
    url: str
    params: dict[str, Any]
    status: str
    is_live: bool = False
    progress: dict[str, Any] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)  # paths relative to /data/downloads
    error_code: str | None = None
    error_message: str | None = None
    callback_url: str | None = None
    callback_ref: str | None = None
    notify_progress: bool = False
    keep: bool = False
    created: float = 0.0
    started: float | None = None
    finished: float | None = None


JSON_COLUMNS = {"params", "progress", "files"}
BOOL_COLUMNS = {"is_live", "notify_progress", "keep"}


def to_row(job: Job) -> dict[str, Any]:
    row = asdict(job)
    for key in JSON_COLUMNS:
        row[key] = json.dumps(row[key])
    for key in BOOL_COLUMNS:
        row[key] = int(row[key])
    return row


def to_job(row: sqlite3.Row) -> Job:
    values = dict(row)
    for key in JSON_COLUMNS:
        values[key] = json.loads(values[key])
    for key in BOOL_COLUMNS:
        values[key] = bool(values[key])
    return Job(**values)


class Store:
    def __init__(self, path: Path) -> None:
        self.db = LockedConnection(sqlite3.connect(path, isolation_level=None, check_same_thread=False))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # kv

    def load_value(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def save_value(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def delete_value(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # jobs

    def add_job(self, job: Job) -> None:
        row = to_row(job)
        names = ", ".join(row)
        marks = ", ".join(f":{name}" for name in row)
        self.db.execute(f"INSERT INTO jobs ({names}) VALUES ({marks})", row)

    def save_job(self, job: Job, event: dict[str, Any] | None = None) -> None:
        """Writes the job, and its outbox event if any, in one transaction: a crash cannot keep one and lose the other."""
        row = to_row(job)
        assignments = ", ".join(f"{name} = :{name}" for name in row if name != "id")
        with self.transaction():
            self.db.execute(f"UPDATE jobs SET {assignments} WHERE id = :id", row)
            if event is not None:
                self.db.execute(
                    "INSERT INTO events (id, job_id, type, url, body, next_at, created)"
                    " VALUES (:id, :job_id, :type, :url, :body, :next_at, :created)",
                    event,
                )

    def save_progress(self, job: Job) -> None:
        self.db.execute("UPDATE jobs SET progress = ? WHERE id = ?", (json.dumps(job.progress), job.id))

    def job_by_id(self, job_id: str) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return to_job(row) if row else None

    def jobs(self, statuses: tuple[str, ...] = (), limit: int = 200) -> list[Job]:
        if statuses:
            marks = ", ".join("?" for _ in statuses)
            rows = self.db.execute(
                f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created DESC LIMIT ?", (*statuses, limit)
            )
        else:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        return [to_job(row) for row in rows]

    def count_jobs(self, status: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)).fetchone()[0]

    def delete_old_jobs(self, before: float) -> None:
        marks = ", ".join("?" for _ in FINAL_STATES)
        self.db.execute(f"DELETE FROM jobs WHERE status IN ({marks}) AND finished < ?", (*FINAL_STATES, before))

    # events

    def due_events(self, now: float, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE status = 'pending' AND next_at <= ? ORDER BY next_at LIMIT ?", (now, limit)
        ).fetchall()

    def save_event_result(self, event_id: str, **values: Any) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        self.db.execute(f"UPDATE events SET {assignments} WHERE id = :id", {**values, "id": event_id})

    def undelivered_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE status = 'undelivered' ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()

    def count_events(self, status: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events WHERE status = ?", (status,)).fetchone()[0]

    def delete_old_events(self, before: float) -> None:
        self.db.execute("DELETE FROM events WHERE status != 'pending' AND created < ?", (before,))

    # files

    def add_file(self, path: str, job_id: str | None, keep: bool) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files (path, job_id, created, keep) VALUES (?, ?, ?, ?)",
            (path, job_id, time.time(), int(keep)),
        )

    def file_by_path(self, path: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()

    def delete_file(self, path: str) -> None:
        self.db.execute("DELETE FROM files WHERE path = ?", (path,))

    def delete_files_under(self, folder: str) -> None:
        self.db.execute("DELETE FROM files WHERE path = ? OR path LIKE ?", (folder, folder.rstrip("/") + "/%"))

    # cookies

    def save_cookie_site(self, site: str, browser: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO cookies (site, browser, saved) VALUES (?, ?, ?)", (site, browser, time.time()))

    def delete_cookie_site(self, site: str) -> None:
        self.db.execute("DELETE FROM cookies WHERE site = ?", (site,))

    def cookie_sites(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM cookies ORDER BY site").fetchall()

    def transaction(self):
        return Transaction(self.db)


class LockedConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        self.connection = connection
        self.lock = threading.RLock()

    def execute(self, sql: str, parameters: Any = ()) -> "Rows":
        with self.lock:
            return Rows(self.connection.execute(sql, parameters).fetchall())

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.connection.executescript(sql)


class Rows(list):
    """Rows fetched while holding the lock; fetchone/fetchall keep the sqlite3 cursor's interface."""

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class Transaction:
    def __init__(self, db: LockedConnection) -> None:
        self.db = db

    def __enter__(self) -> None:
        self.db.lock.acquire()
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind, value, traceback) -> None:
        try:
            self.db.execute("COMMIT" if kind is None else "ROLLBACK")
        finally:
            self.db.lock.release()
