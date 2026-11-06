"""End-to-end behaviour of the agent loop through the real HTTP API with a scripted model."""
import asyncio

import pytest


def types(events):
    return [e["type"] for e in events]


async def test_plain_chat_and_history_saved(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "hello there")
    final = next(e for e in events if e["type"] == "final")
    assert final["content"] == "Echo: hello there" and final["status"] == "completed"
    assert types(events)[0] == "run_started" and types(events)[-1] == "run_finished"
    msgs = (await c.get("/api/sessions/s1/messages")).json()["messages"]
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "ok"), ("assistant", "ok")]
    assert msgs[1]["run_id"] == events[0]["run_id"]
    sessions = (await c.get("/api/sessions")).json()["sessions"]
    assert sessions[0]["id"] == "s1" and sessions[0]["title"] == "hello there"


async def test_circuit_breaker_resets_between_tasks(env):
    """Original bug: the breaker counted forever and every sub-agent died after ~15 requests."""
    c = await env.signup("alice")
    for i in range(25):
        events = await env.chat(c, "s1", f"message {i}")
        assert next(e for e in events if e["type"] == "final")["status"] == "completed", i


async def test_multi_agent_build_runs_workers_in_parallel_and_writes_files(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "Build a todo app")
    t = types(events)
    assert "plan" in t
    queued = [e for e in events if e["type"] == "agent_queued"]
    assert {e["role"] for e in queued} == {"Backend developer", "Frontend developer"}
    finished = {e["agent"]: e for e in events if e["type"] == "agent_finished"}
    assert set(finished) == {"orchestrator", "worker_1", "worker_2"}
    assert all(f["status"] == "completed" for f in finished.values())
    # both workers started before either finished (parallel)
    first_finish = min(i for i, e in enumerate(events) if e["type"] == "agent_finished" and e["agent"] != "orchestrator")
    # (queuing both is deterministic; the exact start order depends on scheduling, so it is not asserted)
    queued_at = [i for i, e in enumerate(events) if e["type"] == "agent_queued"]
    assert len(queued_at) == 2 and max(queued_at) < first_finish
    ws = (await c.get("/api/workspace")).json()["entries"]
    assert {"todo/backend/app.py", "todo/frontend/index.html"} <= {e["path"] for e in ws}
    done = events[-1]
    assert done["type"] == "run_finished" and sorted(done["files"]) == ["todo/backend/app.py", "todo/frontend/index.html"]
    assert done["usage"]["llm_calls"] >= 6 and done["usage"]["prompt_tokens"] > 0
    assert "todo api" in (await c.get("/api/workspace/file", params={"path": "todo/backend/app.py"})).json()["content"]
    # the stored trace can be replayed after the run
    run_id = events[0]["run_id"]
    stored = (await c.get(f"/api/runs/{run_id}")).json()["run"]
    assert any(e["type"] == "plan" for e in stored["events"])


async def test_loop_is_stopped_gracefully_with_a_summary(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "please loop forever")
    breaker = [e for e in events if e["type"] == "breaker"]
    assert breaker and "loop" in breaker[0]["reason"].lower()
    final = next(e for e in events if e["type"] == "final")
    assert final["status"] == "partial" and "stopped early" in final["content"].lower()
    # the model was asked to wrap up WITHOUT tools
    last = env.fake.state.calls[-1]
    assert "tools" not in last and last["messages"][-1]["content"].startswith("[System notice]")
    # and the breaker does not stay tripped for the next task
    again = await env.chat(c, "s1", "hello")
    assert next(e for e in again if e["type"] == "final")["status"] == "completed"


async def test_invalid_tool_json_is_reported_to_model_not_executed(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "badjson test")
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["ok"] is False and "JSON" in end["summary"]
    assert next(e for e in events if e["type"] == "final")["content"] == "Recovered after the bad tool call."
    assert (await c.get("/api/workspace")).json()["entries"] == []  # nothing was written with default args


async def test_remember_tool_saves_fact_and_next_prompt_contains_it(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "please remember my name")
    assert any(e["type"] == "memory_saved" for e in events)
    facts = (await c.get("/api/memory")).json()["facts"]
    assert [f["fact"] for f in facts] == ["The user's name is Praful"] and facts[0]["category"] == "profile"
    await env.chat(c, "s2", "hello again")
    assert "The user's name is Praful" in env.fake.state.calls[-1]["messages"][0]["content"]
    # bob does not get alice's memory
    bob = await env.signup("bob")
    await env.chat(bob, "s1", "hello")
    assert "Praful" not in env.fake.state.calls[-1]["messages"][0]["content"]
    # user can delete memories
    assert (await c.delete(f"/api/memory/{facts[0]['id']}")).status_code == 200
    assert (await c.get("/api/memory")).json()["facts"] == []


async def test_llm_failure_is_an_honest_error_and_not_sent_back_to_model(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "fail now")
    final = next(e for e in events if e["type"] == "final")
    assert final["status"] == "error" and "boom" in final["content"] and "Task completed" not in final["content"]
    msgs = (await c.get("/api/sessions/s1/messages")).json()["messages"]
    assert msgs[-1]["status"] == "error"
    await env.chat(c, "s1", "hello")
    sent = env.fake.state.calls[-1]["messages"]
    assert not any("boom" in str(m.get("content")) for m in sent)  # errors never poison the model's context
    # consecutive user messages (the failed turn had no reply) are merged for strict chat templates
    roles = [m["role"] for m in sent if m["role"] != "system"]
    assert all(a != b for a, b in zip(roles, roles[1:]))


async def test_model_without_tool_support_degrades_with_a_warning(env):
    env.fake.state.reject_tools = True
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "hello")
    assert any(e["type"] == "warning" and "tool" in e["message"].lower() for e in events)
    assert next(e for e in events if e["type"] == "final")["content"] == "Echo: hello"


async def test_stop_button_cancels_a_running_task(env):
    c = await env.signup("alice")
    task = asyncio.create_task(c.post("/api/sessions/s1/chat", json={"prompt": "slow please"}))
    for _ in range(100):
        await asyncio.sleep(0.02)
        active = env.orch.runs.active_for("alice")
        if active:
            break
    assert active
    assert (await c.post(f"/api/runs/{active.id}/cancel")).json() == {"cancelled": True}
    resp = await task
    from tests.conftest import parse_sse
    events = parse_sse(resp.text)
    assert next(e for e in events if e["type"] == "final")["status"] == "cancelled"
    assert env.orch.runs.active_for("alice") is None
    again = await env.chat(c, "s1", "hello")  # can run again immediately
    assert next(e for e in again if e["type"] == "final")["status"] == "completed"


async def test_one_task_at_a_time_per_user(env):
    c = await env.signup("alice")
    task = asyncio.create_task(c.post("/api/sessions/s1/chat", json={"prompt": "slow please"}))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if env.orch.runs.active_for("alice"):
            break
    r = await c.post("/api/sessions/s2/chat", json={"prompt": "hi"})
    assert r.status_code == 409
    env.orch.runs.active_for("alice").task.cancel()
    await task


async def test_knowledge_base_flow(env):
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    doc = b"# Memory decay\n\nTransient run traces expire after a TTL while long-term facts are kept forever.\n"
    r = await admin.put("/api/knowledge/decay.md", content=doc)
    assert r.status_code == 200 and r.json()["converter"] == "text"
    assert [d["filename"] for d in (await admin.get("/api/knowledge")).json()["documents"]] == ["decay.md"]
    user = await env.signup("alice")
    # users never see what admins added, but the assistant can still use it (with the name hidden)
    assert (await user.get("/api/knowledge")).json()["documents"] == []
    found = (await user.get("/api/knowledge/search", params={"q": "how do traces expire"})).json()["matches"]
    assert found and found[0]["filename"] == "Shared knowledge"
    events = await env.chat(user, "s1", "kb question")
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["ok"] and "matching passage" in end["summary"]
    assert (await admin.delete("/api/knowledge/decay.md")).status_code == 200
    assert (await user.get("/api/knowledge/search", params={"q": "traces expire"})).json()["matches"] == []


async def test_user_knowledge_is_private_and_limited(env):
    alice, bob = await env.signup("alice"), await env.signup("bob")
    assert (await alice.put("/api/knowledge/mine.md", content=b"# Secret\n\nThe launch code is pineapple.")).status_code == 200
    info = (await alice.get("/api/knowledge")).json()
    assert [d["filename"] for d in info["documents"]] == ["mine.md"] and info["quota_bytes"] and info["usage_bytes"] > 0
    assert (await bob.get("/api/knowledge")).json()["documents"] == []
    assert (await bob.get("/api/knowledge/search", params={"q": "launch code pineapple"})).json()["matches"] == []
    assert (await alice.get("/api/knowledge/search", params={"q": "launch code pineapple"})).json()["matches"]
    assert (await bob.delete("/api/knowledge/mine.md")).status_code == 404
    # quota: 1 MB limit, second 700 KB file does not fit
    env.orch.settings.effective.kb_quota_mb = 1
    big = b"# Big\n\n" + b"word " * 140000
    assert (await alice.put("/api/knowledge/big1.md", content=big)).status_code == 200
    r = await alice.put("/api/knowledge/big2.md", content=big)
    assert r.status_code == 422 and "full" in r.json()["error"]
    assert (await alice.delete("/api/knowledge/big1.md")).status_code == 200


async def test_events_can_be_replayed_after_disconnect(env):
    c = await env.signup("alice")
    events = await env.chat(c, "s1", "hello")
    run_id = events[0]["run_id"]
    from tests.conftest import parse_sse
    replay = parse_sse((await c.get(f"/api/runs/{run_id}/events", params={"after": events[2]["seq"]})).text)
    assert [e["seq"] for e in replay] == [e["seq"] for e in events[3:]]


async def test_validation_limits(env):
    c = await env.signup("alice")
    assert (await c.post("/api/sessions/s1/chat", json={"prompt": "   "})).status_code == 400
    assert (await c.post("/api/sessions/s1/chat", json={"prompt": "x" * 9000})).status_code == 413
    assert (await c.post("/api/sessions/bad id!/chat", json={"prompt": "hi"})).status_code in (400, 404)
    assert (await c.post("/api/sessions/s1/chat", json={})).status_code == 422


async def test_unconfigured_model_gives_clear_message(env):
    env.settings.update({"openai": {"base_url": "", "model": ""}})
    c = await env.signup("alice")
    r = await c.post("/api/sessions/s1/chat", json={"prompt": "hi"})
    assert r.status_code == 503 and "Admin" in r.json()["error"]


async def test_delete_session_and_user_wipe(env):
    c = await env.signup("alice")
    await env.chat(c, "s1", "Build a todo app")
    assert (await c.delete("/api/sessions/s1")).status_code == 200
    assert (await c.get("/api/sessions")).json()["sessions"] == []
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    assert (await admin.delete("/api/admin/users/alice")).status_code == 200
    assert not (env.orch.workspace_root / "alice").exists()
    assert (await c.get("/api/sessions")).status_code == 401
