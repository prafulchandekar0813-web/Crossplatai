"""Read a public web page as text. Refuses anything that is not on the public internet (SSRF protection):
every hop of every redirect is resolved and must point at a globally routable address."""
import asyncio
import ipaddress
import re
import socket
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from llm_agent.tools.rag_search import _HTMLText

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
PAGE_CHARS = 12000
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
                                                 headers={"User-Agent": "AgentWorkbench/1.0 (+page reader)"})
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
    def _to_text(r: httpx.Response) -> str:
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "application/pdf" or r.final_url.lower().split("?")[0].endswith(".pdf"):  # type: ignore[attr-defined]
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(r.content))
            return "\n\n".join((p.extract_text() or "") for p in reader.pages[:60]).strip()
        if ctype in ("text/html", "application/xhtml+xml", ""):
            parser = _HTMLText()
            parser.feed(r.content.decode(r.encoding or "utf-8", errors="replace"))
            return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(parser.parts))).strip()
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/rss+xml", "application/atom+xml"):
            return r.content.decode(r.encoding or "utf-8", errors="replace").strip()
        raise FetchError(f"Cannot read content of type '{ctype}'.")

    async def read(self, url: str, offset: int = 0) -> Dict[str, Any]:
        url = (url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url if url and "://" not in url else url
        try:
            m = _GITHUB_REPO.match(url)
            r = None
            if m and m.group(2).lower() not in ("issues", "pulls"):
                for name in ("README.md", "readme.md", "README.rst", "README"):  # a repository's front page is its README
                    cand = await self._get(f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/{name}")
                    if cand.status_code == 200:
                        r = cand
                        break
            if r is None:
                r = await self._get(url)
            if r.status_code >= 400:
                raise FetchError(f"The site answered HTTP {r.status_code}.")
            text = await asyncio.to_thread(self._to_text, r)
        except FetchError as e:
            return {"status": "error", "error": str(e)}
        except httpx.TimeoutException:
            return {"status": "error", "error": "The site took too long to respond."}
        except (httpx.HTTPError, ValueError, OSError) as e:
            return {"status": "error", "error": f"Could not fetch the page ({type(e).__name__})."}
        except Exception as e:  # unreadable PDF and the like
            return {"status": "error", "error": f"Could not read the page content: {e}"}
        if not text:
            return {"status": "error", "error": "The page has no readable text (it may need JavaScript)."}
        offset = max(0, int(offset or 0))
        chunk = text[offset:offset + PAGE_CHARS]
        nxt = offset + PAGE_CHARS if offset + PAGE_CHARS < len(text) else None
        out: Dict[str, Any] = {"status": "success", "url": r.final_url, "content": chunk,  # type: ignore[attr-defined]
                               "total_chars": len(text), "next_offset": nxt}
        if nxt is not None:
            out["note"] = f"Page continues; call read_url again with offset={nxt} for more."
        return out
