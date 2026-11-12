"""SQLite storage. One short-lived connection per operation (safe for the threadpool), WAL mode.

Every table is CREATE ... IF NOT EXISTS, so a database from an earlier version gets the new tables
(v3: tasks, task_events) on the next start; existing tables and rows are never touched."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS asks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    title       TEXT NOT NULL,
    context     TEXT,
    options     TEXT NOT NULL DEFAULT '[]',
    links       TEXT NOT NULL DEFAULT '[]',
    priority    TEXT NOT NULL DEFAULT 'normal',
    ref         TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    answer      TEXT,
    answered_at TEXT,
    resolution  TEXT,
    closed_by   TEXT,
    closed_at   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS asks_status ON asks (status);
CREATE INDEX IF NOT EXISTS asks_agent ON asks (agent);

CREATE TABLE IF NOT EXISTS ask_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ask_id  INTEGER NOT NULL REFERENCES asks (id),
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ask_events_ask ON ask_events (ask_id);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- own sequence: task #3 is not ask #3
    agent       TEXT NOT NULL,
    title       TEXT NOT NULL,
    context     TEXT,
    status      TEXT NOT NULL DEFAULT 'active',     -- active | waiting | done | dropped
    waiting_on  TEXT,
    next_check  TEXT,                               -- YYYY-MM-DD
    links       TEXT NOT NULL DEFAULT '[]',
    ref         TEXT,
    resolution  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS tasks_agent ON tasks (agent);

CREATE TABLE IF NOT EXISTS task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id),
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events (task_id);
"""

ASK_FIELDS = (
    "id", "agent", "title", "context", "options", "links", "priority", "ref", "status",
    "answer", "answered_at", "resolution", "closed_by", "closed_at", "created_at", "updated_at",
)
TASK_FIELDS = (
    "id", "agent", "title", "context", "status", "waiting_on", "next_check", "links", "ref", "resolution",
    "created_at", "updated_at", "finished_at",
)


def utc_now() -> str:
    """ISO-8601 UTC with milliseconds and Z, e.g. 2026-10-31T10:48:00.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ask_from_row(row: sqlite3.Row) -> dict:
    ask = {field: row[field] for field in ASK_FIELDS}
    ask["options"] = json.loads(ask["options"] or "[]")
    ask["links"] = json.loads(ask["links"] or "[]")
    ask["attachments"] = []  # pictures and video live on the hosted bridge only (SPEC v5 step 4); never null
    return ask


def task_from_row(row: sqlite3.Row) -> dict:
    task = {field: row[field] for field in TASK_FIELDS}
    task["links"] = json.loads(task["links"] or "[]")
    return task


def event_from_row(row: sqlite3.Row, key: str = "ask_id") -> dict:
    """An ask_events row (key "ask_id") or a task_events row (key "task_id")."""
    return {
        "id": row["id"],
        key: row[key],
        "at": row["at"],
        "actor": row["actor"],
        "kind": row["kind"],
        "payload": json.loads(row["payload"] or "{}"),
    }


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def ensure_schema(self, script: str) -> None:
        """Create the tables of an optional component (CREATE ... IF NOT EXISTS), e.g. the Drive mirror."""
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.executescript(script)
        finally:
            conn.close()

    @contextmanager
    def transaction(self, write: bool = False):
        """Yield a connection inside a transaction; writers take the lock up front (BEGIN IMMEDIATE)
        so a read-check-update sequence cannot interleave with another writer."""
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
