"""Turn a web page's HTML into clean, readable text (markdown-like), and pull data out of pages that build themselves with JavaScript.

Pure functions, standard library only. Used by `web_fetch`."""
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

DROP = {"script", "style", "noscript", "svg", "canvas", "iframe", "template", "head", "form", "button", "select", "option", "dialog"}
CHROME = {"nav", "footer", "aside", "header"}       # page furniture; kept only when the page has no <main> or <article>
BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "table", "blockquote", "pre", "figure", "dl"}
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "track"}


class _Reader(HTMLParser):
    def __init__(self, base: str = "", keep_chrome: bool = False):
        super().__init__(convert_charrefs=True)
        self.base, self.keep_chrome = base, keep_chrome
        self.out: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.title = ""
        self._skip: List[str] = []
        self._in_title = False
        self._pre = 0
        self._href = ""
        self._anchor: List[str] = []
        self._cell = False

    def _skipping(self) -> bool:
        return bool(self._skip)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if self._skip:
            if tag not in VOID:
                self._skip.append(tag)
            return
        hidden = "hidden" in a or a.get("aria-hidden") == "true" or "display:none" in (a.get("style") or "").replace(" ", "")
        if tag in DROP or hidden or (tag in CHROME and not self.keep_chrome):
            if tag not in VOID:
                self._skip.append(tag)
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag == "tr":
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | " if self._cell else "")
            self._cell = True
        elif tag == "pre":
            self._pre += 1
            self.out.append("\n\n```\n")
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt'].strip()[:120]}]")
        elif tag == "a" and a.get("href") and not a["href"].startswith(("#", "javascript:", "mailto:", "tel:")):
            self._href, self._anchor = urljoin(self.base, a["href"]), []
        elif tag in BLOCK:
            self.out.append("\n\n")
        if tag == "tr":
            self._cell = False

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._skip:
            if tag in self._skip:
                while self._skip and self._skip.pop() != tag:
                    pass
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n")
        elif tag == "pre" and self._pre:
            self._pre -= 1
            self.out.append("\n```\n\n")
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "a" and self._href:
            text = " ".join("".join(self._anchor).split())
            if text and len(text) < 160:
                self.links.append((text, self._href))
            self._href, self._anchor = "", []
        elif tag in BLOCK or tag == "tr":
            self.out.append("\n\n" if tag in BLOCK else "\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        if self._pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip() or (self.out and not self.out[-1].endswith(("\n", " "))):
            self.out.append(text)
            if self._href:
                self._anchor.append(text)


def _tidy(parts: List[str]) -> str:
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract(html: str, base_url: str = "") -> Dict[str, Any]:
    """{title, text, links:[(text,url)], structured:str}. Falls back to the page furniture when the body text is tiny."""
    reader = _Reader(base_url)
    reader.feed(html)
    text = _tidy(reader.out)
    if len(text) < 60:  # many sites keep everything inside header/nav/aside; try again with those included
        loose = _Reader(base_url, keep_chrome=True)
        loose.feed(html)
        more = _tidy(loose.out)
        if len(more) > len(text):
            text, reader = more, loose
    seen, links = set(), []
    for label, url in reader.links:
        if url.startswith("http") and url not in seen:
            seen.add(url)
            links.append((label, url))
    return {"title": " ".join(reader.title.split())[:200], "text": text, "links": links, "structured": structured(html)}


def _meta(html: str) -> List[str]:
    out = []
    for key in ("og:title", "og:description", "description", "twitter:description"):
        m = re.search(r"""<meta[^>]+(?:property|name)=["']%s["'][^>]*content=["']([^"']+)["']""" % re.escape(key), html, re.I)
        if m:
            out.append(f"{key}: {m.group(1).strip()}")
    return out


def _flatten(node: Any, depth: int = 0) -> List[str]:
    """Readable strings out of JSON: long text values, with their key."""
    found: List[str] = []
    if depth > 8:
        return found
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and len(v) > 40 and not v.startswith(("http", "data:")) and k not in ("@context", "@id", "id", "url", "image", "src"):
                found.append(f"{k}: {v}")
            else:
                found += _flatten(v, depth + 1)
    elif isinstance(node, list):
        for v in node[:60]:
            found += _flatten(v, depth + 1)
    return found


def structured(html: str) -> str:
    """Data a JavaScript page embeds for itself: JSON-LD, Next.js/Nuxt state and meta tags. Often has the real content."""
    chunks: List[str] = _meta(html)
    pats = [r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
            r"""<script[^>]+id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""",
            r"""<script[^>]+id=["']__NUXT_DATA__["'][^>]*>(.*?)</script>""",
            r"""<script[^>]+type=["']application/json["'][^>]*>(.*?)</script>"""]
    for pat in pats:
        for raw in re.findall(pat, html, re.S | re.I)[:6]:
            try:
                chunks += _flatten(json.loads(raw))[:40]
            except ValueError:
                continue
    text = "\n".join(dict.fromkeys(chunks))
    return text[:6000]
