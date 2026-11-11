"""Build app / Manage app: publish a user's front-end app (a zip) under a public link, and let its owner update it later
with the app id + password issued when it was built.

Only static files are hosted (HTML, CSS, JavaScript, images, ...). Visitors see the app inside a sandboxed frame, so it can never
touch the workbench, its login or its API.
"""
from urllib.parse import urlparse
import asyncio
import hashlib
import io
import json
import os
import mimetypes
import posixpath
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_agent import appkit
from llm_agent.db import Database
from llm_agent.security import LoginThrottle, hash_password, verify_password
from llm_agent.preview import STORAGE_SHIM

ID_RE = re.compile(r"^[a-z0-9]{10}$")
COOLDOWN_SECONDS = 60
MAX_DEVICES = 50_000
MAX_SUBMISSIONS = 5000
SUBMIT_PER_HOUR = 30
MAX_SUBMISSION_BYTES = 8 * 1024
DEFAULT_SETTINGS = {"title": "", "accent": "", "announcement": "", "backend_url": ""}
DEVICE_RE = re.compile(r"^[0-9a-f]{24}$")
ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
MAX_FILES = 2000
SKIP_DIRS = {"__macosx", ".git", ".svn", "node_modules", "__pycache__", ".idea", ".vscode"}
SKIP_NAMES = {".ds_store", "thumbs.db"}
SKIP_SUFFIXES = (".pem", ".key", ".pyc", ".p12", ".pfx")
BACKEND_HINTS = {"app.py", "server.py", "main.py", "server.js", "app.js", "manage.py", "requirements.txt", "package.json"}


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _normalize_password(p: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (p or "").lower())


def _new_password() -> str:
    raw = "".join(secrets.choice(ID_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise AppError("Give your app a name.")
    return name


class AppStore:
    def __init__(self, db: Database, root: Path, limits: Callable[[], Tuple[int, int]]):
        """limits() -> (max apps per user, max bytes per app)."""
        self.db, self.root, self._limits = db, root, limits
        self.root.mkdir(parents=True, exist_ok=True)
        self.throttle = LoginThrottle(max_failures=6, window_seconds=600)
        self.clock = time.time          # replaced in tests

    def _dir(self, app_id: str) -> Path:
        if not ID_RE.match(app_id or ""):
            raise AppError("Unknown app id.", 404)
        return self.root / app_id

    # ---- zip handling ----------------------------------------------------------------------------
    def _extract(self, data: bytes, dest: Path, max_bytes: int) -> Dict[str, Any]:
        if len(data) > max_bytes:
            raise AppError(f"The zip is larger than the {max_bytes // (1024 * 1024)} MB limit.", 413)
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise AppError("That is not a valid .zip file.")
        members = []
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
                raise AppError(f"The zip contains an unsafe path ({info.filename}).")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # symlink
            parts = [p for p in name.split("/") if p]
            if not parts or any(p.lower() in SKIP_DIRS for p in parts[:-1]) or parts[-1].lower() in SKIP_NAMES \
                    or parts[-1].lower().startswith(".env") or parts[-1].lower().endswith(SKIP_SUFFIXES):
                members.append((info, None))
                continue
            members.append((info, parts))
        kept = [(i, p) for i, p in members if p]
        skipped = len(members) - len(kept)
        if not kept:
            raise AppError("The zip has no files in it.")
        if len(kept) > MAX_FILES:
            raise AppError(f"The app has more than {MAX_FILES} files.")
        if sum(i.file_size for i, _ in kept) > max_bytes:
            raise AppError(f"The app is larger than {max_bytes // (1024 * 1024)} MB once unzipped.", 413)
        # a single top-level folder around everything is removed (zipping a folder usually creates one)
        if all(len(p) > 1 for _, p in kept) and len({p[0] for _, p in kept}) == 1:
            kept = [(i, p[1:]) for i, p in kept]
        names = {"/".join(p).lower() for _, p in kept}
        if "index.html" not in names:
            hint = (" It looks like it contains server code (" + ", ".join(sorted(n for n in names if n in BACKEND_HINTS)[:3]) +
                    "), which cannot run here. Only front-end apps (HTML, CSS, JavaScript) can be built.") if names & BACKEND_HINTS else ""
            raise AppError("The zip needs an index.html at its top level (the page that opens first)." + hint)
        dest.mkdir(parents=True)
        written = 0
        for info, parts in kept:
            target = dest.joinpath(*parts)
            if not target.resolve().is_relative_to(dest.resolve()):
                raise AppError("The zip contains an unsafe path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(65536):
                    written += len(chunk)
                    if written > max_bytes:
                        raise AppError(f"The app is larger than {max_bytes // (1024 * 1024)} MB once unzipped.", 413)
                    out.write(chunk)
        return {"files": len(kept), "size_bytes": written, "skipped": skipped}

    # ---- lifecycle -------------------------------------------------------------------------------
    async def build(self, owner: str, name: str, data: bytes) -> Dict[str, Any]:
        name = _clean_name(name)
        max_apps, max_bytes = self._limits()
        mine = await self.db.fetchone("SELECT COUNT(*) AS n FROM published_apps WHERE owner_id=?", (owner,))
        if mine["n"] >= max_apps:
            raise AppError(f"You can have {max_apps} apps. Delete one in Manage app first.", 409)
        app_id = next(i for i in ("".join(secrets.choice(ID_ALPHABET) for _ in range(10)) for _ in range(50)) if not (self.root / i).exists())
        tmp = self.root / f".tmp-{app_id}"
        try:
            stats = await asyncio.to_thread(self._extract, data, tmp, max_bytes)
            (self.root / app_id).mkdir()
            tmp.rename(self.root / app_id / "current")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        password = _new_password()
        now = time.time()
        await self.db.execute(
            "INSERT INTO published_apps(app_id, owner_id, name, password_hash, version, size_bytes, files, has_previous, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (app_id, owner, name, await asyncio.to_thread(hash_password, _normalize_password(password)), 1, stats["size_bytes"], stats["files"], 0, now, now))
        return {"app_id": app_id, "password": password, "name": name, **stats}

    async def get(self, app_id: str) -> Dict[str, Any]:
        self._dir(app_id)
        row = await self.db.fetchone("SELECT * FROM published_apps WHERE app_id=?", (app_id,))
        if not row:
            raise AppError("Unknown app id.", 404)
        return dict(row)

    async def authenticate(self, app_id: str, password: str, client: str = "") -> Dict[str, Any]:
        """Check app id + password. Same message for an unknown id and a wrong password."""
        key = f"{client}|{app_id}"
        if self.throttle.blocked(key):
            raise AppError("Too many wrong attempts. Wait ten minutes and try again.", 429)
        app_id = (app_id or "").strip().lower()
        row = await self.db.fetchone("SELECT * FROM published_apps WHERE app_id=?", (app_id,)) if ID_RE.match(app_id) else None
        ok = await asyncio.to_thread(verify_password, _normalize_password(password), row["password_hash"] if row else hash_password("x"))
        if not row or not ok:
            self.throttle.record_failure(key)
            raise AppError("That app id and password do not match.", 401)
        self.throttle.reset(key)
        return dict(row)

    async def update(self, app_id: str, data: bytes) -> Dict[str, Any]:
        row = await self.get(app_id)
        if row["kind"] != "web":
            raise AppError("This is an Android / Windows app. Use “Change the app itself” in Manage app instead.", 409)
        _, max_bytes = self._limits()
        base = self._dir(app_id)
        tmp = base / ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            stats = await asyncio.to_thread(self._extract, data, tmp, max_bytes)
            prev = base / "previous"
            shutil.rmtree(prev, ignore_errors=True)
            (base / "current").rename(prev)
            tmp.rename(base / "current")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        now = time.time()
        await self.db.execute("UPDATE published_apps SET version=version+1, size_bytes=?, files=?, has_previous=1, updated_at=? WHERE app_id=?",
                              (stats["size_bytes"], stats["files"], now, app_id))
        return {**stats, "version": row["version"] + 1}

    async def rollback(self, app_id: str) -> Dict[str, Any]:
        row = await self.get(app_id)
        if row["kind"] != "web":
            raise AppError("Going back to an earlier version only applies to apps with a web link.", 409)
        base = self._dir(app_id)
        if not row["has_previous"] or not (base / "previous").is_dir():
            raise AppError("There is no earlier version to go back to.", 409)
        swap = base / ".swap"
        shutil.rmtree(swap, ignore_errors=True)
        (base / "current").rename(swap)
        (base / "previous").rename(base / "current")
        swap.rename(base / "previous")  # going back twice returns to the newer one
        size = sum(f.stat().st_size for f in (base / "current").rglob("*") if f.is_file())
        count = sum(1 for f in (base / "current").rglob("*") if f.is_file())
        await self.db.execute("UPDATE published_apps SET version=version+1, size_bytes=?, files=?, updated_at=? WHERE app_id=?",
                              (size, count, time.time(), app_id))
        return {"version": row["version"] + 1}

    async def delete(self, app_id: str) -> None:
        self._dir(app_id)
        await self.db.execute("DELETE FROM published_apps WHERE app_id=?", (app_id,))
        await self.db.execute("DELETE FROM app_items WHERE app_id=?", (app_id,))
        await self.db.execute("DELETE FROM app_submissions WHERE app_id=?", (app_id,))
        await self.db.execute("DELETE FROM app_devices WHERE app_id=?", (app_id,))
        await self.db.execute("DELETE FROM app_builds WHERE app_id=?", (app_id,))
        shutil.rmtree(self.root / app_id, ignore_errors=True)
        shutil.rmtree(self.root.parent / "shell_builds" / app_id, ignore_errors=True)

    async def reset_password(self, app_id: str) -> str:
        password = _new_password()
        await self.db.execute("UPDATE published_apps SET password_hash=? WHERE app_id=?",
                              (await asyncio.to_thread(hash_password, _normalize_password(password)), app_id))
        return password

    async def list_for(self, owner: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM published_apps WHERE owner_id=? ORDER BY created_at DESC", (owner,))
        return [self.public_info(dict(r)) for r in rows]

    async def list_all(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM published_apps ORDER BY created_at DESC")
        return [{**self.public_info(dict(r)), "owner": r["owner_id"]} for r in rows]

    async def delete_owner(self, owner: str) -> None:
        for r in await self.db.fetchall("SELECT app_id FROM published_apps WHERE owner_id=?", (owner,)):
            await self.delete(r["app_id"])

    @staticmethod
    def public_info(row: Dict[str, Any]) -> Dict[str, Any]:
        kit = row.get("kind") == "kit"
        meta = json.loads(row.get("meta") or "{}") if kit else {}
        return {"app_id": row["app_id"], "name": row["name"], "version": row["version"], "size_bytes": row["size_bytes"],
                "files": row["files"], "has_previous": bool(row["has_previous"]), "created_at": row["created_at"],
                "updated_at": row["updated_at"], "kind": row.get("kind", "web"), "url": None if kit else f"/a/{row['app_id']}/",
                "kit": meta or None}

    def count_files(self) -> int:
        return sum(1 for _ in self.root.glob("??????????"))

    # ---- managed apps (Android / Windows shell) --------------------------------------------------------------------
    @staticmethod
    def _new_id(root: Path) -> str:
        return next(i for i in ("".join(secrets.choice(ID_ALPHABET) for _ in range(10)) for _ in range(50)) if not (root / i).exists())

    def _write_files(self, row_id: str, logo: bytes, project_zip: bytes) -> None:
        base = self.root / row_id
        base.mkdir(exist_ok=True)
        (base / "logo.png").write_bytes(logo)
        if project_zip:
            (base / "project.zip").write_bytes(project_zip)

    async def create_app(self, owner: str, name: str, version: str, api_base: str, project_zip: bytes, logo: Optional[bytes]) -> Dict[str, Any]:
        """A managed app: App ID + password, logo, optional backend project. The Android / Windows shell is built later (ci_build)."""
        try:
            name, version = appkit.check_name(name), appkit.check_version(version)
            max_apps, max_bytes = self._limits()
            mine = await self.db.fetchone("SELECT COUNT(*) AS n FROM published_apps WHERE owner_id=?", (owner,))
            if mine["n"] >= max_apps:
                raise AppError(f"You can have {max_apps} apps. Delete one in Manage app first.", 409)
            logo = appkit.check_logo(logo) if logo else None
            project = await asyncio.to_thread(appkit.read_project, project_zip, max_bytes) if project_zip else None
            app_id = self._new_id(self.root)
            custom_logo = bool(logo)
            logo = logo or appkit.default_logo(app_id)
            await asyncio.to_thread(self._write_files, app_id, logo, project_zip)
        except appkit.KitError as e:
            raise AppError(e.message, e.status)
        self._write_snapshot(app_id, {"revision": 0, "published_at": 0, "effective_at": 0, "digest": self._digest({}, DEFAULT_SETTINGS), "collections": {}, "settings": DEFAULT_SETTINGS})
        password, now = _new_password(), time.time()
        stats = {"size_bytes": len(project_zip or b""), "files": len(project["files"]) if project else 0}
        meta = {"kit_version": version, "custom_logo": custom_logo, "builder": project["builder"] if project else None,
                "has_project": bool(project), "api_base": api_base, "settings": dict(DEFAULT_SETTINGS)}
        await self.db.execute(
            "INSERT INTO published_apps(app_id, owner_id, name, password_hash, version, size_bytes, files, has_previous, created_at, updated_at, kind, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (app_id, owner, name, await asyncio.to_thread(hash_password, _normalize_password(password)), 1, stats["size_bytes"], stats["files"], 0, now, now,
             "kit", json.dumps(meta)))     # kind "kit" = a managed app (the name is kept so existing databases keep working)
        return {"app_id": app_id, "password": password, "name": name, "kit": meta, "warnings": self._warnings(project), **stats}

    @staticmethod
    def _warnings(project: Optional[Dict[str, Any]]) -> List[str]:
        out = []
        if project and project["skipped"]:
            out.append(f"{project['skipped']} file(s) were left out (caches, secrets, build folders).")
        return out

    async def update_app(self, app_id: str, version: str, project_zip: Optional[bytes], logo: Optional[bytes], name: Optional[str]) -> Dict[str, Any]:
        """New name, logo, backend files or version. The shell app must be built again to show a new name or icon."""
        row = await self.get(app_id)
        if row["kind"] != "kit":
            raise AppError("Only managed apps can be updated here.", 409)
        meta = json.loads(row["meta"] or "{}")
        try:
            version = appkit.check_version(version)
            if tuple(map(int, version.split("."))) <= tuple(map(int, meta["kit_version"].split("."))):
                raise appkit.KitError(f"The new version must be higher than the current one ({meta['kit_version']}).")
            name = appkit.check_name(name) if name else row["name"]
            _, max_bytes = self._limits()
            project = await asyncio.to_thread(appkit.read_project, project_zip, max_bytes) if project_zip else None
            if logo:
                logo_bytes, custom = appkit.check_logo(logo), True
            else:
                logo_bytes, custom = (self.root / app_id / "logo.png").read_bytes(), meta.get("custom_logo", False)
            await asyncio.to_thread(self._write_files, app_id, logo_bytes, project_zip or b"")
        except appkit.KitError as e:
            raise AppError(e.message, e.status)
        meta.update(kit_version=version, custom_logo=custom)
        if project:
            meta.update(builder=project["builder"], has_project=True)
        size, count = (len(project_zip), len(project["files"])) if project else (row["size_bytes"], row["files"])
        await self.db.execute("UPDATE published_apps SET name=?, meta=?, version=version+1, size_bytes=?, files=?, updated_at=? WHERE app_id=?",
                              (name, json.dumps(meta), size, count, time.time(), app_id))
        return {"app": self.public_info(await self.get(app_id)), "warnings": self._warnings(project)}

    def backend_bundle(self, row: Dict[str, Any], api_base: str) -> bytes:
        """The owner's backend project plus the helper and deploy notes, ready to host."""
        try:
            project = appkit.read_project((self.root / row["app_id"] / "project.zip").read_bytes(), 10 ** 9)
        except OSError:
            raise AppError("No backend project was added to this app. Add one under Update details.", 404)
        return appkit.backend_bundle(row["name"], row["app_id"], api_base, project["files"])

    # ---- publishing: draft edits become visible to installed apps only when the owner presses Update --------
    @staticmethod
    def _digest(collections: Dict[str, Any], settings: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps([collections, settings], sort_keys=True).encode()).hexdigest()[:16]

    def _snap(self, app_id: str, prev: bool = False) -> Path:
        return self._dir(app_id) / ("published.prev.json" if prev else "published.json")

    def _write_snapshot(self, app_id: str, snap: Dict[str, Any]) -> None:
        path = self.root / app_id / "published.json"
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap))
        tmp.replace(path)

    def _read_snapshot(self, app_id: str, prev: bool = False) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._snap(app_id, prev).read_text())
        except (OSError, ValueError):
            return None

    async def _draft(self, app_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM app_items WHERE app_id=? ORDER BY collection, id", (app_id,))
        cols: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            cols.setdefault(r["collection"], []).append({"title": r["title"], "description": r["description"], "price": r["price"],
                                                         "image": r["image"], "in_stock": bool(r["in_stock"])})
        meta = json.loads((await self.get(app_id))["meta"] or "{}")
        return cols, {**DEFAULT_SETTINGS, **meta.get("settings", {})}

    async def publish(self, app_id: str) -> Dict[str, Any]:
        """Make the draft live for installed apps after a short cool-down, so every phone can pick it up cleanly."""
        cols, settings = await self._draft(app_id)
        now = self.clock()
        cur = self._read_snapshot(app_id) or {"revision": 0, "effective_at": 0, "digest": ""}
        digest = self._digest(cols, settings)
        if digest == cur["digest"]:
            raise AppError("Nothing to update: your apps already show these products and settings.", 409)
        if cur["effective_at"] <= now:                      # the current version is live: keep it as the fallback
            if self._snap(app_id).exists():
                shutil.copyfile(self._snap(app_id), self._snap(app_id, prev=True))
        snap = {"revision": cur["revision"] + 1, "published_at": now, "effective_at": now + COOLDOWN_SECONDS, "digest": digest,
                "collections": cols, "settings": settings}
        self._write_snapshot(app_id, snap)
        return {"revision": snap["revision"], "seconds_left": COOLDOWN_SECONDS}

    def serving(self, app_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """(the snapshot customers see now, update info). While a new revision cools down, the old one is still served."""
        now = self.clock()
        cur = self._read_snapshot(app_id) or {"revision": 0, "effective_at": 0, "collections": {}, "settings": dict(DEFAULT_SETTINGS), "published_at": 0}
        if cur["effective_at"] > now:
            old = self._read_snapshot(app_id, prev=True) or {"revision": cur["revision"] - 1, "collections": {}, "settings": dict(DEFAULT_SETTINGS), "published_at": 0}
            return old, {"pending": True, "seconds_left": max(1, int(cur["effective_at"] - now + 0.999)), "next_revision": cur["revision"]}
        return cur, {"pending": False, "seconds_left": 0}

    def catalog(self, app_id: str, collection: str, base: str) -> Dict[str, Any]:
        snap, update = self.serving(app_id)
        items = []
        for n, it in enumerate(snap["collections"].get(self._collection(collection), []), 1):
            img = it["image"]
            items.append({"id": n, "title": it["title"], "description": it["description"], "price": it["price"],
                          "image_url": (base + img) if img.startswith("/") else img, "in_stock": it["in_stock"]})
        return {"collection": collection, "revision": snap["revision"], "items": items, "update": update,
                "settings": {**DEFAULT_SETTINGS, **snap.get("settings", {})}}

    def logo_path(self, app_id: str):
        p = self.root / app_id / "logo.png"
        return p if re.fullmatch(r"[a-z0-9]{3,32}", app_id or "") and p.is_file() else None

    async def save_settings(self, app_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        title = " ".join(str(data.get("title") or "").split())[:30]
        accent = str(data.get("accent") or "").strip()
        if accent and not re.match(r"^#[0-9a-fA-F]{6}$", accent):
            raise AppError("The colour must look like #2563eb.")
        backend = str(data.get("backend_url") or "").strip()
        if backend:
            u = urlparse(backend)
            if u.scheme != "https" or not u.hostname or u.username or u.password or len(backend) > 300:
                raise AppError("The app address must start with https:// and have no user name or password in it.")
        settings = {"title": title, "accent": accent, "announcement": str(data.get("announcement") or "").strip()[:200], "backend_url": backend}
        row = await self.get(app_id)
        meta = json.loads(row["meta"] or "{}")
        meta["settings"] = settings
        await self.db.execute("UPDATE published_apps SET meta=?, updated_at=? WHERE app_id=?", (json.dumps(meta), self.clock(), app_id))
        return settings

    # ---- what customers send back (orders, messages, sign-ups): the owner's inbox ---------------------
    async def add_submission(self, app_id: str, collection: str, device_id: str, data: Any) -> int:
        collection = self._collection(collection or "orders")
        if not DEVICE_RE.match(device_id or ""):
            raise AppError("This request needs the app's device id.", 400)
        if not isinstance(data, dict) or not data:
            raise AppError("Send an object with the fields to save, for example {\"name\": \"Ana\", \"item\": \"Tea\"}.")
        clean = {str(k)[:60]: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:1000]) for k, v in list(data.items())[:30]
                 if isinstance(v, (str, int, float, bool, type(None)))}
        raw = json.dumps(clean, ensure_ascii=False)
        if not clean or len(raw.encode()) > MAX_SUBMISSION_BYTES:
            raise AppError("That message is too big or empty.", 413)
        now = self.clock()
        recent = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_submissions WHERE app_id=? AND device_id=? AND created_at>=?", (app_id, device_id, now - 3600))
        if recent["n"] >= SUBMIT_PER_HOUR:
            raise AppError("Too many messages from this device. Try again later.", 429)
        total = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_submissions WHERE app_id=?", (app_id,))
        if total["n"] >= MAX_SUBMISSIONS:
            raise AppError("The owner's inbox is full. Please try again later.", 503)
        return await self.db.insert("INSERT INTO app_submissions(app_id, collection, device_id, data, status, created_at) VALUES (?,?,?,?, 'new', ?)",
                                    (app_id, collection, device_id, raw, now))

    async def submissions(self, app_id: str, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM app_submissions WHERE app_id=?", [app_id]
        if status in ("new", "done"):
            sql, args = sql + " AND status=?", args + [status]
        rows = await self.db.fetchall(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 200))))
        return [{"id": r["id"], "collection": r["collection"], "data": json.loads(r["data"]), "status": r["status"], "created_at": r["created_at"],
                 "from": r["device_id"][:6]} for r in rows]

    async def set_submission_status(self, app_id: str, sub_id: int, status: str) -> None:
        if status not in ("new", "done"):
            raise AppError("Status must be new or done.")
        if not await self.db.execute("UPDATE app_submissions SET status=? WHERE id=? AND app_id=?", (status, sub_id, app_id)):
            raise AppError("That message no longer exists.", 404)

    async def delete_submission(self, app_id: str, sub_id: int) -> None:
        if not await self.db.execute("DELETE FROM app_submissions WHERE id=? AND app_id=?", (sub_id, app_id)):
            raise AppError("That message no longer exists.", 404)

    # ---- who uses the app --------------------------------------------------------------------------
    async def touch_device(self, app_id: str, device_id: str, version: str, platform: str) -> None:
        """Count an install. Only a random id the app made for itself is stored: no IP address, name or account."""
        if not DEVICE_RE.match(device_id or ""):
            return
        version = version[:12] if re.match(r"^[0-9.]{1,12}$", version or "") else ""
        platform = platform if platform in ("apk", "exe") else ""
        now = self.clock()
        n = await self.db.execute("UPDATE app_devices SET last_seen=?, opens=opens+1, app_version=?, platform=? WHERE app_id=? AND device_id=?",
                                  (now, version, platform, app_id, device_id))
        if not n:
            count = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_devices WHERE app_id=?", (app_id,))
            if count["n"] < MAX_DEVICES:
                await self.db.execute("INSERT OR IGNORE INTO app_devices(app_id, device_id, first_seen, last_seen, opens, app_version, platform) VALUES (?,?,?,?,1,?,?)",
                                      (app_id, device_id, now, now, version, platform))

    async def usage(self, app_id: str) -> Dict[str, Any]:
        now, day = self.clock(), 86400
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total, COALESCE(SUM(opens),0) AS opens, COALESCE(SUM(last_seen>=?),0) AS d1, COALESCE(SUM(last_seen>=?),0) AS d7,"
            " COALESCE(SUM(last_seen>=?),0) AS d30, COALESCE(SUM(first_seen>=?),0) AS new7 FROM app_devices WHERE app_id=?",
            (now - day, now - 7 * day, now - 30 * day, now - 7 * day, app_id))
        versions = await self.db.fetchall("SELECT app_version AS v, COUNT(*) AS n FROM app_devices WHERE app_id=? GROUP BY app_version ORDER BY n DESC LIMIT 8", (app_id,))
        platforms = await self.db.fetchall("SELECT platform AS p, COUNT(*) AS n FROM app_devices WHERE app_id=? GROUP BY platform ORDER BY n DESC", (app_id,))
        return {"installs": row["total"], "opens": row["opens"], "active_24h": row["d1"], "active_7d": row["d7"], "active_30d": row["d30"],
                "new_7d": row["new7"], "versions": [{"version": r["v"] or "unknown", "devices": r["n"]} for r in versions],
                "platforms": [{"platform": r["p"] or "unknown", "devices": r["n"]} for r in platforms]}

    def project_contents(self, app_id: str) -> Dict[str, bytes]:
        """The stored backend project (path -> bytes); empty when the app has none."""
        try:
            return appkit.read_project((self.root / app_id / "project.zip").read_bytes(), 10 ** 9)["files"]
        except (OSError, appkit.KitError):
            return {}

    async def overview(self, app_id: str) -> Dict[str, Any]:
        row = await self.get(app_id)
        cols, settings = await self._draft(app_id)
        snap = self._read_snapshot(app_id) or {"revision": 0, "digest": "", "published_at": 0, "effective_at": 0}
        _, update = self.serving(app_id)
        images = self.root / app_id / "images"
        inbox = await self.db.fetchone("SELECT COUNT(*) AS total, COALESCE(SUM(status='new'),0) AS fresh FROM app_submissions WHERE app_id=?", (app_id,))
        return {"app": self.public_info(row), "usage": await self.usage(app_id), "inbox": {"total": inbox["total"], "new": inbox["fresh"]},
                "settings": settings, "images": sum(1 for _ in images.iterdir()) if images.is_dir() else 0,
                "collections": [{"name": k, "items": len(v)} for k, v in cols.items()],
                "publish": {"revision": snap["revision"], "published_at": snap["published_at"], "update": update,
                            "has_changes": self._digest(cols, settings) != snap["digest"], "cooldown": COOLDOWN_SECONDS}}

    # ---- catalogue (what the packed apps show; edited in Manage app) ----------------------------------
    MAX_ITEMS = 500
    MAX_IMAGES = 200
    IMAGE_MAX = 600 * 1024

    @staticmethod
    def _collection(name: str) -> str:
        name = (name or "products").strip().lower()
        if not re.match(r"^[a-z0-9_-]{1,30}$", name):
            raise AppError("A collection name uses 1 to 30 letters, numbers, - or _ (for example products or offers).")
        return name

    @staticmethod
    def _item_fields(d: Dict[str, Any]) -> Dict[str, Any]:
        title = " ".join(str(d.get("title") or "").split())[:120]
        if not title:
            raise AppError("Give the item a title.")
        price = d.get("price")
        if price in ("", None):
            price = None
        else:
            try:
                price = round(float(price), 2)
            except (TypeError, ValueError):
                raise AppError("The price must be a number, for example 19.99.")
            if not 0 <= price <= 1_000_000_000:
                raise AppError("The price is out of range.")
        image = str(d.get("image") or "").strip()[:500]
        if image and not re.match(r"^(https://|/api/catalog/)", image):
            raise AppError("An image must be an uploaded picture or an https:// address.")
        return {"title": title, "description": str(d.get("description") or "")[:2000], "price": price, "image": image,
                "in_stock": 1 if d.get("in_stock", True) else 0}

    async def collections(self, app_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT collection, COUNT(*) AS n FROM app_items WHERE app_id=? GROUP BY collection ORDER BY collection", (app_id,))
        return [{"name": r["collection"], "items": r["n"]} for r in rows]

    async def items(self, app_id: str, collection: str, base: str = "") -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM app_items WHERE app_id=? AND collection=? ORDER BY id", (app_id, self._collection(collection)))
        return [{"id": r["id"], "title": r["title"], "description": r["description"], "price": r["price"],
                 "image_url": (base + r["image"]) if r["image"].startswith("/") else r["image"], "image": r["image"],
                 "in_stock": bool(r["in_stock"])} for r in rows]

    async def save_item(self, app_id: str, collection: str, data: Dict[str, Any]) -> int:
        collection, f = self._collection(collection), self._item_fields(data)
        now = time.time()
        if data.get("id"):
            n = await self.db.execute("UPDATE app_items SET title=?, description=?, price=?, image=?, in_stock=?, updated_at=? WHERE id=? AND app_id=?",
                                      (f["title"], f["description"], f["price"], f["image"], f["in_stock"], now, int(data["id"]), app_id))
            if not n:
                raise AppError("That item no longer exists.", 404)
            item_id = int(data["id"])
        else:
            count = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_items WHERE app_id=?", (app_id,))
            if count["n"] >= self.MAX_ITEMS:
                raise AppError(f"An app can have {self.MAX_ITEMS} items. Delete some first.", 409)
            item_id = await self.db.insert("INSERT INTO app_items(app_id, collection, title, description, price, image, in_stock, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                           (app_id, collection, f["title"], f["description"], f["price"], f["image"], f["in_stock"], now))
        await self.db.execute("UPDATE published_apps SET updated_at=? WHERE app_id=?", (now, app_id))
        return item_id

    async def delete_item(self, app_id: str, item_id: int) -> None:
        if not await self.db.execute("DELETE FROM app_items WHERE id=? AND app_id=?", (item_id, app_id)):
            raise AppError("That item no longer exists.", 404)
        await self.db.execute("UPDATE published_apps SET updated_at=? WHERE app_id=?", (time.time(), app_id))

    @staticmethod
    def _image_type(data: bytes) -> Optional[str]:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        return None

    def save_image(self, app_id: str, data: bytes) -> str:
        kind = self._image_type(data)
        if not kind:
            raise AppError("Pictures must be PNG, JPG, WEBP or GIF.")
        if len(data) > self.IMAGE_MAX:
            raise AppError(f"That picture is {len(data) // 1024} KB. Please use one under {self.IMAGE_MAX // 1024} KB (resize it, or save it as JPG).", 413)
        folder = self._dir(app_id) / "images"
        folder.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in folder.iterdir()) >= self.MAX_IMAGES:
            raise AppError(f"An app can store {self.MAX_IMAGES} pictures.", 409)
        name = f"{secrets.token_hex(8)}.{kind}"
        (folder / name).write_bytes(data)
        return f"/api/catalog/{app_id}/images/{name}"

    def image_path(self, app_id: str, name: str) -> Optional[Path]:
        if not re.match(r"^[0-9a-f]{16}\.(png|jpg|webp|gif)$", name or "") or not ID_RE.match(app_id or ""):
            return None
        path = self.root / app_id / "images" / name
        return path if path.is_file() else None

    # ---- serving ---------------------------------------------------------------------------------
    def find_file(self, app_id: str, path: str) -> Optional[Path]:
        if not ID_RE.match(app_id or ""):
            return None
        base = (self.root / app_id / "current").resolve()
        clean = posixpath.normpath("/" + path.replace("\\", "/")).lstrip("/")
        target = (base / clean).resolve() if clean else base
        if not target.is_relative_to(base):
            return None
        if target.is_dir():
            target = target / "index.html"
        if target.is_file():
            return target
        if "." not in posixpath.basename(clean) and (base / "index.html").is_file():  # single-page apps: unknown routes -> index
            return base / "index.html"
        return None

    @staticmethod
    def render(target: Path) -> Tuple[bytes, str]:
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        if ctype == "text/html":
            text = data.decode("utf-8", errors="replace")
            low = text.lower()
            i = low.find("<head")
            i = low.find(">", i) + 1 if i != -1 else 0
            return (text[:i] + STORAGE_SHIM + text[i:]).encode("utf-8"), "text/html; charset=utf-8"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml", "application/javascript"):
            ctype += "; charset=utf-8"
        return data, ctype
