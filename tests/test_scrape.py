"""Olostep page scraping with a hard monthly budget."""
import httpx
import pytest

from llm_agent.config import SystemConfig
from llm_agent.db import Database
from llm_agent.tools.registry import build_registry, definitions
from llm_agent.tools.web_scrape import Scraper


def make(tmp_path, handler, **cfg):
    db = Database(str(tmp_path / "t.db"))
    c = SystemConfig()
    for k, v in cfg.items():
        setattr(c, k, v)
    calls = []

    def h(req):
        calls.append(req)
        return handler(req)
    s = Scraper(db, lambda: c, client=httpx.AsyncClient(transport=httpx.MockTransport(h)), key=lambda: "k-secret", clock=lambda: 1_800_000_000)
    return s, calls, db


def ok(text="# Hello\nworld", credits=1):
    return lambda req: httpx.Response(200, json={"result": {"markdown_content": text, "page_metadata": {"status_code": 200}}, "credits_consumed": credits})


async def test_scrape_returns_markdown_and_sends_the_key_only_in_the_header(tmp_path):
    s, calls, _ = make(tmp_path, ok())
    r = await s.scrape("u1", "example.com/page")
    assert r["status"] == "success" and "Hello" in r["content"] and r["scrapes_left_this_month"] == 449
    req = calls[0]
    assert req.headers["authorization"] == "Bearer k-secret" and b"k-secret" not in req.content
    assert b"https://example.com/page" in req.content


async def test_stops_before_the_monthly_limit_and_never_calls_the_service_then(tmp_path):
    s, calls, _ = make(tmp_path, ok(), scrape_monthly_limit=5, scrape_reserve=2, scrape_user_daily=99)
    for i in range(3):
        assert (await s.scrape("u", f"https://e.com/{i}"))["status"] == "success"
    r = await s.scrape("u", "https://e.com/x")
    assert r["status"] == "error" and "allowance" in r["error"] and len(calls) == 3
    assert (await s.usage())["remaining"] == 0


async def test_daily_per_user_cap_and_cache_is_free(tmp_path):
    s, calls, _ = make(tmp_path, ok("x" * 30000), scrape_user_daily=2)
    a = await s.scrape("u", "https://e.com/a")
    assert a["next_offset"] == 12000
    b = await s.scrape("u", "https://e.com/a", offset=12000)               # rest of the same page: no new request
    assert b["status"] == "success" and len(calls) == 1 and "no allowance" in b["note"]
    await s.scrape("u", "https://e.com/b")
    assert "Daily" in (await s.scrape("u", "https://e.com/c"))["error"]
    assert (await s.scrape("other", "https://e.com/c"))["status"] == "success"


@pytest.mark.parametrize("status", [400, 402, 429, 502, 504])
async def test_failures_do_not_use_up_the_allowance(tmp_path, status):
    s, _, _ = make(tmp_path, lambda req: httpx.Response(status, json={"error": {"message": "nope"}}))
    r = await s.scrape("u", "https://e.com/a")
    assert r["status"] == "error" and "k-secret" not in str(r)
    assert (await s.usage())["used"] == 0


async def test_network_error_refunds_and_bad_urls_never_reach_the_service(tmp_path):
    def boom(req):
        raise httpx.ConnectError("x")
    s, calls, _ = make(tmp_path, boom)
    assert (await s.scrape("u", "https://e.com/a"))["status"] == "error" and (await s.usage())["used"] == 0
    for bad in ("", "ftp://x.com", "http://localhost/a", "http://127.0.0.1/", "https://user:pw@e.com/", "http://intranet/"):
        assert (await s.scrape("u", bad))["status"] == "error"
    assert len(calls) == 1


async def test_credits_consumed_count_and_disabled_without_key(tmp_path):
    s, _, _ = make(tmp_path, ok(credits=4))
    await s.scrape("u", "https://e.com/a")
    assert (await s.usage())["used"] == 4
    s2, calls, _ = make(tmp_path, ok())
    s2._key = lambda: ""
    assert not s2.enabled and (await s2.scrape("u", "https://e.com/a"))["status"] == "error" and not calls


def test_tool_only_offered_when_scraping_is_on():
    reg = build_registry()
    names = lambda on: {d["function"]["name"] for d in definitions(reg, "orchestrator", True, False, on)}
    assert "scrape_page" in names(True) and "scrape_page" not in names(False)
