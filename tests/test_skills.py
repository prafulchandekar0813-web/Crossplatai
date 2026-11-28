import pytest

from llm_agent import scrub, skills as sk
from llm_agent.core.prompts import build_system_prompt
from llm_agent.config import SystemConfig


def test_builtin_skills_are_real_files_with_names_and_descriptions():
    import pathlib
    files = sorted(pathlib.Path(sk.BUILTIN_DIR).glob("*.md"))
    assert {f.stem for f in files} >= {"app-design", "github-ui", "editing-files", "playbook-delivery", "playbook-shop", "playbook-booking", "playbook-menu"}
    for f in files:
        meta, body = sk.parse(f.read_text())
        assert meta["name"] == f.stem and 20 < len(meta["description"]) <= sk.MAX_DESCRIPTION and len(body) < sk.MAX_BODY and not scrub.problems(body), f.name


def test_the_prompt_lists_skills_and_points_to_them_instead_of_carrying_them():
    text = build_system_prompt(SystemConfig(), "orchestrator", "", "u", True, [], [], skills=[("app-design", "How to design"), ("playbook-shop", "Shops")])
    assert "use_skill(name)" in text and "* app-design: How to design" in text and "* playbook-shop: Shops" in text
    assert "use_skill(\"app-design\")" in text and "1) REFERENCE" not in text and "design.json at the project root" not in text
    assert "Skills:" not in build_system_prompt(SystemConfig(), "orchestrator", "", "u", True, [], [])


async def test_edits_win_over_the_file_and_can_be_reverted(env):
    s = env.orch.skills
    assert (await s.get("editing-files"))["source"] == "built-in"
    got = await s.save("editing-files", "Edit files carefully and reliably.", "Always read first.", "kim")
    assert got["source"] == "edited" and (await s.get("editing-files"))["body"] == "Always read first."
    assert next(x for x in await s.list() if x["name"] == "editing-files")["source"] == "edited"
    assert await s.revert("editing-files") and "Read the file" in (await s.get("editing-files"))["body"]
    added = await s.save("playbook-gym", "Playbook for gym and fitness apps.", "Screens: plans, classes, progress.", "kim")
    assert added["source"] == "added" and await s.revert("playbook-gym") and await s.get("playbook-gym") is None


async def test_shared_skills_refuse_personal_data_and_bad_input(env):
    s = env.orch.skills
    for body in ("Mail rita@example.com about it", "Call +91 98765 43210 for access", "key sk-abcdefghijklmnop123", "see /home/rita/project", "app 0123456789abcdef01234567"):
        with pytest.raises(sk.SkillError) as e:
            await s.save("bad-skill", "A fine description of use.", body, "kim")
        assert "must not contain" in e.value.message
    for name, desc, body in (("Bad Name", "d" * 30, "x"), ("ok-name", "", "x"), ("ok-name", "d" * 30, ""), ("ok-name", "d" * 30, "x" * 13000)):
        with pytest.raises(sk.SkillError):
            await s.save(name, desc, body, "kim")


def test_scrub_removes_what_it_can_recognise_from_a_flag():
    t = scrub.clean("Mail rita@example.com or +91 98765 43210, key sk-abcdefghijklmnop12 at /home/rita/app, https://x.io/a?token=abc123 app 0123456789abcdef01234567")
    for leak in ("rita@", "98765", "sk-abc", "/home/rita", "token=abc123", "0123456789abcdef01234567"):
        assert leak not in t
    assert "https://x.io/a" in t and len(scrub.clean("x" * 900)) == 300


async def test_review_endpoints_need_admin_or_manager(env):
    kim = await env.signup("kim", plan="free")
    assert (await kim.get("/api/review/skills")).status_code == 403
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    names = [x["name"] for x in (await admin.get("/api/review/skills")).json()["skills"]]
    assert "app-design" in names
    await admin.post("/api/admin/users/kim/manager", json={"manager": True})
    r = await kim.put("/api/review/skills/playbook-gym", json={"description": "Playbook for gym and fitness apps.", "body": "Screens: plans, classes."})
    assert r.status_code == 200 and r.json()["source"] == "added"
    assert (await kim.put("/api/review/skills/playbook-gym", json={"description": "Playbook for gym.", "body": "mail me at a@b.co"})).status_code == 400
    assert (await kim.delete("/api/review/skills/playbook-gym")).status_code == 200
    assert (await kim.delete("/api/review/skills/playbook-gym")).status_code == 404


async def test_a_users_own_skills_are_private_and_replace_shared_ones_only_for_them(env):
    ann, bob = await env.signup("ann"), await env.signup("bob")
    r = await ann.put("/api/skills/my-voice", json={"description": "How I like app text written.", "body": "Short, warm, no jargon."})
    assert r.status_code == 200
    md = "---\nname: shop-rules\ndescription: Rules for my shop apps.\n---\nAlways show prices in rupees."
    assert (await ann.post("/api/skills/import", json={"text": md})).json()["name"] == "shop-rules"
    assert [s["name"] for s in (await ann.get("/api/skills")).json()["mine"]] == ["my-voice", "shop-rules"]
    assert (await bob.get("/api/skills")).json()["mine"] == []
    assert (await bob.get("/api/skills/my-voice")).status_code == 404
    await ann.put("/api/skills/app-design", json={"description": "My own take on design.", "body": "Always dark mode."})
    sk = env.orch.skills
    assert (await sk.get("app-design", "ann"))["body"] == "Always dark mode." and (await sk.get("app-design", "bob"))["source"] == "built-in"
    names = dict(await sk.catalog("ann"))
    assert names["app-design"].startswith("(your own skill)") and "my-voice" in names and "my-voice" not in dict(await sk.catalog("bob"))
    assert (await ann.put("/api/skills/Bad Name", json={"description": "x", "body": "y"})).status_code == 400
    assert (await bob.delete("/api/skills/my-voice")).status_code == 404 and (await ann.delete("/api/skills/my-voice")).status_code == 200
    assert "my-voice" not in {s["name"] for s in await env.orch.skills.list()}                        # reviewers never see personal skills


async def test_personal_skill_count_is_limited(env):
    ann = await env.signup("ann")
    for i in range(20):
        assert (await ann.put(f"/api/skills/skill-{i}", json={"description": "d", "body": "b"})).status_code == 200
    assert (await ann.put("/api/skills/one-too-many", json={"description": "d", "body": "b"})).status_code == 400
    assert (await ann.put("/api/skills/skill-3", json={"description": "d", "body": "changed"})).status_code == 200   # editing is still fine
