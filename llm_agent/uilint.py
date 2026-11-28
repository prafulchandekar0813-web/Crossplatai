"""Is the app's screen worth looking at? Plain checks over the project's HTML and CSS that catch the "bare minimum" look.

Pure functions over {path: text}. Each finding is one sentence the assistant can act on, and none of them is a hard error:
a good design can break any single rule, but a screen that breaks most of them is the plain page users are tired of."""
import re
from typing import Dict, List, Optional, Sequence

STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.I | re.S)
PAGE_EXT = (".html", ".htm", ".jinja", ".j2")
NAV_BOTTOM = re.compile(r"(position\s*:\s*(fixed|sticky)[^}]*bottom\s*:\s*0|bottom\s*:\s*0[^}]*position\s*:\s*(fixed|sticky)|bottom-nav|tabbar|tab-bar|bottom-bar)", re.I | re.S)
NAV_SIDE = re.compile(r"(sidebar|side-nav|sidenav|nav-rail|navrail|navigation-view|side-menu)", re.I)
WIDE_QUERY = re.compile(r"@media[^{]*min-width\s*:\s*(\d{3,4})px", re.I)


def _parts(files: Dict[str, str]):
    pages = {p: t for p, t in files.items() if p.lower().endswith(PAGE_EXT) and "<html" in t.lower()}
    css = "\n".join([t for p, t in files.items() if p.lower().endswith(".css")] +
                    [m for t in pages.values() for m in STYLE_BLOCK.findall(t)])
    return pages, css


def lint(files: Dict[str, str], platforms: Optional[Sequence[str]] = None) -> List[str]:
    pages, css = _parts(files)
    if not pages and not css.strip():
        return []                                           # no web screens (API only, or a native shell): nothing to judge
    html = "\n".join(pages.values()) + "\n" + "\n".join(t for p, t in files.items() if p.lower().endswith((".js", ".mjs")))
    out: List[str] = []
    rules = len([ln for ln in css.splitlines() if "{" in ln or ln.strip().endswith(";")])
    if rules < 60:
        out.append(f"The styling is very small ({rules} CSS lines), which gives the plain default look. Build a real design: colour variables, a type scale, cards, "
                   "spacing, a header, buttons with states. Use the design brief and the references you studied.")
    if not re.search(r":root\s*\{[^}]*--[a-z]", css, re.I):
        out.append("No colour or spacing variables on :root (for example --accent, --bg, --text, --radius). Define a small palette so the whole app stays consistent.")
    if "font-family" not in css.lower():
        out.append("No font is chosen, so the browser default is used. Set a font stack and a size scale (a headline, a title, body, small text).")
    if "@media" not in css.lower():
        out.append("No @media rules: the layout cannot adapt between a phone and a wide window. Add a narrow and a wide layout.")
    if "prefers-color-scheme" not in css.lower() and "dark" not in css.lower():
        out.append("There is no dark theme. Most phones use one at night; add prefers-color-scheme: dark colours.")
    if not re.search(r"<svg|<img|\.svg|\.png|\.jpe?g|\.webp|material-symbols|material-icons|fa-|bi-|lucide|feather", html, re.I):
        out.append("There are no icons or pictures. Add icons to the navigation and actions (inline SVG works everywhere) and a hero or illustration where it fits.")
    multi = len(pages) > 1 or len(re.findall(r"data-(page|view|tab)|class=\"[^\"]*(page|view|tab)\b", html, re.I)) > 1
    if multi and not re.search(r"<nav\b|role=\"(navigation|tablist)\"", html, re.I):
        out.append("The app has several screens but no navigation element. Add a <nav> (tabs on a phone, a side list on a wide window) so every screen is one tap away.")
    if not re.search(r"empty|no (items|results|data)|nothing (here|yet)|loading|skeleton|spinner", html, re.I):
        out.append("No empty state or loading state. Show a friendly message and a spinner or skeleton while data loads and when a list is empty.")
    if not re.search(r"hover|:focus|:active", css, re.I):
        out.append("No hover, focus or pressed styles. Buttons and cards should react when touched or pointed at.")
    wanted = {p.lower() for p in platforms or []}
    if "android" in wanted:
        if not NAV_BOTTOM.search(css + html):
            out.append("For Android: use a bottom navigation bar with 3 to 5 tabs, reachable by thumb, plus one main action button. Keep a top app bar for the title.")
        if not re.search(r"(min-height|height)\s*:\s*(4[4-9]|[5-9]\d)px|min-height\s*:\s*[3-9](\.\d+)?rem", css, re.I):
            out.append("For Android: make touch targets at least 48px tall (buttons, list rows, tabs).")
    if "windows" in wanted:
        wide = [int(m) for m in WIDE_QUERY.findall(css)]
        if not any(w >= 800 for w in wide):
            out.append("For Windows: add a wide layout (@media (min-width: 900px) or more) that uses the space: a side navigation, content in columns, a list with the detail beside it.")
        if not NAV_SIDE.search(css + html):
            out.append("For Windows: use a side navigation (rail or sidebar) on wide windows instead of the phone's bottom tabs, and show hover states and keyboard focus.")
    if {"android", "windows"} <= wanted and not (NAV_BOTTOM.search(css + html) and (NAV_SIDE.search(css + html) and wide_ok(css))):
        out.append("This app is for both Android and Windows: one page, two layouts. Bottom tabs on narrow screens and a side rail from about 900px wide, using the same data.")
    return out


def wide_ok(css: str) -> bool:
    return any(int(w) >= 800 for w in WIDE_QUERY.findall(css))
