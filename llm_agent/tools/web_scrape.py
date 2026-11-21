"""Scrape one web page through Olostep (handles JavaScript-built pages, anti-bot walls and PDFs that a plain fetch cannot read).

The free plan allows a fixed number of requests per month, so this is a metered resource: every call is counted in the
database *before* it is made, refused once the month's allowance minus a reserve is used, and refunded when the service
did not do the work. Pages are cached for a few minutes so asking for the rest of a long page costs nothing.
"""
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from llm_agent.config import SystemConfig
from llm_agent.db import Database

API_URL = "https://api.olostep.com/v1/scrapes"
PAGE_CHARS = 12000
CACHE_SECONDS = 600
CACHE_PAGES = 30
_PRIVATE = re.compile(r"^(localhost|.*\.local|.*\.internal)$|^(\d{1,3}\.){3}\d{1,3}$|^\[", re.I)


class Scraper:
    def __init__(self, db: Database, get_cfg: Callable[[], SystemConfig], client: Optional[httpx.AsyncClient] = None,
                 key: Callable[[], str] = lambda: os.environ.get("OLOSTEP_API_KEY", ""), clock: Callable[[], float] = time.time):
        self.db = db
        self._cfg = get_cfg
        self._key = key
        self._clock = clock
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(70, connect=8), follow_redirects=False, trust_env=False)
        self._cache: "OrderedDict[str, Tuple[float, str, str]]" = OrderedDict()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().scrape_enabled and self._key())

    def _stamp(self) -> Tuple[str, str]:
        d = datetime.fromtimestamp(self._clock(), timezone.utc)
        return d.strftime("%Y-%m"), d.strftime("%Y-%m-%d")

    async def usage(self) -> Dict[str, Any]:
        cfg, (month, _) = self._cfg(), self._stamp()
        row = await self.db.fetchone("SELECT COALESCE(SUM(credits),0) AS n, COUNT(*) AS calls FROM scrape_usage WHERE month=?", (month,))
        stop_at = max(0, cfg.scrape_monthly_limit - cfg.scrape_reserve)
        return {"month": month, "used": row["n"], "calls": row["calls"], "limit": cfg.scrape_monthly_limit, "stops_at": stop_at,
                "remaining": max(0, stop_at - row["n"]), "enabled": self.enabled}

    @staticmethod
    def _clean_url(url: str) -> str:
        url = (url or "").strip()
        if url and "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or _PRIVATE.match(parts.hostname) or "." not in parts.hostname:
            raise ValueError("Give a full public web address (https://...).")
        return url

    async def _reserve(self, user_id: str, host: str) -> Tuple[Optional[int], str]:
        """Count the request now (so parallel calls cannot overshoot). Returns (row id, "") or (None, why not)."""
        cfg, (month, day) = self._cfg(), self._stamp()
        mine = await self.db.fetchone("SELECT COUNT(*) AS n FROM scrape_usage WHERE day=? AND user_id=?", (day, user_id))
        if mine["n"] >= cfg.scrape_user_daily:
            return None, f"Daily scrape limit reached ({cfg.scrape_user_daily} per user). Use read_url or web_search instead."
        stop_at = max(0, cfg.scrape_monthly_limit - cfg.scrape_reserve)

        def txn(c) -> Optional[int]:
            used = c.execute("SELECT COALESCE(SUM(credits),0) FROM scrape_usage WHERE month=?", (month,)).fetchone()[0]
            if used >= stop_at:
                return None
            return c.execute("INSERT INTO scrape_usage(month, day, user_id, host, credits, ts) VALUES (?,?,?,?,1,?)",
                             (month, day, user_id, host[:100], self._clock())).lastrowid
        rid = await self.db.transaction(txn)
        if rid is None:
            return None, "The monthly page-scraping allowance is nearly used up, so it is switched off until next month. Use read_url or web_search."
        return rid, ""

    async def scrape(self, user_id: str, url: str, offset: int = 0, wait_ms: int = 0) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "error", "error": "Page scraping is not set up on this server. Use read_url or web_search."}
        try:
            url = self._clean_url(url)
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        offset = max(0, int(offset or 0))
        hit = self._cache.get(url)
        if hit and self._clock() - hit[0] < CACHE_SECONDS:
            return self._page(url, hit[1], offset, cached=True)
        rid, why = await self._reserve(user_id, urlsplit(url).hostname or "")
        if rid is None:
            return {"status": "error", "error": why}
        refund, out = True, {}
        try:
            body: Dict[str, Any] = {"url_to_scrape": url, "formats": ["markdown"]}
            if wait_ms:
                body["wait_before_scraping"] = max(0, min(int(wait_ms), 10000))
            r = await self._http.post(API_URL, json=body, headers={"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"})
            if r.status_code == 402:
                out = {"status": "error", "error": "The scraping service rejected the key or its allowance is used up."}
            elif r.status_code == 429:
                out = {"status": "error", "error": "The scraping service is busy; try again later or use read_url."}
            elif r.status_code == 504:
                out = {"status": "error", "error": "The page took too long to scrape. Try read_url, or a different page."}
            elif r.status_code >= 400:
                msg = ""
                try:
                    msg = str((r.json().get("error") or {}).get("message") or "")[:200]
                except Exception:
                    pass
                out = {"status": "error", "error": f"The scraping service could not read that page (HTTP {r.status_code}). {msg}".strip()}
            else:
                res = (r.json() or {}).get("result") or {}
                text = (res.get("markdown_content") or "").strip()
                code = (res.get("page_metadata") or {}).get("status_code")
                if isinstance(code, int) and code >= 400:
                    out = {"status": "error", "error": f"The site answered HTTP {code}."}
                    refund = False                                  # the service did the work
                elif not text:
                    out = {"status": "error", "error": "The page had no readable text."}
                    refund = False
                else:
                    refund = False
                    credits = (r.json() or {}).get("credits_consumed")
                    if isinstance(credits, int) and credits > 1:
                        await self.db.execute("UPDATE scrape_usage SET credits=? WHERE id=?", (min(credits, 50), rid))
                    self._cache[url] = (self._clock(), text, "")
                    while len(self._cache) > CACHE_PAGES:
                        self._cache.popitem(last=False)
                    out = self._page(url, text, offset)
        except (httpx.HTTPError, ValueError) as e:
            out = {"status": "error", "error": f"Could not reach the scraping service ({type(e).__name__})."}
        finally:
            if refund:                                              # nothing was scraped, so it must not use up the allowance
                await self.db.execute("DELETE FROM scrape_usage WHERE id=?", (rid,))
        if out.get("status") == "success":
            u = await self.usage()
            out["scrapes_left_this_month"] = u["remaining"]
        return out

    @staticmethod
    def _page(url: str, text: str, offset: int, cached: bool = False) -> Dict[str, Any]:
        chunk = text[offset:offset + PAGE_CHARS]
        nxt = offset + PAGE_CHARS if offset + PAGE_CHARS < len(text) else None
        out: Dict[str, Any] = {"status": "success", "url": url, "content": chunk, "total_chars": len(text), "next_offset": nxt}
        if cached:
            out["note"] = "Served from the recent scrape; no allowance used."
        if nxt is not None:
            out["note"] = (out.get("note", "") + f" Page continues; call scrape_page again with offset={nxt} (free, from the same scrape).").strip()
        return out
