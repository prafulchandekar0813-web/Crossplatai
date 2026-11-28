"""One call for a normal question: search, open the best few pages in parallel, and return the passages that match the question.

Saves the assistant several turns (search, read, read, read) and keeps what comes back short: only the paragraphs that share words
with the question, in page order, from each page."""
import asyncio
import re
from typing import Any, Dict, List

from llm_agent.tools.web_fetch import WebFetch
from llm_agent.tools.web_search import WebSearch

MAX_PAGES, EXTRACT_CHARS = 4, 2200
_WORDS = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
STOP = set("the a an and or of to in on for with is are was were be what how why when who which do does did can i you it this that from by at as about".split())


def terms(text: str) -> set:
    return {w for w in _WORDS.findall(text.lower()) if len(w) > 2 and w not in STOP}


def best_passages(text: str, query: str, limit: int = EXTRACT_CHARS) -> str:
    """The paragraphs that best match the query (kept in page order, cut to `limit` characters)."""
    want = terms(query)
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) > 40]
    if not paras:
        return text[:limit]
    scored = sorted(((len(want & terms(p)) / (1 + len(p) / 1500), i) for i, p in enumerate(paras)), reverse=True)
    keep, used = [], 0
    for score, i in scored:
        if used >= limit or (score == 0 and keep):
            break
        keep.append(i)
        used += len(paras[i])
    return "\n\n".join(paras[i][:1200] for i in sorted(keep))[:limit]


class WebResearch:
    def __init__(self, search: WebSearch, fetch: WebFetch):
        self.search, self.fetch = search, fetch

    async def run(self, query: str, pages: int = 3, recency: str = "") -> Dict[str, Any]:
        found = await self.search.search(query, recency)
        if found["status"] != "success":
            return found
        results: List[Dict[str, str]] = found["results"]
        if not results:
            return {"status": "success", "query": query, "sources": [], "note": found.get("note", "The search returned nothing.")}
        chosen = results[:max(1, min(int(pages or 3), MAX_PAGES))]
        pages_read = await asyncio.gather(*(self.fetch.read(r["url"]) for r in chosen))
        sources, failed = [], []
        for r, page in zip(chosen, pages_read):
            if page.get("status") != "success":
                failed.append({"title": r["title"], "url": r["url"], "snippet": r.get("snippet", ""), "problem": page.get("error", "")})
                continue
            src = {"title": page.get("title") or r["title"], "url": page["url"], "extract": best_passages(page["content"], query)}
            if page.get("note"):
                src["note"] = page["note"]
            sources.append(src)
        out: Dict[str, Any] = {"status": "success", "query": query, "sources": sources,
                               "more_results": [{"title": r["title"], "url": r["url"], "snippet": r.get("snippet", "")} for r in results[len(chosen):]]}
        if failed:
            out["could_not_open"] = failed
        if not sources:
            out["note"] = "None of the top pages could be opened; the snippets above may be enough, or try read_url on another result."
        return out
