"""The design format: a design is pages, a page is a list of blocks, and a block is plain data.

Users never write code or HTML. A design can only contain the blocks and settings defined here, every value is checked and
trimmed, and the viewer draws it with safe text and style calls. The same definitions are sent to the editor, so the palette
and the property forms are built from this one table and cannot drift from what the server accepts.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MAX_PAGES, MAX_BLOCKS, MAX_DEPTH = 20, 400, 4
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ID = re.compile(r"^[a-z0-9_-]{1,24}$")
ALIGN = ("left", "center", "right")
FONTS = ("system", "serif", "rounded", "mono")
RADII = ("square", "soft", "round")
UPLOADED = re.compile(r"^/api/designs/img/[0-9a-f]{16}\.webp$")     # a picture uploaded to the platform (see designs.py)
SLUG = re.compile(r"^[a-z0-9_-]{1,30}$")
# The icon names the editor offers; web_ui/js/design/icons.js draws each one (a test keeps the two lists equal).
ICONS = ("home", "search", "heart", "star", "cart", "bag", "user", "users", "phone", "mail", "pin", "clock", "calendar", "camera", "image",
         "gift", "tag", "truck", "shield", "check", "info", "bell", "settings", "menu", "plus", "arrow-right", "arrow-left", "share", "link",
         "globe", "coffee", "utensils", "music", "play", "book", "briefcase", "wrench", "leaf", "sun", "moon", "chat", "scissors", "bolt")


class DesignError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    kind: str                       # text | longtext | choice | int | color | bool | action | image | icon | slug
    label: str
    default: Any = ""
    options: Tuple[str, ...] = ()
    lo: int = 0
    hi: int = 0
    max_len: int = 0

    def spec(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"key": self.key, "kind": self.kind, "label": self.label, "default": self.default}
        if self.options:
            out["options"] = list(self.options)
        if self.kind == "int":
            out.update(min=self.lo, max=self.hi)
        if self.kind == "icon":
            out["options"] = list(ICONS)
        if self.max_len:
            out["max_length"] = self.max_len
        return out


@dataclass(frozen=True)
class Block:
    label: str
    container: bool = False
    fields: Tuple[Field, ...] = field(default_factory=tuple)


def _align(default="left"):
    return Field("align", "choice", "Align", default, ALIGN)


def _color(key="color", label="Colour"):
    return Field(key, "color", label, "")


BLOCKS: Dict[str, Block] = {
    "heading": Block("Heading", False, (Field("text", "text", "Text", "Your heading", max_len=200),
                                        Field("size", "choice", "Size", "m", ("s", "m", "l")), _align(), _color())),
    "text": Block("Text", False, (Field("text", "longtext", "Text", "Write something here.", max_len=2000),
                                  Field("size", "choice", "Size", "m", ("s", "m", "l")), _align(), _color())),
    "button": Block("Button", False, (Field("label", "text", "Label", "Tap me", max_len=40),
                                      Field("action", "action", "When tapped", {"type": "none"}),
                                      Field("style", "choice", "Style", "solid", ("solid", "outline")),
                                      Field("icon", "icon", "Icon (optional)", ""),
                                      Field("full", "bool", "Full width", False), _align("center"))),
    "image": Block("Image", False, (Field("src", "image", "Picture", "", max_len=300),
                                    Field("alt", "text", "Description", "", max_len=100),
                                    Field("height", "int", "Height", 160, lo=40, hi=600),
                                    Field("fit", "choice", "Fit", "cover", ("cover", "contain")),
                                    Field("radius", "int", "Rounded corners", 12, lo=0, hi=48))),
    "shape": Block("Shape", False, (Field("shape", "choice", "Shape", "rect", ("rect", "circle", "line")),
                                    _color("color", "Colour"), Field("height", "int", "Height", 80, lo=2, hi=400),
                                    Field("width", "int", "Width (%)", 100, lo=10, hi=100), _align("center"))),
    "group": Block("Row / box", True, (Field("direction", "choice", "Direction", "column", ("column", "row")),
                                       Field("gap", "int", "Space between", 12, lo=0, hi=48),
                                       Field("pad", "int", "Padding", 12, lo=0, hi=48),
                                       _color("bg", "Background"), Field("radius", "int", "Rounded corners", 12, lo=0, hi=48),
                                       Field("align", "choice", "Line up", "stretch", ("start", "center", "end", "stretch")),
                                       Field("card", "bool", "Card look (surface colour, border)", False),
                                       Field("sizing", "choice", "Items in a row", "equal", ("equal", "natural")),
                                       Field("wrap", "bool", "Drop to the next line when there is no room", True),
                                       Field("justify", "choice", "Spread (rows)", "start", ("start", "center", "end", "between")))),
    "spacer": Block("Space", False, (Field("height", "int", "Height", 24, lo=4, hi=200),)),
    "divider": Block("Divider", False, (_color("color", "Colour"),)),
    "icon": Block("Icon", False, (Field("icon", "icon", "Icon", "star"), Field("size", "int", "Size", 36, lo=16, hi=96), _color(), _align("center"))),
    "badge": Block("Badge", False, (Field("text", "text", "Text", "New", max_len=30), _color("bg", "Colour"), _align("left"))),
    "banner": Block("Banner", False, (Field("text", "text", "Message", "Free delivery this week", max_len=200), _color("bg", "Background"),
                                      _color("color", "Text colour"), _align("center"))),
    "quote": Block("Quote", False, (Field("text", "longtext", "Quote", "A short word from a happy customer.", max_len=300),
                                    Field("author", "text", "Who said it", "Customer name", max_len=60), _align("center"), _color())),
    "faq": Block("Questions and answers", False, (Field("items", "longtext", "One per line: question :: answer", "How do I order? :: Tap an item, then Order.\nWhere are you? :: Come and visit us!", max_len=3000),)),
    "table": Block("Table (hours, prices)", False, (Field("rows", "longtext", "One per line: label :: value", "Monday to Friday :: 9:00 to 18:00\nSaturday :: 10:00 to 14:00", max_len=1500),)),
    "list": Block("List", False, (Field("lines", "longtext", "One per line", "First point\nSecond point\nThird point", max_len=1500),
                                  Field("icon", "icon", "Icon", "check"), _color())),
    "stat": Block("Number", False, (Field("value", "text", "Number", "250+", max_len=20), Field("label", "text", "Label", "Happy customers", max_len=40), _align("center"), _color())),
    "rating": Block("Rating", False, (Field("value", "int", "Stars (0 to 5)", 5, lo=0, hi=5), Field("label", "text", "Text", "Loved by our customers", max_len=60), _align("center"))),
    "progress": Block("Progress bar", False, (Field("value", "int", "Percent", 75, lo=0, hi=100), Field("label", "text", "Label", "", max_len=60), _color())),
    "form": Block("Form (messages, bookings)", False, (
        Field("title", "text", "Title", "Contact us", max_len=60),
        Field("fields", "longtext", "Questions, one per line (add * if required)", "Your name*\nPhone*\nMessage", max_len=500),
        Field("big_last", "bool", "Make the last one a big text box", True),
        Field("button", "text", "Button", "Send", max_len=30),
        Field("collection", "slug", "Where it arrives in your inbox", "messages", max_len=30),
        Field("success", "text", "Thank-you message", "Thank you! We will get back to you.", max_len=120))),
    "items": Block("Items from Manage app", False, (
        Field("collection", "slug", "Section name in Manage app", "products", max_len=30),
        Field("layout", "choice", "Layout", "grid", ("grid", "list")),
        Field("columns", "int", "Columns (grid)", 2, lo=1, hi=4),
        Field("show_image", "bool", "Show pictures", True), Field("show_details", "bool", "Show details", True),
        Field("limit", "int", "Show at most", 12, lo=1, hi=50),
        Field("detail", "bool", "Tapping an item opens its details", True),
        Field("order", "bool", "Let customers order or ask about an item", False),
        Field("order_label", "text", "Order button", "Order this", max_len=30),
        Field("empty", "text", "When there is nothing yet", "Nothing here yet.", max_len=80))),
}

def _col(*kids: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "group", "pad": 0, "children": list(kids)}


def _row(*kids: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    return {"type": "group", "direction": "row", "pad": 0, "children": list(kids), **kw}


# Ready-made arrangements the editor offers next to the blocks. Dropping one adds these blocks (the editor gives them new ids).
LAYOUTS: Tuple[Dict[str, Any], ...] = (
    {"name": "two_columns", "label": "2 columns", "block": _row(_col({"type": "text", "text": "Left column"}), _col({"type": "text", "text": "Right column"}))},
    {"name": "three_columns", "label": "3 columns", "block": _row(*[_col({"type": "text", "text": f"Column {n}"}) for n in (1, 2, 3)])},
    {"name": "icon_text", "label": "Icon beside text", "block": _row({"type": "icon", "icon": "star", "size": 28}, {"type": "text", "text": "A short line next to the icon"},
                                                                    sizing="natural", wrap=False, align="center", gap=10)},
    {"name": "image_text", "label": "Picture beside text", "block": _row({"type": "image", "height": 140}, _col({"type": "heading", "text": "Title", "size": "s"}, {"type": "text", "text": "A few words about it."}), align="center")},
    {"name": "gallery", "label": "3 pictures in a line", "block": _row(*[{"type": "image", "height": 110} for _ in range(3)], gap=8, wrap=False)},
    {"name": "numbers", "label": "3 numbers in a line", "block": _row({"type": "stat", "value": "10+", "label": "Years"}, {"type": "stat", "value": "500", "label": "Customers"},
                                                                       {"type": "stat", "value": "4.9", "label": "Rating"}, wrap=False)},
    {"name": "buttons", "label": "2 buttons in a line", "block": _row({"type": "button", "label": "Call us", "icon": "phone"}, {"type": "button", "label": "Message", "icon": "chat", "style": "outline"},
                                                                       sizing="natural", justify="center")},
)

THEME: Tuple[Field, ...] = (
    Field("accent", "color", "Main colour", "#2f5bea"), Field("bg", "color", "Background", "#ffffff"),
    Field("text", "color", "Text colour", "#14213d"), Field("surface", "color", "Cards and panels", "#f1f4fa"),
    Field("font", "choice", "Font", "system", FONTS), Field("radius", "choice", "Corners", "soft", RADII))

# Ready-made colour themes the editor shows as swatches. Applying one just copies these values into the design's theme.
PRESETS: Tuple[Dict[str, Any], ...] = (
    {"name": "Ocean", "theme": {"accent": "#2f5bea", "bg": "#ffffff", "text": "#14213d", "surface": "#eef2ff"}},
    {"name": "Sunset", "theme": {"accent": "#e8590c", "bg": "#fff8f1", "text": "#3b1d0e", "surface": "#ffe8d6"}},
    {"name": "Forest", "theme": {"accent": "#2b8a3e", "bg": "#f6fbf6", "text": "#12301a", "surface": "#e3f3e6"}},
    {"name": "Berry", "theme": {"accent": "#c2255c", "bg": "#fff7fa", "text": "#3a1122", "surface": "#fde5ee"}},
    {"name": "Violet", "theme": {"accent": "#7048e8", "bg": "#faf8ff", "text": "#231640", "surface": "#ece6fd"}},
    {"name": "Teal", "theme": {"accent": "#0c8599", "bg": "#f4fbfc", "text": "#0f2f36", "surface": "#dff3f6"}},
    {"name": "Sand", "theme": {"accent": "#a0723a", "bg": "#fbf7f0", "text": "#2f2415", "surface": "#f1e7d6"}},
    {"name": "Midnight", "theme": {"accent": "#74c0fc", "bg": "#10151f", "text": "#e8edf5", "surface": "#1b2333"}},
    {"name": "Charcoal", "theme": {"accent": "#f59f00", "bg": "#1c1c1e", "text": "#f2f2f2", "surface": "#2a2a2d"}},
    {"name": "Mono", "theme": {"accent": "#212529", "bg": "#ffffff", "text": "#212529", "surface": "#f1f3f5"}},
)


def spec() -> Dict[str, Any]:
    """What the editor needs: every block with its fields, and the theme fields."""
    return {"blocks": {k: {"label": b.label, "container": b.container, "fields": [f.spec() for f in b.fields]} for k, b in BLOCKS.items()},
            "theme": [f.spec() for f in THEME], "presets": list(PRESETS), "layouts": list(LAYOUTS), "icons": list(ICONS), "limits": {"pages": MAX_PAGES, "blocks": MAX_BLOCKS, "depth": MAX_DEPTH}}


def _value(f: Field, v: Any) -> Any:
    """One value, checked against its field. A wrong value becomes the default instead of failing the whole design."""
    if f.kind in ("text", "longtext"):
        s = v if isinstance(v, str) else ("" if v is None else str(v))
        s = s.replace("\r", "")
        return (s if f.kind == "longtext" else " ".join(s.split()))[:f.max_len or 200]
    if f.kind == "choice":
        return v if v in f.options else f.default
    if f.kind == "int":
        try:
            return max(f.lo, min(f.hi, int(v)))
        except (TypeError, ValueError):
            return f.default
    if f.kind == "color":
        return v if isinstance(v, str) and HEX.match(v) else ""
    if f.kind == "icon":
        return v if v in ICONS else f.default
    if f.kind == "slug":
        s = str(v or "").strip().lower()
        return s if SLUG.match(s) else f.default
    if f.kind == "bool":
        return v is True
    if f.kind == "image":
        s = str(v or "").strip()[:f.max_len]
        if s == "@logo" or UPLOADED.match(s):
            return s
        return s if (s.startswith("https://") and " " not in s and "\"" not in s and "<" not in s) else ""
    return f.default  # "action" is handled with the pages it can point to


def _action(v: Any, pages: set) -> Dict[str, str]:
    if not isinstance(v, dict):
        return {"type": "none"}
    if v.get("type") == "page" and v.get("target") in pages:
        return {"type": "page", "target": v["target"]}
    url = str(v.get("url") or "").strip()[:300]
    if v.get("type") == "link" and url.startswith(("https://", "tel:", "mailto:")) and not re.search(r"[\s\"<>]", url):
        return {"type": "link", "url": url}
    return {"type": "none"}


def _block(raw: Any, pages: set, seen: set, depth: int, count: List[int]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("type") not in BLOCKS:
        return None
    count[0] += 1
    if count[0] > MAX_BLOCKS:
        raise DesignError(f"A design can hold up to {MAX_BLOCKS} blocks.")
    bid = raw.get("id")
    if not isinstance(bid, str) or not ID.match(bid) or bid in seen:
        raise DesignError("Every block needs its own short id (letters, digits, - and _).")
    seen.add(bid)
    spec_ = BLOCKS[raw["type"]]
    out: Dict[str, Any] = {"id": bid, "type": raw["type"]}
    for f in spec_.fields:
        out[f.key] = _action(raw.get(f.key), pages) if f.kind == "action" else _value(f, raw.get(f.key, f.default))
    if spec_.container:
        if depth >= MAX_DEPTH:
            raise DesignError(f"Boxes can be nested up to {MAX_DEPTH} deep.")
        kids = raw.get("children") if isinstance(raw.get("children"), list) else []
        out["children"] = [b for b in (_block(k, pages, seen, depth + 1, count) for k in kids) if b]
    return out


def clean(design: Any) -> Dict[str, Any]:
    """Check and normalise a whole design. Raises DesignError with a message the owner can act on."""
    if not isinstance(design, dict):
        raise DesignError("The design is not valid.")
    raw_pages = design.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise DesignError("A design needs at least one page.")
    if len(raw_pages) > MAX_PAGES:
        raise DesignError(f"A design can have up to {MAX_PAGES} pages.")
    ids = []
    for p in raw_pages:
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str) or not ID.match(pid) or pid in ids:
            raise DesignError("Every page needs its own short id.")
        ids.append(pid)
    page_ids, seen, count = set(ids), set(), [0]
    pages = []
    for p in raw_pages:
        title = " ".join(str(p.get("title") or "").split())[:20] or "Page"
        blocks = [b for b in (_block(x, page_ids, seen, 1, count) for x in (p.get("blocks") if isinstance(p.get("blocks"), list) else [])) if b]
        pages.append({"id": p["id"], "title": title, "blocks": blocks})
    theme_in = design.get("theme") if isinstance(design.get("theme"), dict) else {}
    theme = {f.key: (_value(f, theme_in.get(f.key)) or f.default) for f in THEME}
    return {"theme": theme, "nav": "tabs" if design.get("nav") != "none" else "none", "pages": pages}


def retarget_items(design: Dict[str, Any], sections: List[str]) -> int:
    """Point "items" blocks at a section the app really has. A template says "products" or "menu"; an app may call its
    content something else, and a block showing a section that does not exist would stay empty. Returns how many changed."""
    if not sections:
        return 0
    changed = 0

    def walk(blocks: List[Dict[str, Any]]) -> None:
        nonlocal changed
        for b in blocks:
            if b.get("type") == "items" and b.get("collection") not in sections:
                b["collection"] = sections[0]
                changed += 1
            walk(b.get("children", []))
    for p in design.get("pages", []):
        walk(p.get("blocks", []))
    return changed
