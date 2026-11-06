"""Activity record the Reporter reads: what users asked for and what the safety checks refused.
Only user-written text and refusals are kept (never the assistant's replies or file contents), and rows expire
after `reporter_retention_days`."""
import time
from typing import Any, Dict, List

MAX_DETAIL = 600
KINDS = ("prompt", "blocked_command", "build", "suspended")


class Audit:
    def __init__(self, db, enabled=lambda: True):
        self.db, self.enabled = db, enabled

    async def log(self, user_id: str, kind: str, detail: str) -> None:
        """Best effort: recording must never break the request that triggered it."""
        if not self.enabled() or not user_id or kind not in KINDS:
            return
        try:
            await self.db.insert("INSERT INTO audit_events (user_id, kind, detail, created_at) VALUES (?,?,?,?)",
                                 (user_id, kind, (detail or "")[:MAX_DETAIL], time.time()))
        except Exception:
            pass

    async def users_with_news(self) -> List[str]:
        rows = await self.db.fetchall(
            "SELECT DISTINCT e.user_id FROM audit_events e WHERE e.id > COALESCE("
            "(SELECT MAX(r.last_event) FROM reports r WHERE r.user_id=e.user_id), 0)")
        return [r["user_id"] for r in rows]

    async def since_last_report(self, user_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, kind, detail, created_at FROM audit_events WHERE user_id=? AND id > COALESCE("
            "(SELECT MAX(last_event) FROM reports WHERE user_id=?), 0) ORDER BY id LIMIT ?", (user_id, user_id, limit))
        return [dict(r) for r in rows]

    async def purge(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        n = await self.db.execute("DELETE FROM audit_events WHERE created_at < ?", (cutoff,))
        n += await self.db.execute("DELETE FROM reports WHERE created_at < ?", (cutoff,))
        return n

    async def forget_user(self, user_id: str) -> None:
        await self.db.execute("DELETE FROM audit_events WHERE user_id=?", (user_id,))
