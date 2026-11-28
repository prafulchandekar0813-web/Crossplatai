"""The platform-built admin dashboard: admin.json describes it, the app gives a small /admin-api, the owner edits through the signed session."""
import json

from llm_agent import admin_kit
from tests.test_appkit import b64
from tests.test_apps import make_zip
from tests.test_hosting import master  # noqa: F401  (fixture)

APP = '''import os, json
from http.server import BaseHTTPRequestHandler, HTTPServer
ROWS = {"dishes": [{"id": "1", "name": "Tea", "price": 3, "sample": True}]}
class H(BaseHTTPRequestHandler):
    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def route(self, method):
        parts = self.path.strip("/").split("/")
        if parts[0] != "admin-api":
            return self.send(200, {"public": True})
        if self.headers.get("X-Workbench-Admin") != "1":
            return self.send(403, {"error": "no"})
        rows = ROWS.get(parts[1]) if len(parts) > 1 else None
        if rows is None:
            return self.send(404, {"error": "unknown"})
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if method == "GET": return self.send(200, {"items": rows})
        if method == "POST":
            data["id"] = str(len(rows) + 10); rows.append(data); return self.send(201, data)
        item = next((r for r in rows if r["id"] == parts[2]), None)
        if item is None: return self.send(404, {"error": "missing"})
        if method == "PUT": item.update(data); return self.send(200, item)
        rows.remove(item); return self.send(200, {"ok": True})
    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_DELETE(self): self.route("DELETE")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''
ADMIN = {"title": "Tea admin", "collections": [{"name": "dishes", "label": "Dishes", "title": "name", "fields": [
    {"key": "name", "type": "text", "required": True}, {"key": "price", "type": "price"}, {"key": "photo", "type": "image"}]}]}


def test_admin_json_is_validated_and_never_raises():
    got = admin_kit.parse(json.dumps(ADMIN).encode())
    assert got["title"] == "Tea admin" and got["collections"][0]["title"] == "name" and got["collections"][0]["fields"][2]["type"] == "image"
    assert admin_kit.parse(b"not json") == {} and admin_kit.parse(b'{"paths": ["/x"]}') == {} and admin_kit.parse(None) == {}
    bad = {"collections": [{"name": "Bad Name!", "fields": [{"key": "a"}]}, {"name": "ok", "fields": [{"key": "a", "type": "script"}, {"key": "b", "type": "select", "options": ["x"]}]}]}
    got = admin_kit.parse(json.dumps(bad).encode())
    assert [c["name"] for c in got["collections"]] == ["ok"] and [f["key"] for f in got["collections"][0]["fields"]] == ["b"]


async def test_owner_manages_the_app_from_the_dashboard(env, master, monkeypatch):
    monkeypatch.setenv("HOSTING_ALLOW_ROOT_PROCESS", "1")
    env.settings.update({"hosting_runtime": "process"})
    owner, visitor = await env.signup("dash"), await env.new_client()
    project = {"app.py": APP, "run.json": json.dumps({"start": "python3 app.py"}), "admin.json": json.dumps(ADMIN)}
    app = (await owner.post("/api/apps/create", json={"name": "Tea", "version": "1.0.0", "project": b64(make_zip(project))})).json()
    aid, auth = app["app_id"], {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "deploy"})).status_code == 200
    try:
        assert (await visitor.get(f"/live/{aid}/admin-api/dishes")).status_code == 403          # never public
        opened = (await owner.post("/api/apps/manage/admin-session", headers=auth)).json()
        assert opened["dashboard"] is True and f"/admin-app/{aid}/" in opened["url"]
        base = "/" + opened["url"].split("/", 3)[3].rstrip("/")
        page = await visitor.get(base + "/")
        assert page.status_code == 200 and "Admin" in page.text and page.headers["cache-control"] == "no-store"
        assert (await visitor.get(base + "/schema")).json()["collections"][0]["name"] == "dishes"
        assert (await visitor.get(base + "/api/dishes")).json()["items"][0]["sample"] is True
        made = (await visitor.post(base + "/api/dishes", json={"name": "Cake", "price": 5})).json()
        assert made["id"] and (await visitor.put(base + f"/api/dishes/{made['id']}", json={"price": 6})).json()["price"] == 6
        assert (await visitor.delete(base + f"/api/dishes/{made['id']}")).json() == {"ok": True}
        assert (await visitor.get(base + "/api/secrets")).status_code == 404                   # only the listed collections
        assert (await visitor.delete(base + "/api/dishes")).status_code == 405
        up = await visitor.post(base + "/upload", content=b"\x89PNG fake", headers={"X-Filename": "pic.png"})
        assert up.status_code == 200
        media = await visitor.get("/" + up.json()["url"].split("/", 3)[3])
        assert media.status_code == 200 and media.headers["content-type"] == "image/png" and "sandbox" in media.headers["content-security-policy"]
        assert (await visitor.post(base + "/upload", content=b"<script>", headers={"X-Filename": "x.html"})).status_code == 415
        assert (await visitor.post(base + "/upload", content=b"", headers={"X-Filename": "x.png"})).status_code == 400
        assert (await visitor.get("/media/" + aid + "/..%2f..%2fapp.db")).status_code == 404
        forged = base.rsplit("/", 1)[0] + "/" + base.rsplit("/", 1)[1][:-3] + "abc"
        for tail in ("/", "/schema", "/api/dishes"):
            assert (await visitor.get(forged + tail)).status_code == 403
    finally:
        await env.orch.hosting.stop_all()
        await env.orch.hosting.remove(aid)


def test_audit_counts_the_admin_api_as_the_admin():
    from llm_agent import project_audit
    files = {"app.py": b"@app.get('/admin-api/dishes')\ndef x(): request.headers.get('X-Workbench-Admin')", "admin.json": json.dumps(ADMIN).encode()}
    a = project_audit._admin(files, set(), set())
    assert a["exists"] and a["kind"] == "Admin API for the dashboard" and a["trusts_manage_login"]
