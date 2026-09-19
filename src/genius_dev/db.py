"""SQLite durable state with versioned migrations."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1 — core schema
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, root TEXT UNIQUE, created REAL, info TEXT);
    CREATE TABLE sessions (id INTEGER PRIMARY KEY, started REAL, ended REAL, goal TEXT, status TEXT, summary TEXT);
    CREATE TABLE tasks (id INTEGER PRIMARY KEY, parent_id INTEGER, title TEXT, status TEXT DEFAULT 'todo',
                        req_ids TEXT DEFAULT '[]', position INTEGER DEFAULT 0, created REAL, updated REAL);
    CREATE TABLE requirements (id TEXT PRIMARY KEY, description TEXT, source TEXT, status TEXT DEFAULT 'NOT_STARTED',
                        files TEXT DEFAULT '[]', verify TEXT DEFAULT '', result TEXT DEFAULT '', updated REAL);
    CREATE TABLE model_calls (id INTEGER PRIMARY KEY, ts REAL, project TEXT, session_id INTEGER, provider TEXT, model TEXT,
                        role TEXT, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER DEFAULT 0,
                        cost REAL, latency REAL, ok INTEGER, error TEXT);
    CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, ts REAL, session_id INTEGER, tool TEXT, args TEXT, duration REAL,
                        ok INTEGER, stdout TEXT, stderr TEXT, files TEXT, status TEXT DEFAULT 'done');
    CREATE TABLE checkpoints (id TEXT PRIMARY KEY, ts REAL, commit_hash TEXT, task TEXT, requirements TEXT, tests TEXT,
                        files_changed TEXT, migrations TEXT, kind TEXT DEFAULT 'auto');
    CREATE TABLE test_runs (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, command TEXT, passed INTEGER, failed INTEGER,
                        total INTEGER, ok INTEGER, duration REAL, summary TEXT);
    CREATE TABLE bugs (id INTEGER PRIMARY KEY, ts REAL, title TEXT, detail TEXT, status TEXT DEFAULT 'open');
    CREATE TABLE decisions (id INTEGER PRIMARY KEY, ts REAL, title TEXT, detail TEXT, source TEXT);
    CREATE TABLE handoffs (id INTEGER PRIMARY KEY, ts REAL, path TEXT);
    CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, session_id INTEGER, category TEXT, label TEXT, message TEXT, data TEXT);
    CREATE TABLE processes (name TEXT PRIMARY KEY, pid INTEGER, command TEXT, port INTEGER, url TEXT, log TEXT, started REAL, status TEXT);
    CREATE INDEX idx_model_calls_ts ON model_calls(ts);
    CREATE INDEX idx_events_session ON events(session_id, id);
    """,
]


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # -- migrations -----------------------------------------------------------
    @property
    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        with self.lock:
            v = self.version
            for i in range(v, len(MIGRATIONS)):
                self.conn.executescript("BEGIN;" + MIGRATIONS[i] + f"PRAGMA user_version={i + 1};COMMIT;")
            return self.version

    # -- helpers --------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        r = self.one(sql, params)
        return default if r is None or r[0] is None else r[0]

    def get_meta(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_meta(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- domain ---------------------------------------------------------------
    def cost_today(self, provider: str | None = None) -> float:
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        q = "SELECT COALESCE(SUM(cost),0) c FROM model_calls WHERE ts>=?"
        args: list[Any] = [start]
        if provider:
            q += " AND provider=?"
            args.append(provider)
        return float(self.query(q, args)[0]["c"])

    def usage_grouped(self, col: str, since: float | None = None) -> list[dict[str, Any]]:
        if col not in ("role", "model", "provider", "project"):
            raise ValueError(col)
        if since is None:
            since = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        rows = self.query(f"""SELECT {col} AS key, COUNT(*) requests, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
                      SUM(cached_tokens) cached_tokens, SUM(cost) cost, AVG(latency) latency FROM model_calls WHERE ts>=? GROUP BY {col} ORDER BY cost DESC, requests DESC""", (since,))
        return [dict(r) for r in rows]

    def usage_by_provider(self, since: float | None = None) -> list[dict[str, Any]]:
        if since is None:
            since = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        rows = self.query(
            """SELECT provider, model, COUNT(*) requests, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
                      SUM(cached_tokens) cached_tokens, SUM(cost) cost, AVG(latency) latency,
                      SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) errors, MAX(ts) last_ts
               FROM model_calls WHERE ts>=? GROUP BY provider, model ORDER BY cost DESC""", (since,))
        return [dict(r) for r in rows]
