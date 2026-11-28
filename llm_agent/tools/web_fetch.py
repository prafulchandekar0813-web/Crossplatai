"""Read a public web page as text. Refuses anything that is not on the public internet (SSRF protection):
every hop of every redirect is resolved and must point at a globally routable address."""
import asyncio
import ipaddress
import re
import socket
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from llm_agent.tools import html_text

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
PAGE_CHARS = 12000
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 AgentWorkbench/1.0"
RETRY_STATUS = {429, 502, 503, 504}
BLOCKED_STATUS = {401, 403, 404, 410, 429, 451, 502, 503, 504}   # worth looking for an archived copy
_GITHUB_BLOB = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/blob/(.+)$", re.I)
_GITHUB_REPO = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)

Resolver = Callable[[str, int], Awaitable[List[str]]]


async def _resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


class FetchError(Exception):
    pass


class WebFetch:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, resolver: Resolver = _resolve):
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), follow_redirects=False,
                                                 headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
        self._resolve = resolver

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError("Only http(s) web addresses can be read.")
        if parts.username or parts.password:
            raise FetchError("Addresses with a username or password are not allowed.")
        try:
            addrs = await self._resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        except OSError:
            raise FetchError(f"Could not find the host '{parts.hostname}'.")
        if not addrs or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs):
            raise FetchError("That address is not on the public internet, so it cannot be read.")

    async def _get(self, url: str) -> httpx.Response:
        """GET with manual, re-validated redirects and a hard size cap. Returns a response with .content loaded."""
        for _ in range(MAX_REDIRECTS + 1):
            await self._check(url)
            async with self._http.stream("GET", url, headers={"Accept": "text/html,text/plain,application/json,application/pdf;q=0.8,*/*;q=0.1"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                r.truncated_download = len(body) > MAX_BYTES  # type: ignore[attr-defined]
                r._content = bytes(body[:MAX_BYTES])
                r.final_url = url  # type: ignore[attr-defined]
                return r
        raise FetchError("Too many redirects.")

    @staticmethod
    def _decode(r: httpx.Response) -> str:
        enc = r.charset_encoding
        if not enc:  # no charset in the header: look for <meta charset=...> in the first bytes
            m = re.search(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", r.content[:3000], re.I)
            enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
        try:
            return r.content.decode(enc, errors="replace")
        except LookupError:
            return r.content.decode("utf-8", errors="replace")

    def _to_text(self, r: httpx.Response, want_links: bool = False) -> Dict[str, Any]:
        """{text, title, links}. Raises FetchError for types that are not text."""
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "application/pdf" or r.final_url.lower().split("?")[0].endswith(".pdf"):  # type: ignore[attr-defined]
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(r.content))
            return {"text": "\n\n".join((p.extract_text() or "") for p in reader.pages[:60]).strip(), "title": "", "links": []}
        body = self._decode(r)
        if ctype in ("text/html", "application/xhtml+xml", "") or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            page = html_text.extract(body, r.final_url)  # type: ignore[attr-defined]
            text = page["text"]
            if len(text) < 400 and page["structured"]:  # a page that builds itself with JavaScript: use the data it ships with
                text = (text + "\n\n" if text else "") + "[data embedded in the page]\n" + page["structured"]
            return {"text": text, "title": page["title"], "links": page["links"][:40] if (want_links or len(text) < 400) else []}
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/rss+xml", "application/atom+xml", "application/javascript"):
            return {"text": body.strip(), "title": "", "links": []}
        raise FetchError(f"Cannot read content of type '{ctype}'.")

    async def _archived(self, url: str) -> Optional[httpx.Response]:
        """The Internet Archive's copy of a page the site itself refuses to serve."""
        try:
            meta = await self._get("https://archive.org/wayback/available?url=" + url)
            snap = (meta.json().get("archived_snapshots") or {}).get("closest") or {}
            if snap.get("available") and snap.get("url"):
                got = await self._get(snap["url"].replace("http://", "https://", 1))
                if got.status_code < 400:
                    got.archived_at = snap.get("timestamp", "")  # type: ignore[attr-defined]
                    return got
        except (FetchError, httpx.HTTPError, ValueError):
            pass
        return None

    async def _fetch(self, url: str) -> httpx.Response:
        """One request; a busy site (429/503) gets a single polite retry."""
        r = await self._get(url)
        if r.status_code in RETRY_STATUS:
            try:
                wait = min(5.0, float(r.headers.get("retry-after", "2")))
            except ValueError:
                wait = 2.0
            await asyncio.sleep(wait)
            r = await self._get(url)
        return r

    async def read(self, url: str, offset: int = 0, links: bool = False) -> Dict[str, Any]:
        url = (url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url if url and "://" not in url else url
        note = ""
        try:
            m = _GITHUB_REPO.match(url)
            b = _GITHUB_BLOB.match(url)
            r = None
            if b:  # a file on GitHub: read the raw file, not the page around it
                url = f"https://raw.githubusercontent.com/{b.group(1)}/{b.group(2)}/{b.group(3)}"
            elif m and m.group(2).lower() not in ("issues", "pulls"):
                for name in ("README.md", "readme.md", "README.rst", "README"):  # a repository's front page is its README
                    cand = await self._get(f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/{name}")
                    if cand.status_code == 200:
                        r = cand
                        break
            if r is None:
                r = await self._fetch(url)
            page: Dict[str, Any] = {"text": ""}
            if r.status_code < 400:
                page = await asyncio.to_thread(self._to_text, r, links)
            if r.status_code in BLOCKED_STATUS or (r.status_code < 400 and not page["text"]):
                old = await self._archived(url)
                if old is not None:
                    got = await asyncio.to_thread(self._to_text, old, links)
                    if got["text"]:
                        r, page = old, got
                        note = f"The site would not serve this page, so this is the Internet Archive's copy from {old.archived_at[:8]} (it may be out of date)."  # type: ignore[attr-defined]
            if r.status_code >= 400 and not note:
                raise FetchError(f"The site answered HTTP {r.status_code}" + (" (it blocks automated readers)" if r.status_code in (401, 403, 429) else "") + ".")
        except FetchError as e:
            return {"status": "error", "error": str(e)}
        except httpx.TimeoutException:
            return {"status": "error", "error": "The site took too long to respond."}
        except (httpx.HTTPError, ValueError, OSError) as e:
            return {"status": "error", "error": f"Could not fetch the page ({type(e).__name__})."}
        except Exception as e:  # unreadable PDF and the like
            return {"status": "error", "error": f"Could not read the page content: {e}"}
        text = page["text"]
        if not text:
            return {"status": "error", "error": "The page has no readable text (it is built with JavaScript). Look for the site's API, a sitemap or RSS feed, or search for another source."}
        offset = max(0, int(offset or 0))
        chunk = text[offset:offset + PAGE_CHARS]
        nxt = offset + PAGE_CHARS if offset + PAGE_CHARS < len(text) else None
        out: Dict[str, Any] = {"status": "success", "url": r.final_url, "content": chunk,  # type: ignore[attr-defined]
                               "total_chars": len(text), "next_offset": nxt}
        if page.get("title"):
            out["title"] = page["title"]
        if page.get("links") and offset == 0:
            out["links"] = [{"text": t, "url": u} for t, u in page["links"][:30]]
        notes = [n for n in (note, f"Page continues; call read_url again with offset={nxt} for more." if nxt is not None else "") if n]
        if notes:
            out["note"] = " ".join(notes)
        return out
