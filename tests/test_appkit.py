"""Managed apps: validation, the backend bundle, the catalogue the installed apps read, Manage app."""
import base64
import io
import json
import zipfile

import pytest

from llm_agent import appkit
from tests.test_apps import make_zip

PROJECT = {"app.py": "import os\nfrom flask import Flask\napp = Flask(__name__)\napp.run(port=int(os.environ['PORT']))\n", "requirements.txt": "flask\n",
           "templates/index.html": "<h1>Shop</h1>", ".env.example": "API_BASE_URL=x\n", "notes.pem": "secret"}


def b64(b):
    return base64.b64encode(b).decode()


async def make_kit(c, **kw):
    """Create a managed app (the name is kept from the days of build kits; it is used by the other test files)."""
    body = {"name": "Green Shop", "version": "1.0.0", "project": b64(make_zip(PROJECT))}
    body.update(kw)
    return await c.post("/api/apps/create", json=body)


def test_validation():
    assert appkit.check_name(" Green  Shop ") == "Green Shop"
    for bad in ("", "a", "x" * 31, "Bad/Name", "../x"):
        with pytest.raises(appkit.KitError):
            appkit.check_name(bad)
    assert appkit.check_version("1.2.3") == "1.2.3"
    for bad in ("1", "v1.0", "1.2.3.4", "a.b"):
        with pytest.raises(appkit.KitError):
            appkit.check_version(bad)
    logo = appkit.default_logo("abc")
    assert appkit.png_size(logo) == (512, 512) and appkit.check_logo(logo) == logo
    with pytest.raises(appkit.KitError):
        appkit.check_logo(b"not a png")


def test_read_project_rules_and_backend_bundle():
    p = appkit.read_project(make_zip(PROJECT, folder="shop/"), 10 ** 7)
    assert "app.py" in p["files"] and "notes.pem" not in p["files"] and ".env.example" in p["files"] and p["skipped"] == 1 and p["builder"] == "flask"
    for bad in (PROJECT | {"../evil.py": "x"},):
        with pytest.raises(appkit.KitError):
            appkit.read_project(make_zip(bad), 10 ** 7)
    with pytest.raises(appkit.KitError):
        appkit.read_project(b"not a zip", 10 ** 7)
    z = zipfile.ZipFile(io.BytesIO(appkit.backend_bundle("Green Shop", "abcdefghjk", "https://x.test/", p["files"])))
    names = {n.split("/", 1)[1] for n in z.namelist()}
    assert {"app.py", "workbench_client.py", "app_config.json", "DEPLOY.txt", ".env.example"} <= names
    cfg = json.loads(z.read(next(n for n in z.namelist() if n.endswith("app_config.json"))))
    assert cfg == {"app_id": "abcdefghjk", "name": "Green Shop", "api_base": "https://x.test", "catalog_url": "https://x.test/api/catalog/abcdefghjk/products"}
    assert "password" not in json.dumps(cfg).lower()


async def test_managed_app_flow_and_catalog(env):
    owner = await env.signup("seller")
    r = await make_kit(owner)
    assert r.status_code == 200, r.text
    app = r.json()
    assert len(app["app_id"]) == 10 and app["password"].count("-") == 3 and len(app["warnings"]) == 1  # the .pem was left out
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    visitor = await env.new_client()
    assert (await visitor.get(f"/a/{app['app_id']}/")).status_code == 404  # a managed app has no hosted web page
    # the owner can download the backend, ready to host
    dl = await owner.post("/api/apps/manage/backend", headers=auth)
    assert dl.status_code == 200 and "DEPLOY.txt" in " ".join(zipfile.ZipFile(io.BytesIO(dl.content)).namelist())
    assert (await owner.post("/api/apps/manage/backend", headers={"X-App-Id": app["app_id"], "X-App-Password": "wrong"})).status_code == 401
    # products
    img = (await owner.post("/api/apps/manage/image", headers=auth, content=appkit.default_logo("p"))).json()["image"]
    assert (await owner.post("/api/apps/manage/image", headers=auth, content=b"<svg onload=1>")).status_code == 400
    saved = await owner.post("/api/apps/manage/items/save", headers=auth, json={"title": "Tea", "price": "4.5", "image": img, "description": "Green"})
    assert saved.status_code == 200
    assert (await owner.post("/api/apps/manage/items/save", headers=auth, json={"title": "", "price": 1})).status_code == 400
    assert (await owner.post("/api/apps/manage/items/save", headers=auth, json={"title": "X", "price": "abc"})).status_code == 400
    assert (await owner.post("/api/apps/manage/items/save", headers=auth, json={"title": "X", "image": "javascript:1"})).status_code == 400
    catalog = f"/api/catalog/{app['app_id']}/products"
    dev = {"X-Device-Id": "a" * 24, "X-App-Version": "1.0.0", "X-Platform": "apk"}
    clock = [1_000_000.0]
    env.orch.apps.clock = lambda: clock[0]
    # drafts are invisible to installed apps until the owner presses Update
    cat = await visitor.get(catalog, headers=dev)
    assert cat.status_code == 200 and cat.headers["access-control-allow-origin"] == "*"
    assert cat.json()["items"] == [] and cat.json()["update"]["pending"] is False
    ov = (await owner.post("/api/apps/manage/overview", headers=auth)).json()
    assert ov["publish"]["has_changes"] and ov["usage"]["installs"] == 1 and ov["usage"]["platforms"][0]["platform"] == "apk"
    assert any(f["path"] == "app.py" for f in ov["files"])
    # Update: the old version keeps being served while the cool-down runs, with a countdown
    pub = (await owner.post("/api/apps/manage/publish", headers=auth)).json()
    assert pub["revision"] == 1 and pub["seconds_left"] == 60
    assert (await owner.post("/api/apps/manage/publish", headers=auth)).status_code == 409  # nothing new to push
    clock[0] += 20
    mid = (await visitor.get(catalog, headers=dev)).json()
    assert mid["items"] == [] and mid["update"] == {"pending": True, "seconds_left": 40, "next_revision": 1}
    clock[0] += 41
    live = (await visitor.get(catalog, headers=dev)).json()
    item = live["items"][0]
    assert live["revision"] == 1 and not live["update"]["pending"]
    assert item["title"] == "Tea" and item["price"] == 4.5 and item["image_url"].startswith("http") and item["in_stock"]
    assert (await visitor.get(item["image_url"])).status_code == 200
    # a second device and a later open
    await visitor.get(catalog, headers={**dev, "X-Device-Id": "b" * 24, "X-Platform": "exe"})
    await visitor.get(catalog, headers={**dev, "X-Device-Id": "not-valid"})  # ignored
    usage = (await owner.post("/api/apps/manage/overview", headers=auth)).json()["usage"]
    assert usage["installs"] == 2 and usage["opens"] == 4 and usage["active_24h"] == 2
    # edit: the app keeps showing the old price until Update is pressed and the cool-down is over
    row = (await owner.post("/api/apps/manage/items/list", headers=auth)).json()["items"][0]
    await owner.post("/api/apps/manage/items/save", headers=auth, json={"id": row["id"], "title": "Tea", "price": 5, "in_stock": False, "collection": "products"})
    assert (await visitor.get(catalog, headers=dev)).json()["items"][0]["price"] == 4.5
    await owner.post("/api/apps/manage/publish", headers=auth)
    clock[0] += 30
    still = (await visitor.get(catalog, headers=dev)).json()
    assert still["items"][0]["price"] == 4.5 and still["update"]["pending"]  # still the old version, with the countdown
    clock[0] += 31
    again = (await visitor.get(catalog, headers=dev)).json()
    assert again["items"][0]["price"] == 5 and again["items"][0]["in_stock"] is False and again["revision"] == 2
    # appearance settings travel with the same Update
    assert (await owner.post("/api/apps/manage/settings", headers=auth, json={"accent": "red"})).status_code == 400
    ok = await owner.post("/api/apps/manage/settings", headers=auth, json={"title": "Green Shop", "accent": "#16a34a", "announcement": "Sale today"})
    assert ok.status_code == 200
    assert (await visitor.get(catalog, headers=dev)).json()["settings"]["announcement"] == ""
    await owner.post("/api/apps/manage/publish", headers=auth)
    clock[0] += 61
    assert (await visitor.get(catalog, headers=dev)).json()["settings"] == {"title": "Green Shop", "accent": "#16a34a", "announcement": "Sale today", "backend_url": ""}
    await owner.post("/api/apps/manage/items/delete", headers=auth, params={"item_id": row["id"]})
    assert (await owner.post("/api/apps/manage/overview", headers=auth)).json()["publish"]["has_changes"]
    # a new version must be higher
    assert (await owner.post("/api/apps/manage/details", headers=auth, json={"version": "1.0.0"})).status_code == 400
    up = await owner.post("/api/apps/manage/details", headers=auth, json={"version": "1.1.0", "name": "Green Shop 2"})
    assert up.status_code == 200 and up.json()["app"]["name"] == "Green Shop 2"
    # hosted-web operations are refused for managed apps
    assert (await owner.post("/api/apps/manage/update", headers=auth, content=make_zip(PROJECT))).status_code == 409
    assert (await owner.post("/api/apps/manage/delete", headers=auth)).status_code == 200
    assert (await visitor.get(f"/api/catalog/{app['app_id']}/products")).status_code == 404


async def test_create_rejects_bad_input_and_works_without_a_project(env):
    owner = await env.signup("seller")
    assert (await make_kit(owner, name="x")).status_code == 400
    assert (await make_kit(owner, version="one")).status_code == 400
    assert (await make_kit(owner, logo=b64(b"junk"))).status_code == 400
    assert (await make_kit(owner, project="!!!")).status_code == 400
    anon = await env.new_client()
    assert (await make_kit(anon)).status_code == 401
    bare = await make_kit(owner, project="", name="Hosted Already")                  # an existing site: name + address only
    assert bare.status_code == 200 and bare.json()["files"] == 0
    auth = {"X-App-Id": bare.json()["app_id"], "X-App-Password": bare.json()["password"]}
    assert (await owner.post("/api/apps/manage/backend", headers=auth)).status_code == 404


def test_kit_client_messages_and_offline_cache(tmp_path, monkeypatch):
    from llm_agent import kit_client
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps({"app_id": "abcdefghjk", "api_base": "http://x.invalid"}))
    monkeypatch.setenv("ANDROID_PRIVATE", str(tmp_path))
    c = kit_client.Client(str(cfg), version="1.0.0")
    assert len(c.device_id) == 24 and kit_client.Client(str(cfg)).device_id == c.device_id  # stable across restarts
    r = c._result({"items": [{"title": "Tea"}], "revision": 3, "update": {"pending": True, "seconds_left": 45}})
    assert "Updating app" in r.message and "1 minute" in r.message and r.retry_in == 47 and r.items[0]["title"] == "Tea"
    assert "10 seconds" in c._result({"update": {"pending": True, "seconds_left": 10}}).message
    assert c._result({"items": [], "update": {"pending": False}}).message == ""
    off = c.sync("products", timeout=1)  # unreachable, nothing cached yet
    assert off.offline and off.retry_in == 30
    (tmp_path / "workbench-app" / "abcdefghjk-products.json").write_text(json.dumps({"items": [{"title": "Cached"}]}))
    off = c.sync("products", timeout=1)
    assert off.offline and off.items[0]["title"] == "Cached" and "offline" in off.message


async def test_inbox_orders_from_customers_reach_the_owner(env):
    owner = await env.signup("seller")
    app = (await make_kit(owner)).json()
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    visitor = await env.new_client()
    url = f"/api/catalog/{app['app_id']}/submit/orders"
    dev = {"X-Device-Id": "d" * 24}
    ok = await visitor.post(url, json={"name": "Ana", "item": "Tea", "qty": 2, "nested": {"x": 1}}, headers=dev)
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "*"
    assert (await visitor.post(url, json={"name": "x"})).status_code == 400                       # no device id
    assert (await visitor.post(url, json=[1], headers=dev)).status_code == 400
    assert (await visitor.post(url, content=b"x" * 20000, headers=dev)).status_code == 413
    assert (await visitor.post(f"/api/catalog/{app['app_id']}/submit/BAD NAME", json={"a": 1}, headers=dev)).status_code == 400
    assert (await visitor.options(url)).status_code == 204
    inbox = (await owner.post("/api/apps/manage/inbox/list", headers=auth)).json()["items"]
    assert len(inbox) == 1 and inbox[0]["data"] == {"name": "Ana", "item": "Tea", "qty": 2} and inbox[0]["status"] == "new"   # the nested object was dropped
    assert (await owner.post("/api/apps/manage/overview", headers=auth)).json()["inbox"] == {"total": 1, "new": 1}
    await owner.post("/api/apps/manage/inbox/status", headers=auth, params={"item_id": inbox[0]["id"], "status": "done"})
    assert (await owner.post("/api/apps/manage/overview", headers=auth)).json()["inbox"] == {"total": 1, "new": 0}
    assert (await owner.post("/api/apps/manage/inbox/status", headers=auth, params={"item_id": inbox[0]["id"], "status": "evil"})).status_code == 400
    for _ in range(30):                                                                              # per-device hourly limit
        r = await visitor.post(url, json={"a": 1}, headers=dev)
    assert r.status_code == 429
    other = await env.signup("other")                                                                # someone else cannot read it
    assert (await other.post("/api/apps/manage/inbox/list", headers={"X-App-Id": app["app_id"], "X-App-Password": "wrong"})).status_code == 401
    await owner.post("/api/apps/manage/inbox/delete", headers=auth, params={"item_id": inbox[0]["id"]})
    assert (await owner.post("/api/apps/manage/inbox/delete", headers=auth, params={"item_id": inbox[0]["id"]})).status_code == 404
