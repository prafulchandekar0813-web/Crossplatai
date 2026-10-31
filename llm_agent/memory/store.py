"""Persistent memory: chat history, long-term facts, rolling summaries and run traces (all per user)."""
import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from llm_agent.db import Database

FACT_CATEGORIES = ("general", "preference", "profile")
MAX_FACT_CHARS = 300
_WORD = re.compile(r"[a-z0-9_]+")


def _terms(text: str) -> set:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ---- chat history ----------------------------------------------------------------------------
    async def add_message(self, user_id: str, session_id: str, role: str, content: str,
                          status: str = "ok", run_id: Optional[str] = None) -> int:
        return await self.db.insert(
            "INSERT INTO chat_history (user_id, session_id, role, message, status, run_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, session_id, role, content, status, run_id, time.time()),
        )

    async def get_messages(self, user_id: str, session_id: str, limit: int = 300) -> List[Dict[str, Any]]:
        """The *latest* `limit` messages, oldest first."""
        rows = await self.db.fetchall(
            "SELECT id, role, message, status, run_id, created_at FROM chat_history"
            " WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
            (user_id, session_id, limit),
        )
        return [
            {"id": r["id"], "role": r["role"], "content": r["message"], "status": r["status"],
             "run_id": r["run_id"], "created_at": r["created_at"]}
            for r in reversed(rows)
        ]

    async def model_history(self, user_id: str, session_id: str, after_id: int = 0, limit: int = 400) -> List[Dict[str, Any]]:
        """Messages that may be shown to the model: successful turns only, after the summarised part."""
        rows = await self.db.fetchall(
            "SELECT id, role, message FROM chat_history WHERE user_id=? AND session_id=? AND status='ok'"
            " AND id>? ORDER BY id DESC LIMIT ?",
            (user_id, session_id, after_id, limit),
        )
        return [{"id": r["id"], "role": r["role"], "content": r["message"]} for r in reversed(rows)]

    async def list_sessions(self, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT c.session_id AS id, MAX(c.created_at) AS updated_at, COUNT(*) AS messages,"
            " (SELECT message FROM chat_history f WHERE f.user_id=c.user_id AND f.session_id=c.session_id"
            "  AND f.role='user' ORDER BY f.id LIMIT 1) AS first_prompt"
            " FROM chat_history c WHERE c.user_id=? GROUP BY c.session_id ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )
        out = []
        for r in rows:
            title = " ".join((r["first_prompt"] or "New chat").split())
            out.append({"id": r["id"], "title": title[:60] + ("…" if len(title) > 60 else ""),
                        "updated_at": r["updated_at"], "messages": r["messages"]})
        return out

    async def session_exists(self, user_id: str, session_id: str) -> bool:
        row = await self.db.fetchone("SELECT 1 FROM chat_history WHERE user_id=? AND session_id=? LIMIT 1",
                                     (user_id, session_id))
        return row is not None

    async def delete_session(self, user_id: str, session_id: str) -> None:
        for sql in (
            "DELETE FROM chat_history WHERE user_id=? AND session_id=?",
            "DELETE FROM chat_summaries WHERE user_id=? AND session_id=?",
            "DELETE FROM transient_logs WHERE user_id=? AND session_id=?",
        ):
            await self.db.execute(sql, (user_id, session_id))

    # ---- rolling summaries -----------------------------------------------------------------------
    async def get_summary(self, user_id: str, session_id: str) -> Tuple[int, str]:
        row = await self.db.fetchone("SELECT upto_id, summary FROM chat_summaries WHERE user_id=? AND session_id=?",
                                     (user_id, session_id))
        return (row["upto_id"], row["summary"]) if row else (0, "")

    async def set_summary(self, user_id: str, session_id: str, upto_id: int, summary: str) -> None:
        await self.db.execute(
            "INSERT INTO chat_summaries (user_id, session_id, upto_id, summary) VALUES (?,?,?,?)"
            " ON CONFLICT(user_id, session_id) DO UPDATE SET upto_id=excluded.upto_id, summary=excluded.summary",
            (user_id, session_id, upto_id, summary),
        )

    # ---- long-term facts -------------------------------------------------------------------------
    async def add_fact(self, user_id: str, fact: str, category: str = "general", source: str = "agent",
                       max_facts: int = 100) -> Dict[str, Any]:
        fact = " ".join((fact or "").split())[:MAX_FACT_CHARS]
        if len(fact) < 3:
            raise ValueError("fact is too short")
        if category not in FACT_CATEGORIES:
            category = "general"
        try:
            fact_id = await self.db.insert(
                "INSERT INTO permanent_facts (user_id, fact, category, source, created_at) VALUES (?,?,?,?,?)",
                (user_id, fact, category, source, time.time()),
            )
            created = True
        except sqlite3.IntegrityError:
            row = await self.db.fetchone("SELECT id FROM permanent_facts WHERE user_id=? AND fact=?", (user_id, fact))
            fact_id, created = (row["id"] if row else 0), False
        if created:  # keep only the newest `max_facts`
            await self.db.execute(
                "DELETE FROM permanent_facts WHERE user_id=? AND id NOT IN"
                " (SELECT id FROM permanent_facts WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                (user_id, user_id, max_facts),
            )
        return {"id": fact_id, "fact": fact, "category": category, "created": created}

    async def list_facts(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, fact, category, source, created_at FROM permanent_facts WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def delete_fact(self, user_id: str, fact_id: int) -> bool:
        return await self.db.execute("DELETE FROM permanent_facts WHERE user_id=? AND id=?", (user_id, fact_id)) > 0

    async def relevant_facts(self, user_id: str, query: str, k: int) -> List[Dict[str, Any]]:
        """Top-k facts for this request: keyword overlap first, profile/preference facts and recent ones as tie-breaks."""
        if k <= 0:
            return []
        facts = await self.list_facts(user_id)  # newest first
        q = _terms(query)
        scored = []
        for rank, f in enumerate(facts):
            score = 2.0 * len(q & _terms(f["fact"]))
            score += 1.0 if f["category"] in ("profile", "preference") else 0.0
            score += 0.5 * (1 - rank / max(len(facts), 1))
            scored.append((score, f))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [f for _, f in scored[:k]]

    # ---- run traces (transient: expire with the decay engine) ------------------------------------
    async def save_run(self, user_id: str, session_id: str, run_id: str, trace: Dict[str, Any], ttl_days: int) -> None:
        now = time.time()
        await self.db.execute("DELETE FROM transient_logs WHERE log_type='run' AND ref=?", (run_id,))
        await self.db.insert(
            "INSERT INTO transient_logs (user_id, session_id, log_type, ref, content, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, session_id, "run", run_id, json.dumps(trace, default=str), now, now + ttl_days * 86400),
        )

    async def get_run(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone(
            "SELECT content FROM transient_logs WHERE log_type='run' AND ref=? AND user_id=?", (run_id, user_id))
        return json.loads(row["content"]) if row else None

    # ---- housekeeping ----------------------------------------------------------------------------
    async def delete_user_data(self, user_id: str) -> None:
        for table in ("chat_history", "chat_summaries", "transient_logs", "permanent_facts"):
            await self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))

    async def stats(self) -> Dict[str, int]:
        out = {}
        for key, table in (("users", "users"), ("messages", "chat_history"), ("facts", "permanent_facts"),
                           ("run_traces", "transient_logs")):
            row = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
            out[key] = row["n"]
        return out
