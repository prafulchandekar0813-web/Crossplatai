"""App names are unique across all users; an app remembers the chat that built it."""
from tests.test_appkit import b64
from tests.test_apps import make_zip as app_zip

PROJECT = {"app.py": "print('x')\n", "index.html": "<h1>x</h1>"}


async def create(client, name, **extra):
    return await client.post("/api/apps/create", json={"name": name, "version": "1.0.0", "project": b64(app_zip(PROJECT)), **extra})


async def test_a_name_is_unique_across_users_ignoring_case_and_punctuation(env):
    a, b = await env.signup("anna"), await env.signup("ben")
    assert (await create(a, "Pizza Hut")).status_code == 200
    for other in ("Pizza Hut", "pizza hut", "PIZZA-HUT", "pizza.hut"):
        r = await create(b, other)
        assert r.status_code in (400, 409) and "already used" in r.json()["error"], other
    assert (await create(b, "Pizza Hut Pune")).status_code == 200


async def test_renaming_into_a_taken_name_is_refused_but_keeping_your_own_is_fine(env):
    a = await env.signup("anna")
    one = (await create(a, "Alpha")).json()
    await create(a, "Beta")
    auth = {"X-App-Id": one["app_id"], "X-App-Password": one["password"]}
    r = await a.post("/api/apps/manage/details", headers=auth, json={"version": "1.0.1", "name": "BETA"})
    assert r.status_code in (400, 409) and "already used" in r.json()["error"]
    assert (await a.post("/api/apps/manage/details", headers=auth, json={"version": "1.0.1", "name": "alpha"})).status_code == 200


async def test_the_chat_that_built_the_app_is_remembered(env):
    a = await env.signup("anna")
    assert (await create(a, "Gamma", session="abcd1234efgh5678")).json()["kit"]["session_id"] == "abcd1234efgh5678"
    assert "session_id" not in (await create(a, "Delta", session="../../x")).json()["kit"]
