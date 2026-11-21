"""A user's screen designs: saving them, starting from a template, and showing one as an app's screens."""
import asyncio
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_agent import design_schema as ds, imagefit
from llm_agent.apps import AppError, AppStore
from llm_agent.db import Database

TEMPLATE_DIR = Path(__file__).parent / "design_templates"
META_KEY = "design_id"            # kept in the app's meta: which design shows as its screens
NAME_MAX = 40
MAX_IMAGES = 60                    # pictures one user may keep for designs


class Designs:
    def __init__(self, db: Database, apps: AppStore, limit: Callable[[], int] = lambda: 20, clock: Callable[[], float] = time.time):
        self.db, self.apps, self.limit, self.clock = db, apps, limit, clock

    # ---- ready-made templates (files shipped with the system) --------------------------------------
    @staticmethod
    def templates() -> List[Dict[str, Any]]:
        out = []
        for f in sorted(TEMPLATE_DIR.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({"key": f.stem, "name": data["name"], "description": data["description"], "design": data["design"],
                        "pages": [p["title"] for p in data["design"]["pages"]]})
        return out

    def template(self, key: str) -> Dict[str, Any]:
        found = next((t for t in self.templates() if t["key"] == key), None)
        if not found:
            raise AppError("That template does not exist.", 404)
        return found

    # ---- a user's own designs -----------------------------------------------------------------------
    @staticmethod
    def _name(name: str) -> str:
        return " ".join((name or "").split())[:NAME_MAX] or "My design"

    async def _row(self, owner: str, design_id: str) -> Dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM designs WHERE design_id=? AND owner_id=?", (design_id, owner))
        if not row:
            raise AppError("That design was not found.", 404)
        return dict(row)

    async def list(self, owner: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT design_id, name, body, updated_at FROM designs WHERE owner_id=? ORDER BY updated_at DESC", (owner,))
        return [{"id": r["design_id"], "name": r["name"], "updated_at": r["updated_at"], "pages": len(json.loads(r["body"])["pages"]),
                 "apps": await self.apps.with_meta(META_KEY, r["design_id"])} for r in rows]

    async def get(self, owner: str, design_id: str) -> Dict[str, Any]:
        row = await self._row(owner, design_id)
        return {"id": design_id, "name": row["name"], "design": self._load(row["body"]), "apps": await self.apps.with_meta(META_KEY, design_id)}

    async def create(self, owner: str, name: str, template: Optional[str] = None) -> Dict[str, Any]:
        count = (await self.db.fetchone("SELECT COUNT(*) AS n FROM designs WHERE owner_id=?", (owner,)))["n"]
        if count >= self.limit():
            raise AppError(f"You can keep up to {self.limit()} designs. Delete one you no longer need.", 409)
        if template:
            found = self.template(template)
            body, name = found["design"], name or found["name"]
        else:
            body = {"pages": [{"id": "home", "title": "Home", "blocks": []}]}
        body = ds.clean(body)  # also gives a blank design its default theme
        design_id, now = secrets.token_hex(6), self.clock()
        await self.db.execute("INSERT INTO designs (design_id, owner_id, name, body, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                              (design_id, owner, self._name(name), json.dumps(body), now, now))
        return await self.get(owner, design_id)

    async def save(self, owner: str, design_id: str, name: str, design: Any) -> Dict[str, Any]:
        await self._row(owner, design_id)
        try:
            body = ds.clean(design)
        except ds.DesignError as e:
            raise AppError(str(e))
        await self.db.execute("UPDATE designs SET name=?, body=?, updated_at=? WHERE design_id=?", (self._name(name), json.dumps(body), self.clock(), design_id))
        return await self.get(owner, design_id)

    async def delete(self, owner: str, design_id: str) -> None:
        await self._row(owner, design_id)
        for app_id in await self.apps.with_meta(META_KEY, design_id):
            await self.apps.set_meta(app_id, META_KEY, None)
        await self.db.execute("DELETE FROM designs WHERE design_id=?", (design_id,))

    async def forget_user(self, owner: str) -> None:
        for d in await self.list(owner):
            await self.delete(owner, d["id"])
        for r in await self.db.fetchall("SELECT name FROM design_images WHERE owner_id=?", (owner,)):
            (self.image_dir / r["name"]).unlink(missing_ok=True)
        await self.db.execute("DELETE FROM design_images WHERE owner_id=?", (owner,))

    # ---- pictures for designs ---------------------------------------------------------------------------
    @property
    def image_dir(self) -> Path:
        return self.apps.root.parent / "design_images"

    async def save_image(self, owner: str, data: bytes) -> str:
        """Fit an uploaded picture (imagefit) and keep it for the owner. Returns the address a design block can use."""
        n = (await self.db.fetchone("SELECT COUNT(*) AS n FROM design_images WHERE owner_id=?", (owner,)))["n"]
        if n >= MAX_IMAGES:
            raise AppError(f"You can keep {MAX_IMAGES} pictures for your designs. Delete a design that uses old ones, or reuse a picture.", 409)
        try:
            fitted, kind = await asyncio.to_thread(imagefit.photo, data)
        except imagefit.ImageError as e:
            raise AppError(e.message, e.status)
        name = f"{secrets.token_hex(8)}.{kind}"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((self.image_dir / name).write_bytes, fitted)
        await self.db.execute("INSERT INTO design_images (name, owner_id, created_at) VALUES (?,?,?)", (name, owner, time.time()))
        return f"/api/designs/img/{name}"

    def image_path(self, name: str) -> Optional[Path]:
        path = self.image_dir / name
        return path if re.match(r"^[0-9a-f]{16}\.webp$", name or "") and path.is_file() else None

    # ---- showing a design as an app's screens -----------------------------------------------------------
    async def apply(self, owner: str, design_id: str, app_id: str) -> None:
        await self._row(owner, design_id)
        app = await self.apps.get(app_id)
        if app["owner_id"] != owner:
            raise AppError("Unknown app id.", 404)
        await self.apps.set_meta(app_id, META_KEY, design_id)
        # blocks that list a section this app does not have would stay empty: point them at one it has
        names = [sec["name"] for sec in (await self.apps.schema(app_id)).get("sections", [])]
        design = await self.get(owner, design_id)
        if ds.retarget_items(design["design"], names):
            await self.save(owner, design_id, design["name"], design["design"])

    async def unapply(self, owner: str, app_id: str) -> None:
        app = await self.apps.get(app_id)
        if app["owner_id"] != owner:
            raise AppError("Unknown app id.", 404)
        await self.apps.set_meta(app_id, META_KEY, None)

    @staticmethod
    def _load(body: str) -> Dict[str, Any]:
        """A stored design, brought up to the current format (designs saved before a new setting existed get its default)."""
        raw = json.loads(body)
        try:
            return ds.clean(raw)
        except ds.DesignError:
            return raw

    async def design_id_for(self, app_id: str) -> Optional[str]:
        app = await self.apps.get(app_id)
        return json.loads(app["meta"] or "{}").get(META_KEY) or None

    async def for_app(self, app_id: str) -> Optional[Dict[str, Any]]:
        """The design an app shows, for the public viewer."""
        app = await self.apps.get(app_id)
        design_id = json.loads(app["meta"] or "{}").get(META_KEY)
        row = await self.db.fetchone("SELECT body FROM designs WHERE design_id=?", (design_id,)) if design_id else None
        return self._load(row["body"]) if row else None

    async def linked(self, app_id: str) -> bool:
        return await self.for_app(app_id) is not None
