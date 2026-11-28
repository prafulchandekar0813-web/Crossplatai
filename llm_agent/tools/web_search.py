"""Web search through a configured provider (SearXNG, Brave or the keyless DuckDuckGo page). With no provider the tool is simply not offered.
When the chosen provider fails or finds nothing, DuckDuckGo is tried once as a backup."""
import asyncio
import json
import html as htmllib
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit, urlunsplit

import httpx

from llm_agent.config import SearchConfig

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_URL = "https://html.duckduckgo.com/html/"
DDG_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_DDG_RESULT = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)', re.S)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>', re.S)


CACHE_SECONDS = 600
RETRY_STATUS = {429, 500, 502, 503, 504}


RECENCY = {"day": ("day", "pd", "d"), "week": ("week", "pw", "w"), "month": ("month", "pm", "m"), "year": ("year", "py", "y")}   # SearXNG, Brave, DuckDuckGo
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|igshid|yclid)", re.I)
PER_DOMAIN = 2


def tidy(rows: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    """Clean a result list: no tracking parameters, no duplicates, at most two results per website, and the site named."""
    seen, per_site, out = set(), {}, []
    for r in rows:
        parts = urlsplit(r.get("url", ""))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        query = urlencode([(k, v) for k, v in parse_qs(parts.query, keep_blank_values=True).items() for v in v if not TRACKING.match(k)])
        url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))
        site = parts.hostname.removeprefix("www.")
        same = (site, parts.path.rstrip("/").lower(), query)
        if same in seen or per_site.get(site, 0) >= PER_DOMAIN:
            continue
        seen.add(same)
        per_site[site] = per_site.get(site, 0) + 1
        out.append({**r, "url": url, "site": site})
    return out[:limit]


class WebSearch:
    """One search at a time, spaced out, retried when the provider is busy, and tried on every configured SearXNG address."""

    def __init__(self, get_cfg: Callable[[], SearchConfig], client: Optional[httpx.AsyncClient] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic):
        self._get_cfg = get_cfg
        self._http = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._sleep, self._clock = sleep, clock
        self._lock = asyncio.Lock()
        self._last = -1e9
        self._good = 0                                   # index of the SearXNG address that worked last
        self._cache: Dict[str, Any] = {}
        self._recency = ""

    @property
    def enabled(self) -> bool:
        return bool(self._get_cfg().provider)

    @property
    def provider(self) -> str:
        return self._get_cfg().provider

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _addresses(cfg: SearchConfig) -> List[str]:
        return [u.rstrip("/") for u in re.split(r"[\s,]+", cfg.base_url or "") if u.startswith(("http://", "https://"))]

    async def search(self, query: str, recency: str = "") -> Dict[str, Any]:
        """recency: "day", "week", "month" or "year" limits results to that period (for news and anything that changes)."""
        cfg = self._get_cfg()
        recency = recency if recency in RECENCY else ""
        query = " ".join((query or "").split())[:300]
        if not query:
            return {"status": "error", "error": "query is empty"}
        if not cfg.provider:
            return {"status": "error", "error": "Web search is not configured on this server."}
        key = f"{cfg.provider}|{recency}|{query.lower()}"
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_SECONDS:
            return dict(hit[1], cached=True)
        async with self._lock:                           # parallel searches are what gets a provider to answer 429
            wait = cfg.min_interval_seconds - (self._clock() - self._last)
            if wait > 0:
                await self._sleep(wait)
            self._recency = recency                      # read by the provider calls below; safe because searches run one at a time
            try:
                out = await self._run(cfg, query)
            finally:
                self._last = self._clock()
        if out["status"] == "success":
            out["results"] = tidy(out["results"], cfg.max_results)
        if out["status"] == "success" and out["results"]:
            self._cache[key] = (self._clock(), out)
        return out

    async def _run(self, cfg: SearchConfig, query: str) -> Dict[str, Any]:
        if cfg.provider not in ("searxng", "brave", "duckduckgo"):
            return {"status": "error", "error": f"Unknown search provider '{cfg.provider}'"}
        out = await self._primary(cfg, query)
        if cfg.provider != "duckduckgo" and not (out["status"] == "success" and out["results"]):
            try:                                          # backup: the chosen provider is down, busy or empty
                rows = await self._duckduckgo(cfg, query)
            except (httpx.HTTPError, ValueError):
                rows = []
            if rows:
                return {"status": "success", "query": query, "provider": "duckduckgo", "results": rows[:cfg.max_results],
                        "note": f"{cfg.provider} gave no results, so DuckDuckGo was used."}
        return out

    async def _primary(self, cfg: SearchConfig, query: str) -> Dict[str, Any]:
        addresses = self._addresses(cfg) if cfg.provider == "searxng" else [""]
        if not addresses and cfg.provider == "searxng":
            return {"status": "error", "error": "No valid SearXNG address is set in the admin settings (it must start with https://)."}
        last_error, results, notes = "", [], []
        order = [addresses[(self._good + i) % len(addresses)] for i in range(len(addresses))]
        for address in order:
            try:
                results, notes = await self._once(cfg, address, query)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                hint = " (SearXNG needs the 'json' format enabled in its settings)" if cfg.provider == "searxng" and code == 403 else ""
                last_error = f"Search provider returned HTTP {code}{hint}"
                if code == 429:
                    last_error += ". It is rate limiting this server"
                continue
            except json.JSONDecodeError:
                last_error = ("the SearXNG address is probably wrong, or its 'json' output format is disabled (add json to search.formats in its settings.yml)"
                              if cfg.provider == "searxng" else "the provider sent an unexpected reply")
                last_error = f"The search server did not return search results: {last_error}. web_search only takes search words; to read a web address use read_url."
                continue
            except (httpx.HTTPError, ValueError) as e:
                last_error = f"Search request failed ({type(e).__name__}). Check the search server address in the admin settings."
                continue
            if results:
                self._good = addresses.index(address)
                return {"status": "success", "query": query, "provider": cfg.provider, "results": results[:cfg.max_results]}
        if results == [] and not last_error:
            return {"status": "success", "query": query, "provider": cfg.provider, "results": [],
                    "note": "The search returned nothing" + (f" ({'; '.join(notes)[:200]})" if notes else "") + ". Try 3 to 6 plain words, "
                            "no quotes, or read_url a page you already know (the official docs, a GitHub README, raw.githubusercontent.com)."}
        return {"status": "error", "error": last_error + ". Do not keep searching now: use read_url on pages you know (official documentation, GitHub README files) and say what you could not look up."}

    async def _once(self, cfg: SearchConfig, address: str, query: str):
        """One provider call, retried a few times with growing pauses when the provider says it is busy."""
        for attempt in range(cfg.retries + 1):
            try:
                if cfg.provider == "searxng":
                    return await self._searxng(address, query)
                if cfg.provider == "duckduckgo":
                    return await self._duckduckgo(cfg, query), []
                return await self._brave(cfg, query), []
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in RETRY_STATUS or attempt == cfg.retries:
                    raise
                after = e.response.headers.get("retry-after", "")
                await self._sleep(min(10.0, float(after)) if after.isdigit() else 2.0 * (2 ** attempt))
            except httpx.TransportError:
                if attempt == cfg.retries:
                    raise
                await self._sleep(1.5 * (attempt + 1))

    async def _searxng(self, address: str, query: str):
        when = f"&time_range={RECENCY[self._recency][0]}" if self._recency else ""
        resp = await self._http.get(f"{address}/search?q={quote_plus(query)}&format=json{when}", headers={"Accept": "application/json"})
        resp.raise_for_status()
        body = resp.json()
        rows = [{"title": str(r.get("title", ""))[:200], "url": str(r.get("url", "")),
                 "snippet": str(r.get("content", ""))[:400]} for r in body.get("results", [])]
        down = [f"{e[0]} {e[1]}" for e in body.get("unresponsive_engines", []) if isinstance(e, list) and len(e) >= 2]
        return rows, down

    async def _brave(self, cfg: SearchConfig, query: str) -> List[Dict[str, str]]:
        resp = await self._http.get(
            BRAVE_URL, params={"q": query, "count": cfg.max_results, **({"freshness": RECENCY[self._recency][1]} if self._recency else {})},
            headers={"Accept": "application/json", "X-Subscription-Token": cfg.api_key})
        resp.raise_for_status()
        return [{"title": str(r.get("title", ""))[:200], "url": str(r.get("url", "")),
                 "snippet": str(r.get("description", ""))[:400]} for r in resp.json().get("web", {}).get("results", [])]

    async def _duckduckgo(self, cfg: SearchConfig, query: str) -> List[Dict[str, str]]:
        """DuckDuckGo's plain HTML results page: no key needed, but it may refuse a busy server."""
        resp = await self._http.post(DDG_URL, data={"q": query, **({"df": RECENCY[self._recency][2]} if self._recency else {})}, headers={"User-Agent": DDG_UA, "Accept-Language": "en-US,en;q=0.9"})
        resp.raise_for_status()
        rows: List[Dict[str, str]] = []
        for href, title, rest in _DDG_RESULT.findall(resp.text):
            link = htmllib.unescape(href)
            if "uddg=" in link:                                   # their redirect wrapper holds the real address
                link = (parse_qs(urlsplit(link if "://" in link else "https:" + link).query).get("uddg") or [""])[0]
            if not link.startswith("http"):
                continue
            snip = _DDG_SNIPPET.search(rest)
            clean = lambda t: " ".join(htmllib.unescape(re.sub(r"<[^>]+>", "", t)).split())
            rows.append({"title": clean(title)[:200], "url": link, "snippet": clean(snip.group(1))[:400] if snip else ""})
        return rows
