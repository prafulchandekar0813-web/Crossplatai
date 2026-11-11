"""Build app / Manage app: publishing a zip, credentials, updates, rollback, and what visitors can and cannot do."""
import io
import zipfile

from tests.conftest import ADMIN_PASSWORD


def make_zip(files, folder=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(folder + name, content)
    return buf.getvalue()


SHOP = {"index.html": "<html><head><title>Shop</title></head><body><h1>Shop v1</h1><link rel=stylesheet href=s.css></body></html>", "s.css": "h1{color:red}"}


async def build(c, name="My Shop", files=SHOP, **kw):
    return await c.post("/api/apps/build", params={"name": name}, content=make_zip(files, **kw))


async def test_build_serve_and_share_without_login(env):
    owner = await env.signup("seller")
    r = await build(owner)
    assert r.status_code == 200
    app = r.json()
    assert len(app["app_id"]) == 10 and app["password"].count("-") == 3 and app["url"] == f"/a/{app['app_id']}/"
    visitor = await env.new_client()  # a customer: no account, no cookie
    shell = await visitor.get(app["url"])
    assert shell.status_code == 200 and "<title>My Shop</title>" in shell.text and "manifest.webmanifest" in shell.text
    page = await visitor.get(app["url"] + "app/")
    assert page.status_code == 200 and "Shop v1" in page.text and "localStorage" in page.text  # storage stand-in injected
    csp = page.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert (await visitor.get(app["url"] + "app/s.css")).headers["content-type"].startswith("text/css")
    man = (await visitor.get(app["url"] + "manifest.webmanifest")).json()
    assert man["name"] == "My Shop" and man["display"] == "standalone"
    assert (await visitor.get(app["url"] + "icon.svg")).status_code == 200
    assert (await visitor.get("/a/zzzzzzzzzz/")).status_code == 404
    assert (await visitor.get(app["url"] + "app/../../../etc/passwd")).status_code in (404, 400)
    assert (await visitor.get(app["url"] + "app/products/123")).status_code == 200  # single-page-app route falls back to index


async def test_manage_requires_the_password_and_updates_the_app(env):
    owner = await env.signup("seller")
    app = (await build(owner)).json()
    other = await env.signup("someone")
    good = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await other.post("/api/apps/manage/open", headers={"X-App-Id": app["app_id"], "X-App-Password": "wrong-pass"})).status_code == 401
    assert (await other.post("/api/apps/manage/open", headers={"X-App-Id": "nosuchapp1", "X-App-Password": "x"})).status_code == 401
    assert (await env.client.post("/api/apps/manage/open", headers=good)).status_code == 401  # not signed in
    # anyone signed in who holds the id + password can manage it (the password is the key); formatting is forgiving
    ok = await other.post("/api/apps/manage/open", headers={"X-App-Id": app["app_id"].upper(), "X-App-Password": app["password"].upper().replace("-", " ")})
    assert ok.status_code == 200 and ok.json()["app"]["version"] == 1
    v2 = dict(SHOP, **{"index.html": "<html><head></head><body><h1>Shop v2 - new product</h1></body></html>"})
    up = await owner.post("/api/apps/manage/update", headers=good, content=make_zip(v2))
    assert up.status_code == 200 and up.json()["app"]["version"] == 2 and up.json()["app"]["has_previous"]
    assert "Shop v2" in (await env.client.get(app["url"] + "app/")).text
    rb = await owner.post("/api/apps/manage/rollback", headers=good)
    assert rb.status_code == 200 and "Shop v1" in (await env.client.get(app["url"] + "app/")).text
    assert (await owner.post("/api/apps/manage/delete", headers=good)).status_code == 200
    assert (await env.client.get(app["url"])).status_code == 404
    assert (await owner.get("/api/apps")).json()["apps"] == []


async def test_wrong_password_attempts_are_throttled(env):
    owner = await env.signup("seller")
    app = (await build(owner)).json()
    codes = [(await owner.post("/api/apps/manage/open", headers={"X-App-Id": app["app_id"], "X-App-Password": f"bad{i}"})).status_code for i in range(8)]
    assert codes[:6] == [401] * 6 and codes[-1] == 429
    good = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await owner.post("/api/apps/manage/open", headers=good)).status_code == 429  # even the right one waits


async def test_bad_zips_are_rejected(env):
    c = await env.signup("seller")
    assert (await c.post("/api/apps/build", params={"name": "x"}, content=b"not a zip")).status_code == 400
    r = await build(c, files={"readme.txt": "no index"})
    assert r.status_code == 400 and "index.html" in r.json()["error"]
    r = await build(c, files={"app.py": "print()", "requirements.txt": "flask", "templates/index.html": "x"})
    assert r.status_code == 400 and "server code" in r.json()["error"]
    assert (await build(c, files={"../evil.html": "x", "index.html": "y"})).status_code == 400
    assert (await c.post("/api/apps/build", params={"name": "  "}, content=make_zip(SHOP))).status_code == 400
    # secrets and junk never get published; a wrapping folder is removed
    ok = await build(c, files={"index.html": "<h1>hi</h1>", ".env": "KEY=secret", "node_modules/x.js": "x", "key.pem": "k"}, folder="shop/")
    assert ok.status_code == 200 and ok.json()["files"] == 1 and ok.json()["skipped"] == 3
    app_id = ok.json()["app_id"]
    assert (await env.client.get(f"/a/{app_id}/app/.env")).status_code == 404 and (await env.client.get(f"/a/{app_id}/app/")).status_code == 200


async def test_limits_ownership_password_reset_and_admin(env):
    owner = await env.signup("seller")
    env.orch.settings.effective.max_apps_per_user = 2
    a1, a2 = (await build(owner, "One")).json(), (await build(owner, "Two")).json()
    assert (await build(owner, "Three")).status_code == 409
    listed = (await owner.get("/api/apps")).json()["apps"]
    assert {a["name"] for a in listed} == {"One", "Two"} and "password" not in str(listed) and "password_hash" not in str(listed)
    stranger = await env.signup("stranger")
    assert (await stranger.post(f"/api/apps/{a1['app_id']}/reset-password")).status_code == 404
    new = (await owner.post(f"/api/apps/{a1['app_id']}/reset-password")).json()["password"]
    assert (await owner.post("/api/apps/manage/open", headers={"X-App-Id": a1["app_id"], "X-App-Password": a1["password"]})).status_code == 401
    assert (await owner.post("/api/apps/manage/open", headers={"X-App-Id": a1["app_id"], "X-App-Password": new})).status_code == 200
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert (await stranger.get("/api/admin/apps")).status_code == 403
    assert len((await admin.get("/api/admin/apps")).json()["apps"]) == 2
    assert (await admin.delete(f"/api/admin/apps/{a2['app_id']}")).status_code == 200
    assert (await env.client.get(a2["url"] if "url" in a2 else f"/a/{a2['app_id']}/")).status_code == 404
    await env.orch.delete_user("seller")
    assert (await env.client.get(f"/a/{a1['app_id']}/")).status_code == 404
