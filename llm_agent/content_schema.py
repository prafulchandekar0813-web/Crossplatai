"""What an app lets its owner manage: sections (like Products, Bookings, Posts) and the fields of each.

The AI describes them in the project's manage.json; the owner can change them freely in Manage app. Values of every
field are stored with the item, so changing the layout never loses data. Pure functions, no database."""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

TYPES = ("text", "longtext", "number", "money", "date", "yesno", "choice", "image", "link")
MAX_SECTIONS, MAX_FIELDS, MAX_OPTIONS = 12, 20, 20
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,30}$")
RESERVED = {"id", "image_url"}


class SchemaError(ValueError):
    pass


def _label(v: Any, what: str, limit: int = 40) -> str:
    out = " ".join(str(v or "").split())[:limit]
    if not out:
        raise SchemaError(f"{what} needs a name.")
    return out


def legacy_section(name: str = "products") -> Dict[str, Any]:
    """The layout every app had before layouts existed: what a shop needs."""
    return {"name": name, "label": name.replace("-", " ").replace("_", " ").title(), "item": "Item", "fields": [
        {"key": "title", "label": "Title", "type": "text", "required": True},
        {"key": "price", "label": "Price", "type": "money"},
        {"key": "description", "label": "Description", "type": "longtext"},
        {"key": "image", "label": "Picture", "type": "image"},
        {"key": "in_stock", "label": "In stock", "type": "yesno", "default": True}]}


def default_schema() -> Dict[str, Any]:
    s = legacy_section("products")
    s["label"], s["item"] = "Content", "item"
    return {"sections": [s]}


def clean(raw: Any) -> Dict[str, Any]:
    """Validate a layout from the AI or the owner and return it in canonical form. Raises SchemaError with a plain message."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise SchemaError("The layout is not valid JSON.")
    if isinstance(raw, dict):
        raw = raw.get("sections")
    if not isinstance(raw, list) or not raw:
        raise SchemaError("A layout needs at least one section.")
    if len(raw) > MAX_SECTIONS:
        raise SchemaError(f"At most {MAX_SECTIONS} sections.")
    sections, seen = [], set()
    for s in raw:
        if not isinstance(s, dict):
            raise SchemaError("Each section must be an object.")
        label = _label(s.get("label") or s.get("name"), "A section")
        name = str(s.get("name") or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"))[:30]
        if not NAME_RE.match(name):
            raise SchemaError(f"Section name {name!r} may use only lowercase letters, numbers, - and _.")
        if name in seen:
            raise SchemaError(f"Two sections are called {name!r}.")
        seen.add(name)
        fields_raw = s.get("fields")
        if not isinstance(fields_raw, list) or not fields_raw:
            raise SchemaError(f"Section {label!r} needs at least one field.")
        if len(fields_raw) > MAX_FIELDS:
            raise SchemaError(f"At most {MAX_FIELDS} fields per section.")
        fields, keys = [], set()
        for f in fields_raw:
            if not isinstance(f, dict):
                raise SchemaError(f"A field in {label!r} must be an object.")
            flabel = _label(f.get("label") or f.get("key"), f"A field in {label!r}")
            key = str(f.get("key") or re.sub(r"[^a-z0-9]+", "_", flabel.lower()).strip("_"))[:30]
            if not KEY_RE.match(key) or key in RESERVED:
                raise SchemaError(f"Field key {key!r} in {label!r} must start with a letter and use only lowercase letters, numbers and _.")
            if key in keys:
                raise SchemaError(f"{label!r} has two fields called {key!r}.")
            keys.add(key)
            ftype = f.get("type", "text")
            if ftype not in TYPES:
                raise SchemaError(f"Field {flabel!r}: type must be one of {', '.join(TYPES)}.")
            field: Dict[str, Any] = {"key": key, "label": flabel, "type": ftype, "required": bool(f.get("required"))}
            if ftype == "choice":
                opts = [" ".join(str(o).split())[:40] for o in (f.get("options") or []) if str(o).strip()]
                if not 2 <= len(opts) <= MAX_OPTIONS:
                    raise SchemaError(f"Field {flabel!r}: give 2 to {MAX_OPTIONS} options.")
                field["options"] = list(dict.fromkeys(opts))
            if ftype == "yesno" and "default" in f:
                field["default"] = bool(f["default"])
            fields.append(field)
        sections.append({"name": name, "label": label, "item": " ".join(str(s.get("item") or "").split())[:30] or "item", "fields": fields})
    return {"sections": sections}


def from_project(files: Dict[str, bytes]) -> Tuple[Optional[Dict[str, Any]], str]:
    """(layout, problem) from a project's manage.json: (None, "") when there is none, (None, why) when it is invalid."""
    body = files.get("manage.json")
    if body is None:
        return None, ""
    try:
        return clean(body.decode("utf-8", "replace")), ""
    except SchemaError as e:
        return None, f"manage.json: {e}"


MAX_SAMPLE = 50


def sample_items(files: Dict[str, bytes], layout: Optional[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Starter content the assistant wrote into manage.json ("items" inside a section), checked against that section's fields.
    Entries that do not fit the fields are skipped, so one bad row never blocks the rest."""
    body = files.get("manage.json")
    if body is None or not layout:
        return {}
    try:
        raw = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {}
    raw_sections = raw.get("sections") if isinstance(raw, dict) else raw
    out: Dict[str, List[Dict[str, Any]]] = {}
    for rs in raw_sections if isinstance(raw_sections, list) else []:
        if not isinstance(rs, dict) or not isinstance(rs.get("items"), list):
            continue
        name = str(rs.get("name") or re.sub(r"[^a-z0-9]+", "-", str(rs.get("label") or "").lower()).strip("-"))[:30]
        section = next((s for s in layout["sections"] if s["name"] == name), None)
        if not section:
            continue
        for item in rs["items"][:MAX_SAMPLE]:
            if not isinstance(item, dict):
                continue
            try:
                out.setdefault(name, []).append(check_values(section, item))
            except SchemaError:
                continue
    return out


def section_for(schema: Optional[Dict[str, Any]], name: str) -> Dict[str, Any]:
    for s in (schema or {}).get("sections", []):
        if s["name"] == name:
            return s
    return legacy_section(name) if not schema else {**legacy_section(name), "label": name.title()}


def _num(v: Any, label: str, places: Optional[int]) -> Optional[float]:
    if v in ("", None):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise SchemaError(f"{label} must be a number.")
    if n != n or abs(n) > 1_000_000_000_000:
        raise SchemaError(f"{label} is out of range.")
    return round(n, places) if places is not None else (int(n) if n == int(n) else n)


def check_values(section: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """The values an owner typed, checked and cleaned against the section's fields. Unknown keys are dropped."""
    out: Dict[str, Any] = {}
    for f in section["fields"]:
        key, label, t, v = f["key"], f["label"], f["type"], data.get(f["key"])
        if t == "yesno":
            out[key] = bool(v) if key in data else bool(f.get("default", False))
            continue
        if t == "number":
            v = _num(v, label, None)
        elif t == "money":
            v = _num(v, label, 2)
            if v is not None and v < 0:
                raise SchemaError(f"{label} cannot be negative.")
        elif t in ("text", "choice", "date", "link", "image", "longtext"):
            v = str(v or "").strip() if t == "longtext" else " ".join(str(v or "").split())
            v = v[:2000 if t == "longtext" else 300 if t in ("link", "image") else 200]
            if v and t == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise SchemaError(f"{label} must be a date like 2026-12-31.")
            if v and t == "choice" and v not in f["options"]:
                raise SchemaError(f"{label} must be one of: {', '.join(f['options'])}.")
            if v and t == "link" and not v.startswith("https://"):
                raise SchemaError(f"{label} must start with https://.")
            if v and t == "image" and not re.match(r"^(https://|/api/catalog/)", v):
                raise SchemaError(f"{label} must be an uploaded picture or an https:// address.")
        if f.get("required") and v in (None, ""):
            raise SchemaError(f"{label} is required.")
        out[key] = v
    return out


def legacy_columns(section: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """The five fixed database columns older code reads: filled from fields of the same name, title from the first text field."""
    first = next((values.get(f["key"]) for f in section["fields"] if f["type"] in ("text", "longtext") and values.get(f["key"])), "")
    price = values.get("price")
    return {"title": str(values.get("title") or first or "")[:120], "description": str(values.get("description") or "")[:2000],
            "price": price if isinstance(price, (int, float)) else None, "image": str(values.get("image") or ""),
            "in_stock": 0 if values.get("in_stock") is False else 1}


def summary(section: Dict[str, Any], values: Dict[str, Any]) -> Tuple[str, str]:
    """(heading, one line of the other values) for a list row."""
    fields = section["fields"]
    head_f = next((f for f in fields if f["type"] in ("text", "longtext") and values.get(f["key"])), None)
    head = str(values.get(head_f["key"])) if head_f else "(empty)"
    bits = []
    for f in fields:
        v = values.get(f["key"])
        if f is head_f or f["type"] in ("longtext", "image") or v in (None, ""):
            continue
        bits.append(f"{f['label']}: " + ("yes" if v is True else "no" if v is False else str(v)))
    return head, " · ".join(bits[:4])
