"""Notes: longer, useful write-ups of a conversation (decisions, requirements, how-tos, to-dos) kept per user.

Facts (memory.py) are one short sentence each and always shown to the assistant. Notes are longer: only their titles are
shown, and the assistant opens one with `read_notes` when it matters. Private to the user and deleted with their account."""
import json
import re
import time
from typing import Any, Dict, List, Optional

from llm_agent.db import Database

MAX_TITLE, MAX_BODY, MAX_NOTES, PER_CHAT = 80, 4000, 60, 5
_WORD = re.compile(r"[a-z0-9_]+")

NOTES_SYSTEM = ("You turn a conversation into useful notes the user can come back to. Reply with JSON only: "
                '{"notes":[{"title":"...","body":"..."}]} with 1 to 5 notes. Group by topic. Each body is short markdown (at most 250 words): '
                "what was decided, the requirements, names and settings that matter, the steps that worked, what is still to do. "
                "Keep specifics (names, numbers, file names, choices). Leave out small talk, secrets, passwords, API keys and anything personal that the notes do not need.")


class NoteError(ValueError):
    pass


def _terms(text: str) -> set:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


def parse(raw: str) -> List[Dict[str, str]]:
    """Notes out of a model reply; ignores everything that is not the expected JSON."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        items = json.loads(m.group(0)).get("notes", []) if m else []
    except ValueError:
        return []
    out = []
    for n in items if isinstance(items, list) else []:
        if isinstance(n, dict) and str(n.get("title", "")).strip() and str(n.get("body", "")).strip():
            out.append({"title": str(n["title"]), "body": str(n["body"])})
    return out[:PER_CHAT]


class Notes:
    def __init__(self, db: Database):
        self.db = db

    async def add(self, user_id: str, title: str, body: str, session_id: str = "", source: str = "agent") -> Dict[str, Any]:
        title, body = " ".join((title or "").split())[:MAX_TITLE], (body or "").strip()[:MAX_BODY]
        if len(title) < 2 or len(body) < 5:
            raise NoteError("A note needs a title and some text.")
        same = await self.db.fetchone("SELECT id FROM notes WHERE user_id=? AND title=?", (user_id, title))
        if same:  # saving the same topic again updates it instead of piling up copies
            await self.db.execute("UPDATE notes SET body=?, session_id=?, updated_at=? WHERE id=?", (body, session_id, time.time(), same["id"]))
            return {"id": same["id"], "title": title, "updated": True}
        nid = await self.db.insert("INSERT INTO notes (user_id, title, body, session_id, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                                   (user_id, title, body, session_id, source, time.time(), time.time()))
        await self.db.execute("DELETE FROM notes WHERE user_id=? AND id NOT IN (SELECT id FROM notes WHERE user_id=? ORDER BY updated_at DESC LIMIT ?)",
                              (user_id, user_id, MAX_NOTES))
        return {"id": nid, "title": title, "updated": False}

    async def list(self, user_id: str, full: bool = False) -> List[Dict[str, Any]]:
        cols = "id, title, body, source, session_id, created_at, updated_at" if full else "id, title, source, updated_at"
        rows = await self.db.fetchall(f"SELECT {cols} FROM notes WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        return [dict(r) for r in rows]

    async def get(self, user_id: str, note_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("SELECT id, title, body, source, session_id, created_at, updated_at FROM notes WHERE user_id=? AND id=?", (user_id, note_id))
        return dict(row) if row else None

    async def search(self, user_id: str, query: str, k: int = 3) -> List[Dict[str, Any]]:
        """The notes that share the most words with the query (titles count double); with no query, the newest."""
        notes = await self.list(user_id, full=True)
        want = _terms(query)
        if not want:
            return notes[:k]
        scored = []
        for n in notes:
            score = 2 * len(want & _terms(n["title"])) + len(want & _terms(n["body"]))
            if score:
                scored.append((score, n))
        return [n for _, n in sorted(scored, key=lambda t: -t[0])[:k]]

    async def delete(self, user_id: str, note_id: int) -> bool:
        return await self.db.execute("DELETE FROM notes WHERE user_id=? AND id=?", (user_id, note_id)) > 0

