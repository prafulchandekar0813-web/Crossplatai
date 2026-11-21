"""Web search through a configured provider (SearXNG or Brave). With no provider the tool is simply not offered."""
import json
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus

import httpx

from llm_agent.config import SearchConfig

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class WebSearch:
    def __init__(self, get_cfg: Callable[[], SearchConfig], client: Optional[httpx.AsyncClient] = None):
        self._get_cfg = get_cfg
        self._http = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    @property
    def enabled(self) -> bool:
        return bool(self._get_cfg().provider)

    @property
    def provider(self) -> str:
        return self._get_cfg().provider

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str) -> Dict[str, Any]:
        cfg = self._get_cfg()
        query = " ".join((query or "").split())[:300]
        if not query:
            return {"status": "error", "error": "query is empty"}
        if not cfg.provider:
            return {"status": "error", "error": "Web search is not configured on this server."}
        try:
            if cfg.provider == "searxng":
                results = await self._searxng(cfg, query)
            elif cfg.provider == "brave":
                results = await self._brave(cfg, query)
            else:
                return {"status": "error", "error": f"Unknown search provider '{cfg.provider}'"}
        except httpx.HTTPStatusError as e:
            hint = " (SearXNG needs the 'json' format enabled in its settings)" if cfg.provider == "searxng" and e.response.status_code == 403 else ""
            return {"status": "error", "error": f"Search provider returned HTTP {e.response.status_code}{hint}"}
        except json.JSONDecodeError:
            hint = ("the SearXNG address is probably wrong, or its 'json' output format is disabled (add json to search.formats in its settings.yml)"
                    if cfg.provider == "searxng" else "the provider sent an unexpected reply")
            return {"status": "error", "error": f"The search server did not return search results: {hint}. "
                                                "web_search only takes search words; to read a web address use read_url."}
        except (httpx.HTTPError, ValueError) as e:
            return {"status": "error", "error": f"Search request failed ({type(e).__name__}). Check the search server address in the admin settings."}
        return {"status": "success", "query": query, "provider": cfg.provider, "results": results[:cfg.max_results]}

    async def _searxng(self, cfg: SearchConfig, query: str) -> List[Dict[str, str]]:
        url = cfg.base_url.rstrip("/") + f"/search?q={quote_plus(query)}&format=json"
        resp = await self._http.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return [{"title": str(r.get("title", ""))[:200], "url": str(r.get("url", "")),
                 "snippet": str(r.get("content", ""))[:400]} for r in resp.json().get("results", [])]

    async def _brave(self, cfg: SearchConfig, query: str) -> List[Dict[str, str]]:
        resp = await self._http.get(
            BRAVE_URL, params={"q": query, "count": cfg.max_results},
            headers={"Accept": "application/json", "X-Subscription-Token": cfg.api_key})
        resp.raise_for_status()
        return [{"title": str(r.get("title", ""))[:200], "url": str(r.get("url", "")),
                 "snippet": str(r.get("description", ""))[:400]} for r in resp.json().get("web", {}).get("results", [])]
