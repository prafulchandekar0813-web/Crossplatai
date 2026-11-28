"""The app's admin pages are served only to the owner's Manage session; everyone else, on every route and trick, gets a refusal."""
import json

import pytest

from llm_agent import admin_session as A
from tests.test_appkit import b64
from tests.test_apps import make_zip
from tests.test_hosting import PROJECT, master  # noqa: F401  (fixture)


def test_paths_are_compared_the_way_an_application_will_read_them():
    paths = A.clean_paths(["/dashboard/", "Orders", "../x", "/a b", 5])
    assert paths == ["/admin", "/admin-api", "/dashboard", "/orders"]
    for hidden in ("admin", "/admin", "/admin/", "/Admin/users", "//admin//x", "/admin/../admin", "/x/../admin/", "/%61dmin", "/%2561dmin", "/admin;jsessionid=1",
                   "/admin%2fusers", "\\admin", "/dashboard/stats", "/ADMIN"):
        assert A.protected(hidden, paths), hidden
    for open_ in ("", "/", "/administrator-guide", "/menu", "/static/admin.css", "/api/admins", "/dash"):
        assert not A.protected(open_, paths), open_


def test_sessions_are_signed_expire_and_belong_to_one_app():
    t = A.make_token("app1", now=1000)
    assert A.read_token(t, now=1000 + 60) == "app1" and A.read_token(t, now=1000 + A.TTL + 5) is None
    body, sig = t.split(".")
    assert A.read_token(body + "." + "0" * 32) is None and A.read_token("x") is None and A.read_token("") is None
    other = A.make_token("app2", now=1000).split(".")[0]
    assert A.read_token(other + "." + sig) is None
    assert A.cookie_ok(f"a=1; {A.COOKIE}={A.make_token('app1')}; b=2", "app1") and not A.cookie_ok(f"{A.COOKIE}={A.make_token('app1')}", "app2")
    assert A.without_cookie(f"a=1; {A.COOKIE}=zzz; b=2") == "a=1; b=2"


ECHO = '''import os, json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, "admin": self.headers.get("X-Workbench-Admin"), "cookie": self.headers.get("Cookie")}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


async def test_only_the_owners_session_reaches_admin_pages(env, master, monkeypatch):
    monkeypatch.setenv("HOSTING_ALLOW_ROOT_PROCESS", "1")
    env.settings.update({"hosting_runtime": "process"})
    owner = await env.signup("walled")
    visitor = await env.new_client()
    project = {"app.py": ECHO, "run.json": json.dumps({"start": "python3 app.py"}), "admin.json": json.dumps({"paths": ["/dashboard"]})}
    app = (await owner.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "project": b64(make_zip(project))})).json()
    aid, auth = app["app_id"], {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await owner.post("/api/apps/manage/admin-session", headers=auth)).status_code == 409          # not deployed yet
    assert (await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "deploy"})).status_code == 200
    try:
        pub = f"/live/{aid}/"
        ok = await visitor.get(pub + "menu")
        assert ok.status_code == 200 and ok.json()["admin"] is None
        for path in ("admin", "admin/", "Admin/users", "dashboard/stats", "%61dmin", "admin/../admin"):
            r = await visitor.get(pub + path)
            assert r.status_code == 403 and "Manage app" in r.json()["error"], path
        spoof = await visitor.get(pub + "menu", headers={"X-Workbench-Admin": "1", "Cookie": f"{A.COOKIE}=x; a=1"})
        assert spoof.json()["admin"] is None and A.COOKIE not in (spoof.json()["cookie"] or "")        # a visitor cannot claim to be the owner
        assert (await owner.post("/api/apps/manage/admin-session")).status_code in (401, 403)         # no app id and password, no session
        wrong = await owner.post("/api/apps/manage/admin-session", headers={**auth, "X-App-Password": "wrong"})
        assert wrong.status_code == 401
        opened = (await owner.post("/api/apps/manage/admin-session", headers=auth)).json()
        assert opened["path"] == "/admin" and f"/admin-live/{aid}/" in opened["url"] and opened["cookies"] is False
        url = "/" + opened["url"].split("/", 3)[3]
        mine = await visitor.get(url)                                    # the signed link is the session
        assert mine.status_code == 200 and mine.json()["admin"] == "1" and mine.json()["path"] == "/admin/"
        token = url.split("/")[3]
        assert (await visitor.get(f"/admin-live/{aid}/{token}/dashboard/x")).json()["admin"] == "1"
        assert (await visitor.get(f"/admin-live/{aid}/{token[:-3]}abc/admin/")).status_code == 403
        assert (await visitor.get(f"/admin-live/other/{token}/admin/")).status_code == 403
    finally:
        await env.orch.hosting.stop_all()
        await env.orch.hosting.remove(aid)
