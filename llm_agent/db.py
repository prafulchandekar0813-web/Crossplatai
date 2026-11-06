"""Single shared SQLite connection (WAL) used from async code via worker threads."""
import asyncio
import sqlite3
import threading
from typing import Any, Callable, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,                         -- 'prompt' | 'blocked_command' | 'build' | 'suspended'
    detail TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_events(user_id, id);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    first_event INTEGER NOT NULL,
    last_event INTEGER NOT NULL,
    risk TEXT NOT NULL,                         -- 'none' | 'low' | 'medium' | 'high'
    status TEXT NOT NULL DEFAULT 'new',         -- 'new' | 'reviewed' | 'dismissed'
    markdown TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id, id);
CREATE TABLE IF NOT EXISTS published_apps (
    app_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0,
    has_previous INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'web',            -- web = hosted link | kit = managed Android/Windows app with a hosted catalogue (name kept for old databases)
    meta TEXT NOT NULL DEFAULT '{}'              -- kit settings (targets, version label, orientation) as JSON
);
CREATE INDEX IF NOT EXISTS idx_apps_owner ON published_apps(owner_id);
CREATE TABLE IF NOT EXISTS app_builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    platforms TEXT NOT NULL,
    package_id TEXT NOT NULL,
    status TEXT NOT NULL,                       -- queued | building | done | failed
    run_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_builds_app ON app_builds(app_id, id);
CREATE TABLE IF NOT EXISTS app_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price REAL,
    image TEXT NOT NULL DEFAULT '',
    in_stock INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_app ON app_items(app_id, collection, id);
CREATE TABLE IF NOT EXISTS app_devices (
    app_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    opens INTEGER NOT NULL DEFAULT 1,
    app_version TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (app_id, device_id)
);
CREATE TABLE IF NOT EXISTS app_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    device_id TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subs_app ON app_submissions(app_id, status, id);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_expiry ON auth_sessions(expires_at);

CREATE TABLE IF NOT EXISTS hosted_apps (
    app_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    has_previous INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_apps_owner ON hosted_apps(owner);

CREATE TABLE IF NOT EXISTS permanent_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    fact TEXT NOT NULL COLLATE NOCASE,
    category TEXT NOT NULL DEFAULT 'general',   -- general | preference | profile
    source TEXT NOT NULL DEFAULT 'agent',       -- agent | user
    created_at REAL NOT NULL,
    UNIQUE (user_id, fact)
);
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',          -- ok | error | cancelled (non-ok rows are never sent to the model)
    run_id TEXT,                                -- links an assistant message to its activity trace
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_user_session ON chat_history(user_id, session_id, id);
CREATE TABLE IF NOT EXISTS chat_summaries (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    upto_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
CREATE TABLE IF NOT EXISTS transient_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    log_type TEXT NOT NULL,                     -- 'run' = full activity trace of one task
    ref TEXT,                                   -- run id for 'run' rows
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transient_lookup ON transient_logs(user_id, session_id, id);
CREATE INDEX IF NOT EXISTS idx_transient_ref ON transient_logs(ref);
CREATE INDEX IF NOT EXISTS idx_transient_expiry ON transient_logs(expires_at);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Databases created before a column existed get it added (CREATE TABLE IF NOT EXISTS does not)."""
        ucols = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
        if "disabled" not in ucols:
            self._conn.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(published_apps)")}
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'web'"), ("meta", "TEXT NOT NULL DEFAULT '{}'")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE published_apps ADD COLUMN {name} {ddl}")

    def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            return fn(self._conn)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write statement; returns rowcount."""
        def op(c: sqlite3.Connection) -> int:
            return c.execute(sql, params).rowcount
        return await asyncio.to_thread(self._run, op)

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an INSERT; returns the new row id."""
        def op(c: sqlite3.Connection) -> int:
            return c.execute(sql, params).lastrowid
        return await asyncio.to_thread(self._run, op)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        def op(c: sqlite3.Connection) -> List[sqlite3.Row]:
            return c.execute(sql, params).fetchall()
        return await asyncio.to_thread(self._run, op)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        def op(c: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return c.execute(sql, params).fetchone()
        return await asyncio.to_thread(self._run, op)

    async def transaction(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run `fn(conn)` atomically."""
        def op(c: sqlite3.Connection) -> Any:
            c.execute("BEGIN IMMEDIATE")
            try:
                result = fn(c)
                c.execute("COMMIT")
                return result
            except Exception:
                c.execute("ROLLBACK")
                raise
        return await asyncio.to_thread(self._run, op)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
