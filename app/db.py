"""Tiny SQLite layer. One file, no ORM."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'upload',          -- upload | recording
    status TEXT NOT NULL DEFAULT 'new',           -- new | processing | done | error
    stage TEXT DEFAULT '',
    error TEXT DEFAULT '',
    duration REAL DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    start REAL NOT NULL,
    end REAL NOT NULL,
    hook TEXT NOT NULL,
    reason TEXT DEFAULT '',
    category TEXT DEFAULT '',
    dialect TEXT DEFAULT 'pt-PT',
    main_speaker TEXT DEFAULT 'host',
    score REAL DEFAULT 0,
    text TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pool',          -- pool | assigned | posted | rejected
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'upload',          -- upload | frame
    clip_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    account TEXT NOT NULL,
    day TEXT NOT NULL,                            -- YYYY-MM-DD (local tz)
    slot TEXT NOT NULL,                           -- HH:MM
    status TEXT NOT NULL DEFAULT 'draft',         -- draft | ready | posted | rejected
    hook TEXT NOT NULL,
    caption TEXT DEFAULT '',
    photo_id INTEGER,
    crop TEXT DEFAULT '',                         -- json {cx, cy, zoom}
    render_path TEXT DEFAULT '',
    render_status TEXT DEFAULT 'none',            -- none | queued | rendering | done | error
    render_error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_day ON posts(day, account);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',        -- queued | running | done | error
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def q(self, sql: str, args: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def exec(self, sql: str, args: tuple | dict = ()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid

    # --- jobs ------------------------------------------------------------------
    def enqueue(self, kind: str, **payload) -> int:
        # don't stack identical queued jobs
        p = json.dumps(payload, sort_keys=True)
        existing = self.one(
            "SELECT id FROM jobs WHERE kind=? AND payload=? AND status IN ('queued','running')",
            (kind, p),
        )
        if existing:
            return existing["id"]
        t = now()
        return self.exec(
            "INSERT INTO jobs(kind, payload, created_at, updated_at) VALUES (?,?,?,?)",
            (kind, p, t, t),
        )

    def next_job(self) -> sqlite3.Row | None:
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE jobs SET status='running', updated_at=? WHERE id=?", (now(), row["id"])
                )
            return row

    def finish_job(self, job_id: int, error: str = "") -> None:
        self.exec(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
            ("error" if error else "done", error[:2000], now(), job_id),
        )

    def reset_running_jobs(self) -> None:
        """On worker start: anything 'running' was interrupted — requeue it."""
        self.exec("UPDATE jobs SET status='queued' WHERE status='running'")
        self.exec("UPDATE posts SET render_status='queued' WHERE render_status='rendering'")
        self.exec("UPDATE sources SET status='new' WHERE status='processing'")
