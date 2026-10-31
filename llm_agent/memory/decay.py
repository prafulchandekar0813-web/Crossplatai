"""Decay: expire transient data (run traces, login sessions). Chat history and long-term facts are never touched."""
import time
from typing import Any, Dict

from llm_agent.db import Database


class DecayEngine:
    def __init__(self, db: Database):
        self.db = db

    async def run(self) -> Dict[str, Any]:
        now = time.time()
        traces = await self.db.execute("DELETE FROM transient_logs WHERE expires_at < ?", (now,))
        sessions = await self.db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        return {"status": "success", "pruned_run_traces": traces, "pruned_login_sessions": sessions, "timestamp": now}
