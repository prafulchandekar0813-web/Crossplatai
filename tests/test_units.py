"""Unit tests: circuit breaker, context manager, LLM client, knowledge base ranking, configuration."""
import json

import httpx
import pytest

from llm_agent.circuit_breaker.circuit_breaker import AgentCircuitBreaker
from llm_agent.circuit_breaker.loop_detector import ToolLoopDetector
from llm_agent.config import CircuitBreakerConfig, ContextConfig, OpenAIConfig, SystemConfig
from llm_agent.context.context_manager import COMPACTED_STUB, ContextManager
from llm_agent.core.llm_client import LLMClient, LLMError, LLMNotConfigured, resolve_endpoint
from llm_agent.settings import ConfigError, Settings
from llm_agent.tools.rag_search import KnowledgeBase


# ---- circuit breaker -------------------------------------------------------------------------------
def test_turn_cap_and_fresh_breaker_per_task():
    cb = AgentCircuitBreaker(CircuitBreakerConfig(max_total_turns=3))
    assert [cb.check_turn().allowed for _ in range(4)] == [True, True, True, False] and cb.tripped
    assert AgentCircuitBreaker(CircuitBreakerConfig(max_total_turns=3)).check_turn().allowed


def test_duplicate_detection_includes_alternating_loops():
    d = ToolLoopDetector(3)
    results = [d.inspect_call(n, {"a": 1})[0] for n in ["x", "y", "x", "y", "x"]]
    assert results == [False, False, False, False, True]  # x seen 3 times inside the window
    assert not ToolLoopDetector(3).inspect_call("x", {"a": 1})[0]


def test_consecutive_failures_and_tool_cap():
    cb = AgentCircuitBreaker(CircuitBreakerConfig(max_consecutive_failures=2, max_tool_calls=2))
    assert cb.check_turn().allowed
    cb.record_result(True); cb.record_result(True)
    assert not cb.check_turn().allowed
    cb = AgentCircuitBreaker(CircuitBreakerConfig(max_tool_calls=2))
    assert cb.check_tool_call("a", {"i": 1}).allowed and cb.check_tool_call("a", {"i": 2}).allowed
    assert not cb.check_tool_call("a", {"i": 3}).allowed


# ---- context manager ---------------------------------------------------------------------------------
def test_truncation_and_compaction():
    cm = ContextManager(ContextConfig(max_context_tokens=3000, summary_trigger_tokens=1500, max_tool_output_length=100))
    assert "truncated" in cm.truncate_tool_output("x" * 500) and cm.truncate_tool_output("short") == "short"
    msgs = [{"role": "system", "content": "s"}]
    for i in range(6):
        msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": str(i), "function": {"name": "t", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": str(i), "content": "x" * 900}]
    n, over = cm.compact(msgs)
    assert n > 0 and not over
    tools = [m for m in msgs if m["role"] == "tool"]
    assert tools[0]["content"] == COMPACTED_STUB and tools[-1]["content"] != COMPACTED_STUB  # newest results survive


async def test_history_is_summarised_when_too_long():
    cm = ContextManager(ContextConfig(summary_trigger_tokens=700, sliding_window_messages=4, max_context_tokens=2000))
    rows = [{"id": i, "role": "user" if i % 2 else "assistant", "content": f"message {i} " + "word " * 100} for i in range(1, 21)]
    seen = {}

    async def summarizer(text):
        seen["text"] = text
        return "SUMMARY"
    msgs, summary, new = await cm.prepare_history(rows, "", summarizer, reserved_tokens=100)
    assert summary == "SUMMARY" and new == (16, "SUMMARY") and "message 1 " in seen["text"]
    assert msgs[0]["role"] == "user" and "message 20" in msgs[-1]["content"] and len(msgs) <= 4


def test_merge_adjacent_roles():
    out = ContextManager.merge_adjacent([{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                                         {"role": "assistant", "content": "c"}])
    assert out == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}]


# ---- LLM client --------------------------------------------------------------------------------------
def client_for(handler, **cfg):
    conf = OpenAIConfig(base_url="http://m/v1", model="m", max_retries=2, **cfg)
    return LLMClient(lambda: conf, transport=httpx.MockTransport(handler), backoff=0.0)


def ok(content="hi"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def test_endpoint_resolution():
    assert resolve_endpoint("http://x/v1") == "http://x/v1/chat/completions"
    assert resolve_endpoint("http://x/v1/") == "http://x/v1/chat/completions"
    assert resolve_endpoint("http://x/v1/chat/completions") == "http://x/v1/chat/completions"
    assert resolve_endpoint("http://x/v1/chat/completion") == "http://x/v1/chat/completions"


async def test_retries_on_503_then_succeeds_and_sends_auth():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, json={"error": {"message": "busy"}}) if len(calls) < 3 else ok()
    c = client_for(handler, api_key="sk-1")
    assert (await c.chat([{"role": "user", "content": "x"}])).message["content"] == "hi"
    assert len(calls) == 3 and calls[0].headers["authorization"] == "Bearer sk-1"


async def test_no_retry_on_401_and_error_is_friendly():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})
    with pytest.raises(LLMError, match="bad key.*API key"):
        await client_for(handler).chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1


async def test_tools_rejected_falls_back_and_flags_it():
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(400, json={"error": {"message": "no tools"}}) if "tools" in body else ok("plain")
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    resp = await client_for(handler).chat([{"role": "user", "content": "x"}], tools)
    assert resp.tools_dropped and resp.message["content"] == "plain"
    with pytest.raises(LLMError):
        await client_for(handler, retry_without_tools=False).chat([{"role": "user", "content": "x"}], tools)


async def test_connection_failure_is_explained_and_unconfigured_is_clear():
    def handler(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(LLMError, match="Cannot connect"):
        await client_for(handler).chat([{"role": "user", "content": "x"}])
    unconfigured = LLMClient(lambda: OpenAIConfig())
    with pytest.raises(LLMNotConfigured, match="Admin"):
        await unconfigured.chat([{"role": "user", "content": "x"}])


async def test_bad_payload_shapes():
    with pytest.raises(LLMError, match="unexpected format"):
        await client_for(lambda r: httpx.Response(200, json={"nope": 1})).chat([{"role": "user", "content": "x"}])
    with pytest.raises(LLMError, match="did not return JSON"):
        await client_for(lambda r: httpx.Response(200, text="<html>")).chat([{"role": "user", "content": "x"}])


# ---- knowledge base ----------------------------------------------------------------------------------
async def test_bm25_ranks_the_relevant_document_first(tmp_path):
    kb = KnowledgeBase(str(tmp_path))
    assert "empty" in kb.search("anything")["note"]
    await kb.add_document("cooking.md", b"# Pasta\n\nBoil water, add salt, cook the pasta for ten minutes.")
    await kb.add_document("agents.md", b"# Circuit breakers\n\nA circuit breaker stops an agent that repeats the same tool call.\n\n"
                                       b"# Memory\n\nLong term memory keeps facts; transient traces decay.")
    top = kb.search("why does the agent stop repeating tool calls")["matches"]
    assert top[0]["filename"] == "agents.md" and top[0]["section"] == "Circuit breakers"
    assert kb.search("zzzz qqqq")["matches"] == []
    kb.delete_document("agents.md")
    assert all(m["filename"] != "agents.md" for m in kb.search("circuit breaker")["matches"])


async def test_html_and_pdf_extraction(tmp_path):
    kb = KnowledgeBase(str(tmp_path))
    r = await kb.add_document("page.html", b"<html><script>evil()</script><body><h1>Title</h1><p>Visible text</p></body></html>")
    assert r["converter"] == "html"
    text = (tmp_path / "text" / "page.html.md").read_text()
    assert "Visible text" in text and "evil" not in text
    from llm_agent.tools.rag_search import KnowledgeError
    with pytest.raises(KnowledgeError):
        await kb.add_document("broken.pdf", b"%PDF-not really")


# ---- configuration -----------------------------------------------------------------------------------
def test_strict_updates_reject_bad_values_and_change_nothing(tmp_path):
    s = Settings(str(tmp_path / "c.json"))
    before = s.effective.openai.temperature
    for bad in ({"openai": {"temperature": "hot"}}, {"openai": {"temperature": 5}}, {"circuit_breaker": {"max_total_turns": 0}},
                {"openai": {"base_url": "ftp://x"}}, {"context": {"summary_trigger_tokens": 99999999}}):
        with pytest.raises(ConfigError):
            s.update(bad)
    assert s.effective.openai.temperature == before


def test_secrets_blank_keeps_null_clears_and_admin_is_protected(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    s = Settings(str(tmp_path / "c.json"))
    assert s.generated_admin_password and s.effective.admin.admin_password_hash.startswith("scrypt$")
    s.update({"openai": {"api_key": "sk-1"}})
    s.update({"openai": {"api_key": "", "model": "m"}})
    assert s.effective.openai.api_key == "sk-1"
    s.update({"admin": {"admin_password_hash": "evil"}})
    assert s.effective.admin.admin_password_hash.startswith("scrypt$")
    s.update({"openai": {"api_key": None}})
    assert s.effective.openai.api_key == ""
    s.update({"openai": {"api_key": "sk-2"}})
    s.reset()
    assert s.effective.openai.api_key == "sk-2" and s.effective.openai.model == ""
    assert Settings(str(tmp_path / "c.json")).generated_admin_password is None  # reload keeps the same admin


def test_env_overrides_win_and_are_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    s = Settings(str(tmp_path / "c.json"))
    assert s.effective.openai.model == "env-model" and "openai.model" in s.locked
    s.update({"openai": {"model": "file-model"}})
    assert s.effective.openai.model == "env-model"
    assert json.loads((tmp_path / "c.json").read_text())["openai"]["model"] == "file-model"
    monkeypatch.delenv("OPENAI_MODEL")
    s.reload()
    assert s.effective.openai.model == "file-model"


def test_damaged_config_file_does_not_stop_the_server(tmp_path):
    (tmp_path / "c.json").write_text("{ not json")
    assert Settings(str(tmp_path / "c.json")).effective.max_subagents_per_user == 4
    (tmp_path / "d.json").write_text(json.dumps({"openai": {"temperature": "oops"}, "bogus": 1}))
    assert Settings(str(tmp_path / "d.json")).effective.openai.temperature == SystemConfig().openai.temperature


async def test_office_files_are_indexed_without_docling(tmp_path):
    import io
    import zipfile
    from llm_agent.tools import rag_search

    def make(files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, c in files.items():
                z.writestr(n, c)
        return buf.getvalue()

    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    docx = make({"word/document.xml": f'<w:document {ns}><w:body><w:p><w:r><w:t>Quarterly pumpkin report</w:t></w:r></w:p></w:body></w:document>'})
    xlsx = make({"xl/sharedStrings.xml": '<sst xmlns="x"><si><t>Widget</t></si></sst>',
                 "xl/worksheets/sheet1.xml": '<worksheet xmlns="x"><sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>'})
    kb = rag_search.KnowledgeBase(str(tmp_path))
    if kb.docling_available():
        return
    await kb.add_document("r.docx", docx)
    await kb.add_document("s.xlsx", xlsx)
    assert kb.search("pumpkin report")["matches"][0]["filename"] == "r.docx"
    assert "Widget | 42" in (tmp_path / "text" / "s.xlsx.md").read_text()


# ---- web reader / search -----------------------------------------------------------------------------
def _fetcher(handler, table):
    import httpx
    from llm_agent.tools.web_fetch import WebFetch

    async def resolver(host, port):
        return table.get(host, ["93.184.216.34"])
    return WebFetch(client=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False), resolver=resolver)


async def test_read_url_extracts_text_and_pages():
    import httpx
    html = "<html><script>x=1</script><body><h1>Hello</h1><p>" + "word " * 5000 + "</p></body></html>"
    f = _fetcher(lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=html), {})
    r = await f.read("https://example.org/a")
    assert r["status"] == "success" and r["content"].startswith("Hello") and "x=1" not in r["content"] and r["next_offset"]
    r2 = await f.read("https://example.org/a", r["next_offset"])
    assert r2["status"] == "success" and "word" in r2["content"][:10]


async def test_read_url_blocks_private_addresses_and_redirects_into_them():
    import httpx
    f = _fetcher(lambda req: httpx.Response(302, headers={"location": "http://internal.local/secret"}), {"internal.local": ["10.0.0.5"], "localhost": ["127.0.0.1"]})
    assert "public internet" in (await f.read("http://localhost:8080/api/admin/config"))["error"]
    f2 = _fetcher(lambda req: httpx.Response(200, text="x"), {"169.254.169.254": ["169.254.169.254"]})
    assert "public internet" in (await f2.read("http://169.254.169.254/latest/meta-data"))["error"]
    r = await f.read("https://example.org/redirect")  # public host redirecting to a private one
    assert r["status"] == "error" and "public internet" in r["error"]
    assert (await f.read("file:///etc/passwd"))["status"] == "error"
    assert (await f.read("https://user:pw@example.org/"))["status"] == "error"


async def test_read_url_github_repo_returns_readme():
    import httpx
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="# Pero\n\nA tool.")
    r = await _fetcher(handler, {}).read("https://github.com/phodal/pero")
    assert r["status"] == "success" and "A tool." in r["content"] and "raw.githubusercontent.com/phodal/pero" in seen[0]


async def test_web_search_explains_non_json_reply():
    import httpx
    from llm_agent.config import SearchConfig
    from llm_agent.tools.web_search import WebSearch
    cfg = SearchConfig(provider="searxng", base_url="http://s.example")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>hi</html>")))
    r = await WebSearch(lambda: cfg, client=client).search("https://github.com/phodal/pero")
    assert r["status"] == "error" and "json" in r["error"].lower() and "read_url" in r["error"]


def test_plan_task_does_not_fail_on_a_missing_title():
    from llm_agent.tools.registry import build_registry, validate_args
    spec = build_registry()["plan_task"]
    assert validate_args(spec, {"steps": ["a", "b"], "files": ["x"]}) is None
    assert validate_args(spec, {"task_title": "T"}) is not None  # steps are still required
