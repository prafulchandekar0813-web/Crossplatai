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
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_expiry ON auth_sessions(expires_at);

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
