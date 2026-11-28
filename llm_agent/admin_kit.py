"""The admin dashboard every app gets, built by the platform and not by the assistant.

The assistant only describes what the app manages (`admin.json`: collections and their fields) and gives the app a small JSON API under /admin-api.
The dashboard (web_ui/admin_app.html) draws the screens from that description: lists, add, edit, delete, pictures and videos, like the admin of a
professional app. The owner opens it with Open admin in Manage app (signed session, admin_session.py); the platform forwards each request to the app
with the X-Workbench-Admin header, which the app trusts instead of its own login.

admin.json:
  {"title": "Chai Corner admin", "collections": [{"name": "dishes", "label": "Dishes", "title": "name",
      "fields": [{"key": "name", "label": "Name", "type": "text", "required": true}, {"key": "price", "type": "price"}, {"key": "photo", "type": "image"}, ...]}]}
The app's API for every collection (JSON, all of it guarded by the X-Workbench-Admin header):
  GET    /admin-api/<collection>        -> {"items": [{"id": ..., <fields>}]}
  POST   /admin-api/<collection>        -> the created item (with its id)
  PUT    /admin-api/<collection>/<id>   -> the updated item
  DELETE /admin-api/<collection>/<id>   -> {"ok": true}
Pictures and videos are uploaded to the platform, which returns a public address that the app stores in the field (`image`, `video`, `file` fields).
"""
import json
import re
from typing import Any, Dict, List, Optional

TYPES = ("text", "longtext", "number", "price", "toggle", "select", "date", "url", "color", "image", "video", "file")
MAX_COLLECTIONS, MAX_FIELDS = 20, 40
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
ITEM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# uploads: extension -> content type (no html, svg or scripts: an upload can never run code on our address)
MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif", "mp4": "video/mp4", "webm": "video/webm",
         "mov": "video/quicktime", "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "pdf": "application/pdf"}
MAX_IMAGE, MAX_VIDEO, MAX_APP_MEDIA = 8 * 1024 * 1024, 150 * 1024 * 1024, 500 * 1024 * 1024


def _text(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def parse(raw: Optional[bytes]) -> Dict[str, Any]:
    """The validated dashboard description from admin.json, or {} when the project has none. Never raises."""
    try:
        data = json.loads((raw or b"{}").decode("utf-8", "replace")[:60_000])
    except ValueError:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("collections"), list):
        return {}
    cols: List[Dict[str, Any]] = []
    for c in data["collections"][:MAX_COLLECTIONS]:
        if not isinstance(c, dict) or not NAME.fullmatch(str(c.get("name", ""))) or any(x["name"] == c["name"] for x in cols):
            continue
        fields: List[Dict[str, Any]] = []
        for f in (c.get("fields") or [])[:MAX_FIELDS]:
            if not isinstance(f, dict) or not NAME.fullmatch(str(f.get("key", "")).lower()) or str(f.get("type", "text")) not in TYPES or any(x["key"] == f["key"] for x in fields):
                continue
            field = {"key": str(f["key"]), "label": _text(f.get("label"), 40) or str(f["key"]).replace("_", " ").capitalize(), "type": str(f.get("type", "text")),
                     "required": bool(f.get("required"))}
            if field["type"] == "select":
                field["options"] = [_text(o, 40) for o in (f.get("options") or [])[:30] if _text(o, 40)]
            if f.get("help"):
                field["help"] = _text(f["help"], 120)
            fields.append(field)
        if fields:
            title = str(c.get("title", ""))
            cols.append({"name": c["name"], "label": _text(c.get("label"), 40) or c["name"].replace("_", " ").capitalize(),
                         "title": title if any(f["key"] == title for f in fields) else fields[0]["key"], "fields": fields})
    return {"title": _text(data.get("title"), 60), "collections": cols} if cols else {}
