import httpx

from llm_agent.config import SearchConfig
from llm_agent.tools.web_search import WebSearch


def make(handler, **cfg):
    slept = []

    async def sleep(s):
        slept.append(round(s, 2))
    config = SearchConfig(provider="searxng", base_url=cfg.pop("base_url", "http://a.example"), **cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebSearch(lambda: config, client=client, sleep=sleep), slept


def ok(results):
    return httpx.Response(200, json={"results": [{"title": t, "url": f"https://x/{t}", "content": "c"} for t in results]})


async def test_a_busy_provider_is_retried_after_a_pause_and_then_answers():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "3"}) if len(calls) < 3 else ok(["flutter"])
    ws, slept = make(handler)
    r = await ws.search("flutter docs")
    assert r["status"] == "success" and r["results"][0]["title"] == "flutter" and len(calls) == 3 and slept.count(3.0) == 2


async def test_the_next_address_is_tried_when_one_keeps_saying_429_or_returns_nothing():
    def handler(req):
        if req.url.host == "a.example":
            return httpx.Response(429)
        if req.url.host == "b.example":
            return ok([])
        return ok(["found"])
    ws, _ = make(handler, base_url="http://a.example http://b.example, http://c.example", retries=0)
    r = await ws.search("quick commerce app")
    assert r["status"] == "success" and r["results"][0]["title"] == "found"
    seen = []
    ws2, _ = make(lambda req: (seen.append(req.url.host), ok(["z"]))[1], base_url="http://a.example http://c.example")
    await ws2.search("one"); await ws2.search("two")
    assert seen == ["a.example", "a.example"]                      # the address that works stays first


async def test_still_rate_limited_gives_a_clear_instruction_and_empty_results_explain_themselves():
    ws, _ = make(lambda req: httpx.Response(429), retries=1)
    r = await ws.search("anything")
    assert r["status"] == "error" and "429" in r["error"] and "read_url" in r["error"]
    ws, _ = make(lambda req: httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}))
    r = await ws.search("a very long query with many many words")
    assert r["status"] == "success" and r["results"] == [] and "google CAPTCHA" in r["note"] and "3 to 6 plain words" in r["note"]


async def test_searches_are_spaced_out_and_repeats_come_from_the_cache():
    calls = []
    ws, slept = make(lambda req: (calls.append(1), ok(["r"]))[1], min_interval_seconds=1.5)
    await ws.search("one"); await ws.search("two")
    again = await ws.search("ONE")
    assert len(calls) == 2 and again["cached"] is True


DDG_PAGE = ('<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fflask.palletsprojects.com%2Fen%2F3.0.x%2F&rut=x">Flask <b>3.0</b> docs</a>'
            '<a class="result__snippet" href="x">Welcome to <b>Flask</b></a></div>'
            '<div class="result"><a class="result__a" href="https://example.org/two">Two</a><td class="result__snippet">Second</td></div>')


async def test_duckduckgo_works_without_a_key_and_unwraps_its_redirect_links():
    config = SearchConfig(provider="duckduckgo")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=DDG_PAGE)))
    r = await WebSearch(lambda: config, client=client).search("flask docs")
    assert r["provider"] == "duckduckgo" and r["results"][0] == {"title": "Flask 3.0 docs", "url": "https://flask.palletsprojects.com/en/3.0.x/", "snippet": "Welcome to Flask", "site": "flask.palletsprojects.com"}
    assert r["results"][1]["url"] == "https://example.org/two"


async def test_duckduckgo_is_the_backup_when_the_chosen_provider_fails():
    def handler(req):
        return httpx.Response(200, text=DDG_PAGE) if req.url.host == "html.duckduckgo.com" else httpx.Response(500)
    ws, _ = make(handler, retries=0)
    r = await ws.search("flask docs")
    assert r["status"] == "success" and r["provider"] == "duckduckgo" and "backup" not in r.get("error", "") and r["results"]


async def test_results_are_tidied_and_recency_is_sent_to_the_provider():
    from llm_agent.tools.web_search import tidy
    rows = [{"title": "a", "url": "https://www.x.com/p/?utm_source=z&id=1#top"}, {"title": "dup", "url": "https://x.com/p?id=1"},
            {"title": "b", "url": "https://x.com/q"}, {"title": "c", "url": "https://x.com/r"}, {"title": "bad", "url": "javascript:1"}, {"title": "d", "url": "https://y.org/"}]
    out = tidy(rows, 10)
    assert [r["url"] for r in out] == ["https://www.x.com/p/?id=1", "https://x.com/q", "https://y.org/"] and out[0]["site"] == "x.com"
    seen = []
    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"results": [{"title": "t", "url": "https://z.org/", "content": "c"}]})
    ws, _ = make(handler)
    r = await ws.search("election news", recency="week")
    assert r["results"][0]["site"] == "z.org" and "time_range=week" in seen[0]
    await ws.search("election news")                                           # a different period is a different search
    assert "time_range" not in seen[1]


async def test_research_web_searches_reads_the_best_pages_and_keeps_matching_passages():
    from llm_agent.tools.web_fetch import WebFetch
    from llm_agent.tools.web_research import WebResearch, best_passages
    page = ("<main><p>Unrelated words about gardening and weather over a very long introduction paragraph that goes on.</p>"
            "<p>The Flask 3 release removed before_first_request, so run set-up code once at start-up instead.</p></main>")
    def handler(req):
        if "/search" in str(req.url):
            return httpx.Response(200, json={"results": [{"title": "Flask docs", "url": "https://flask.example/docs", "content": "s"},
                                                         {"title": "Broken", "url": "https://broken.example/x", "content": "s"},
                                                         {"title": "Other", "url": "https://other.example/y", "content": "s"}]})
        if "broken.example" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=page)
    ws, _ = make(handler)
    async def public(host, port):
        return ["93.184.216.34"]
    fetch = WebFetch(client=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False), resolver=public)
    r = await WebResearch(ws, fetch).run("flask before_first_request removed", pages=2)
    assert r["status"] == "success" and r["sources"][0]["url"] == "https://flask.example/docs"
    assert "before_first_request" in r["sources"][0]["extract"] and r["could_not_open"][0]["url"] == "https://broken.example/x"
    assert r["more_results"][0]["url"] == "https://other.example/y"
    assert best_passages("tiny", "anything") == "tiny"
