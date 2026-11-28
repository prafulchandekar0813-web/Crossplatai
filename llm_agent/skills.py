"""Skills: short how-to guides the assistant loads when a task needs them (use_skill), instead of carrying them in every prompt.

Built-in skills are markdown files in `llm_agent/skill_files/` (name and description in front matter). Approved edits, and skills
added by an administrator or manager, are stored in the database and win over the file of the same name. Reading is open to
every assistant run; only people who can review (administrator, managers) change them, and nothing here holds user data.

A user can also keep personal skills (table `user_skills`). Those are private: only that user's assistant sees them, a personal
skill with the name of a shared one replaces it for that user alone, and they are never read by reviewers or the curator."""
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_agent import scrub
from llm_agent.db import Database

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_BODY = 12000
MAX_MINE, MAX_MINE_BODY = 20, 6000
MAX_DESCRIPTION = 240
BUILTIN_DIR = Path(__file__).parent / "skill_files"


class SkillError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def parse(text: str) -> Tuple[Dict[str, str], str]:
    """Front matter (name, description) and body of a skill file."""
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body.strip()


def check(name: str, description: str, body: str) -> None:
    if not NAME.match(name or ""):
        raise SkillError("The name must be 2 to 40 characters: lowercase letters, digits and dashes.")
    if not (description or "").strip() or len(description) > MAX_DESCRIPTION:
        raise SkillError(f"Write a one-line description of when to use it (up to {MAX_DESCRIPTION} characters).")
    if not (body or "").strip() or len(body) > MAX_BODY:
        raise SkillError(f"The skill text must be between 1 and {MAX_BODY} characters.")
    if (found := scrub.problems(f"{description}\n{body}")):
        raise SkillError("A shared skill must not contain " + ", ".join(found) + ". Remove it and save again.")


class Skills:
    def __init__(self, db: Database, directory: Path = BUILTIN_DIR, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock
        self._builtin: Dict[str, Dict[str, str]] = {}
        for f in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            meta, body = parse(f.read_text(encoding="utf-8"))
            name = meta.get("name") or f.stem
            if NAME.match(name):
                self._builtin[name] = {"name": name, "description": meta.get("description", ""), "body": body}

    async def _overrides(self) -> Dict[str, Dict[str, Any]]:
        return {r["name"]: dict(r) for r in await self.db.fetchall("SELECT name, description, body, updated_by, updated_at FROM skill_overrides")}

    # ---- a user's own skills ---------------------------------------------------------------------
    async def mine(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT name, description, body, updated_at FROM user_skills WHERE user_id=? ORDER BY name", (user_id,))
        return [dict(r) for r in rows]

    async def save_mine(self, user_id: str, name: str, description: str, body: str) -> Dict[str, Any]:
        name, description, body = (name or "").strip().lower(), " ".join((description or "").split()), (body or "").strip()
        if not NAME.match(name):
            raise SkillError("The name must be 2 to 40 characters: lowercase letters, digits and dashes.")
        if not description or len(description) > MAX_DESCRIPTION:
            raise SkillError(f"Write one line saying when to use it (up to {MAX_DESCRIPTION} characters).")
        if not body or len(body) > MAX_MINE_BODY:
            raise SkillError(f"The instructions must be between 1 and {MAX_MINE_BODY} characters.")
        have = {r["name"] for r in await self.mine(user_id)}
        if name not in have and len(have) >= MAX_MINE:
            raise SkillError(f"You can keep up to {MAX_MINE} skills. Delete one first.")
        await self.db.execute("INSERT INTO user_skills (user_id, name, description, body, updated_at) VALUES (?,?,?,?,?) "
                              "ON CONFLICT(user_id, name) DO UPDATE SET description=excluded.description, body=excluded.body, updated_at=excluded.updated_at",
                              (user_id, name, description, body, self.clock()))
        return {"name": name, "description": description, "body": body}

    async def import_mine(self, user_id: str, text: str) -> Dict[str, Any]:
        """A skill from the text of a markdown file: name and description in front matter, instructions below."""
        meta, body = parse(text or "")
        return await self.save_mine(user_id, meta.get("name", ""), meta.get("description", ""), body)

    async def delete_mine(self, user_id: str, name: str) -> bool:
        return await self.db.execute("DELETE FROM user_skills WHERE user_id=? AND name=?", (user_id, name)) > 0

    async def list(self) -> List[Dict[str, Any]]:
        over = await self._overrides()
        out = []
        for name in sorted(set(self._builtin) | set(over)):
            o, b = over.get(name), self._builtin.get(name)
            out.append({"name": name, "description": (o or b)["description"], "source": ("edited" if b else "added") if o else "built-in",
                        "updated_at": o["updated_at"] if o else 0, "size": len((o or b)["body"])})
        return out

    async def catalog(self, user_id: str = "") -> List[Tuple[str, str]]:
        """(name, description) of the shared skills, with the user's own on top (theirs replace shared ones of the same name)."""
        mine = {m["name"]: m for m in await self.mine(user_id)} if user_id else {}
        shared = [(s["name"], s["description"]) for s in await self.list() if s["name"] not in mine]
        return [(n, f"(your own skill) {m['description']}") for n, m in mine.items()] + shared

    async def get(self, name: str, user_id: str = "") -> Optional[Dict[str, Any]]:
        if user_id:
            own = await self.db.fetchone("SELECT name, description, body FROM user_skills WHERE user_id=? AND name=?", (user_id, name))
            if own:
                return {**dict(own), "source": "yours"}
        row = await self.db.fetchone("SELECT name, description, body FROM skill_overrides WHERE name=?", (name,))
        got = dict(row) if row else self._builtin.get(name)
        if not got:
            return None
        return {**got, "source": ("edited" if name in self._builtin else "added") if row else "built-in"}

    async def save(self, name: str, description: str, body: str, by: str) -> Dict[str, Any]:
        name, description, body = (name or "").strip().lower(), (description or "").strip(), (body or "").strip()
        check(name, description, body)
        await self.db.execute("INSERT INTO skill_overrides (name, description, body, updated_by, updated_at) VALUES (?,?,?,?,?) "
                              "ON CONFLICT(name) DO UPDATE SET description=excluded.description, body=excluded.body, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                              (name, description, body, by, self.clock()))
        return await self.get(name)

    async def revert(self, name: str) -> bool:
        """Drop the edit: a built-in skill goes back to its file, an added one disappears."""
        return await self.db.execute("DELETE FROM skill_overrides WHERE name=?", (name,)) > 0
