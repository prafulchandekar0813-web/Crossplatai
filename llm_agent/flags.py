"""What the assistant notices about its own knowledge, kept without any user content.

Two things live here, both safe to show to an administrator or manager:
  * flags: short generic notes ("this guide was wrong", "no guide for X") written by the assistant while it works, cleaned of
    personal data, merged when they repeat, and deleted after 30 days;
  * metrics: plain counters (how often each tool worked or failed, how many design notes a first check found...).
A user can switch recording off; nothing is then stored for their tasks. No flag or counter holds a user name."""
import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from llm_agent import scrub
from llm_agent.db import Database

KINDS = ("missing", "wrong", "outdated", "lesson")
KEEP_DAYS = 30
MAX_PER_TASK = 5


class FlagError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Flags:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    # ---- the user's choice ------------------------------------------------------------------------
    async def opted_in(self, user_id: str) -> bool:
        row = await self.db.fetchone("SELECT share_flags FROM users WHERE user_id=?", (user_id,))
        return True if row is None else bool(row["share_flags"])        # the administrator has no row: always on

    async def set_opt(self, user_id: str, share: bool) -> None:
        await self.db.execute("UPDATE users SET share_flags=? WHERE user_id=?", (1 if share else 0, user_id))

    # ---- flags ------------------------------------------------------------------------------------
    async def record(self, kind: str, topic: str, note: str, suggestion: str = "", tool: str = "") -> Dict[str, Any]:
        if kind not in KINDS:
            raise FlagError(f"kind must be one of: {', '.join(KINDS)}")
        topic, note = scrub.clean(topic, 80).lower(), scrub.clean(note, 300)
        suggestion, tool = scrub.clean(suggestion, 300), re.sub(r"[^a-z_]", "", (tool or "").lower())[:40]
        if len(note) < 8:
            raise FlagError("Describe what was missing or wrong in a sentence (no personal details).")
        fp = hashlib.sha256(f"{kind}|{topic}|{' '.join(note.lower().split())}".encode()).hexdigest()[:32]
        now = self.clock()
        await self.db.execute(
            "INSERT INTO knowledge_flags (fp, kind, topic, tool, note, suggestion, count, first_seen, last_seen, status) VALUES (?,?,?,?,?,?,1,?,?,'open') "
            "ON CONFLICT(fp) DO UPDATE SET count=count+1, last_seen=excluded.last_seen, "
            "suggestion=CASE WHEN excluded.suggestion<>'' THEN excluded.suggestion ELSE suggestion END",
            (fp, kind, topic, tool, note, suggestion, now, now))
        await self.purge()
        return {"kind": kind, "topic": topic, "note": note}

    async def list(self, status: str = "open") -> List[Dict[str, Any]]:
        await self.purge()
        rows = await self.db.fetchall("SELECT id, kind, topic, tool, note, suggestion, count, first_seen, last_seen, status FROM knowledge_flags "
                                      "WHERE status=? ORDER BY count DESC, last_seen DESC LIMIT 200", (status,))
        return [dict(r) for r in rows]

    async def set_status(self, flag_id: int, status: str) -> bool:
        if status not in ("open", "used", "dismissed"):
            raise FlagError("Unknown status")
        return await self.db.execute("UPDATE knowledge_flags SET status=? WHERE id=?", (status, flag_id)) > 0

    async def purge(self) -> None:
        cut = self.clock() - KEEP_DAYS * 86400
        await self.db.execute("DELETE FROM knowledge_flags WHERE last_seen<?", (cut,))
        await self.db.execute("DELETE FROM metrics WHERE day<?", (_day(cut),))

    # ---- counters ---------------------------------------------------------------------------------
    async def bump(self, name: str, by: int = 1) -> None:
        await self.db.execute("INSERT INTO metrics (day, name, n) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET n=n+excluded.n",
                              (_day(self.clock()), name[:80], by))

    async def metrics(self, days: int = 14) -> Dict[str, int]:
        since = _day(self.clock() - days * 86400)
        rows = await self.db.fetchall("SELECT name, SUM(n) AS n FROM metrics WHERE day>=? GROUP BY name ORDER BY name", (since,))
        return {r["name"]: int(r["n"]) for r in rows}
