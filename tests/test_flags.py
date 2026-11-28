from llm_agent.flags import Flags, FlagError
import pytest


class Clock:
    t = 1_700_000_000.0

    def __call__(self):
        return self.t


async def test_flags_are_cleaned_merged_and_expire_after_30_days(env):
    clock = Clock()
    f = Flags(env.orch.db, clock)
    a = await f.record("wrong", "Patch_File", "The guide said email rita@example.com about key sk-abcdefghijklmnop12", "Use edit_lines", "patch_file")
    assert "rita@" not in a["note"] and "sk-abc" not in a["note"] and a["topic"] == "patch_file"
    await f.record("wrong", "patch_file", "The guide said email rita@example.com about key sk-abcdefghijklmnop12")
    rows = await f.list()
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["suggestion"] == "Use edit_lines"
    for bad in (("nonsense", "t", "a long enough note"), ("wrong", "t", "short")):
        with pytest.raises(FlagError):
            await f.record(*bad)
    clock.t += 31 * 86400
    assert await f.list() == []


async def test_counters_add_up_per_day_and_old_ones_are_dropped(env):
    clock = Clock()
    f = Flags(env.orch.db, clock)
    await f.bump("tool.patch_file.err"); await f.bump("tool.patch_file.err", 2); await f.bump("tool.patch_file.ok")
    assert await f.metrics() == {"tool.patch_file.err": 3, "tool.patch_file.ok": 1}
    clock.t += 40 * 86400
    await f.purge()
    assert await f.metrics(60) == {}


async def test_the_users_switch_decides_whether_anything_is_recorded(env):
    kim = await env.signup("kim")
    assert (await kim.get("/api/account/improve")).json() == {"share": True, "can_change": True}
    await kim.post("/api/account/improve", json={"share": False})
    assert (await kim.get("/api/account/improve")).json()["share"] is False
    await env.chat(kim, "s1", "run the marathon")
    assert not [k for k in await env.orch.flags.metrics() if k.startswith(("tool.", "task."))]
    await kim.post("/api/account/improve", json={"share": True})
    await env.chat(kim, "s2", "run the marathon")
    m = await env.orch.flags.metrics()
    assert m.get("tool.write_file.ok", 0) >= 7 and m.get("task.completed") == 1


async def test_the_assistants_flag_tool_records_generic_notes_only_for_users_who_allow_it(env):
    from types import SimpleNamespace
    from llm_agent.tools.registry import build_registry, ToolContext
    spec = build_registry()["flag_knowledge"]
    args = {"kind": "missing", "topic": "playbook for fitness apps", "note": "No guide exists for gym apps, owner rita@example.com asked"}
    for share, expect_rows in ((False, 0), (True, 1)):
        rc = SimpleNamespace(share=share, flags=env.orch.flags, flags_made=0)
        ctx = ToolContext(user_id="u", cfg=None, files=None, kb=None, search=None, fetch=None, memory=None, rc=rc, agent_id="a", emit=None)
        r = await spec.handler(ctx, args)
        assert r["status"] == "success"
        rows = await env.orch.flags.list()
        assert len(rows) == expect_rows
    assert "rita@" not in rows[0]["note"]
    rc = SimpleNamespace(share=True, flags=env.orch.flags, flags_made=5)
    ctx = ToolContext(user_id="u", cfg=None, files=None, kb=None, search=None, fetch=None, memory=None, rc=rc, agent_id="a", emit=None)
    assert "Enough flags" in (await spec.handler(ctx, args))["note"]


async def test_reviewers_see_flags_and_counters_and_users_do_not(env):
    kim = await env.signup("kim", plan="free")
    await env.orch.flags.record("lesson", "patch_file", "Patching a file twice with the same text always fails.")
    assert (await kim.get("/api/review/flags")).status_code == 403
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    r = (await admin.get("/api/review/flags")).json()
    assert r["flags"][0]["topic"] == "patch_file" and "metrics" in r
    fid = r["flags"][0]["id"]
    assert (await admin.post(f"/api/review/flags/{fid}/status", json={"status": "dismissed"})).status_code == 200
    assert (await admin.get("/api/review/flags")).json()["flags"] == []
    assert (await admin.post(f"/api/review/flags/{fid}/status", json={"status": "bogus"})).status_code == 400
