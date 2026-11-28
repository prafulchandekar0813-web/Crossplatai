from llm_agent.circuit_breaker.call_cache import CallCache
from llm_agent.circuit_breaker.circuit_breaker import AgentCircuitBreaker
from llm_agent.config import CircuitBreakerConfig


def test_identical_web_call_is_served_again_and_never_counted():
    c, b = CallCache(), AgentCircuitBreaker(CircuitBreakerConfig())
    assert c.lookup("web_search", {"query": "flutter"}) is None
    assert b.check_tool_call("web_search", {"query": "flutter"}).allowed
    c.store("web_search", {"query": "flutter"}, {"status": "success", "results": [1]})
    for _ in range(3):
        hit = c.lookup("web_search", {"query": "flutter"})
        assert hit["results"] == [1] and hit["repeated"] and "Do not repeat" in hit["note"]
        assert b.note_repeat("web_search").allowed
    assert b.tool_calls == 1 and b.consecutive_failures == 0


def test_asking_again_and_again_is_a_loop_but_a_fresh_call_resets_it():
    b = AgentCircuitBreaker(CircuitBreakerConfig())
    for _ in range(3):
        assert b.note_repeat("read_file").allowed
    b.check_tool_call("list_files", {})
    assert b.repeats == 0
    for _ in range(3):
        assert b.note_repeat("read_file").allowed
    assert not b.note_repeat("read_file").allowed and b.tripped


def test_file_results_go_stale_after_a_change_but_web_results_do_not():
    c = CallCache()
    c.store("read_file", {"path": "a"}, {"status": "success", "content": "old"})
    c.store("web_search", {"query": "q"}, {"status": "success"})
    assert c.lookup("read_file", {"path": "a"})
    c.store("patch_file", {"path": "a"}, {"status": "success"})
    assert c.lookup("read_file", {"path": "a"}) is None
    assert c.lookup("web_search", {"query": "q"})


def test_a_failed_change_leaves_reads_valid_and_a_repeated_failure_is_remembered():
    c = CallCache()
    c.store("read_file", {"path": "a"}, {"status": "success"})
    c.store("patch_file", {"path": "a", "edits": [1]}, {"status": "error", "error": "not found"})
    assert c.lookup("read_file", {"path": "a"})
    again = c.lookup("patch_file", {"path": "a", "edits": [1]})
    assert again["status"] == "error" and again["repeated"]


def test_bash_is_never_cached():
    c = CallCache()
    c.store("bash", {"command": "ls"}, {"status": "success"})
    assert c.lookup("bash", {"command": "ls"}) is None
