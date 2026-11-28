"""The shared library of copied projects: kept outside user files, in category folders, managed by the administrator."""
import json
import os
import sys

import pytest

from llm_agent import repo_library as L
from llm_agent.tools.file_manager import WorkspaceFiles
from tests.test_admin_wall import ECHO  # noqa: F401
from tests.test_apps import make_zip as app_zip
from tests.test_appkit import b64
from tests.test_github import client, make_zip
from tests.test_hosting import master  # noqa: F401  (fixture)


def test_names_cannot_escape_the_library(tmp_path):
    lib = L.RepoLibrary(tmp_path / "lib")
    assert L.slug("../../Etc passwd") == "etc-passwd" and L.slug("") == "other"
    with pytest.raises(L.LibraryError):
        lib.load("shop", "../../x")
    with pytest.raises(L.LibraryError):
        lib.save("shop", "a/b", "", [("../evil.txt", b"x")], {})
    assert not (tmp_path / "evil.txt").exists()


async def test_a_clone_is_saved_once_and_the_next_user_gets_it_without_github(tmp_path):
    lib = L.RepoLibrary(tmp_path / "lib")
    gh = client()
    gh.library = lib
    one = WorkspaceFiles(str(tmp_path / "u1"))
    r = await gh.clone(one, "a/menu-ui", "", category="Restaurant")
    assert r["status"] == "success" and r["library"].startswith("restaurant/")
    tree = lib.tree()
    assert tree[0]["category"] == "restaurant" and tree[0]["repos"][0]["repo"] == "a/menu-ui"
    assert not (tmp_path / "u1/menu-ui/meta.json").exists()                     # the library's bookkeeping is not part of the user's copy

    async def no_network(*a, **k):
        raise AssertionError("GitHub must not be contacted for a saved project")
    gh._api = no_network
    two = WorkspaceFiles(str(tmp_path / "u2"))
    r2 = await gh.clone(two, "a/menu-ui", "")
    assert r2["status"] == "success" and (tmp_path / "u2/menu-ui/index.html").exists()
    (tmp_path / "u2/menu-ui/index.html").write_text("changed by user 2")      # a user's edits never reach the shared original
    assert b"<h1>Menu</h1>" in dict(lib.load(*lib.find("a/menu-ui"))[0])["index.html"]


async def test_a_blocked_project_is_never_saved(tmp_path):
    lib = L.RepoLibrary(tmp_path / "lib")
    gh = client(archive=make_zip({"x.py": "eval(base64.b64decode('aGk='))"}))
    gh.library = lib
    r = await gh.clone(WorkspaceFiles(str(tmp_path / "u")), "a/menu-ui", "", category="shop")
    assert r["status"] in ("blocked", "needs_approval"), r
    assert lib.tree() == []


async def test_only_the_administrator_manages_the_library(env):
    user = await env.signup("plain")
    assert (await user.get("/api/admin/repos")).status_code in (401, 403)
    lib = env.orch.repo_library
    lib.save("shop", "a/menu-ui", "", [("index.html", b"hi")], {"license": "MIT"})
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    got = (await admin.get("/api/admin/repos")).json()
    assert got["categories"][0]["category"] == "shop" and got["root"].endswith("repo_library")
    key = got["categories"][0]["repos"][0]["key"]
    assert (await admin.get(f"/api/admin/repos/shop/{key}")).json()["files"][0]["path"] == "index.html"
    assert (await admin.post("/api/admin/repos/category", json={"name": "Blog"})).json()["category"] == "blog"
    assert (await admin.delete(f"/api/admin/repos/shop/{key}")).status_code == 200
    assert (await admin.delete("/api/admin/repos/blog")).status_code == 200
    assert (await admin.delete("/api/admin/repos/blog")).status_code == 404
    assert (await admin.delete("/api/admin/repos/shop/..%2F..")).status_code in (404, 405)


NO_ADMIN = '''import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 404 if self.path.startswith("/admin") else 200
        self.send_response(code); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


async def test_open_admin_explains_when_the_app_has_no_admin_page(env, master, monkeypatch):
    monkeypatch.setenv("HOSTING_ALLOW_ROOT_PROCESS", "1")
    env.settings.update({"hosting_runtime": "process"})
    owner = await env.signup("noadmin")
    project = {"app.py": NO_ADMIN, "run.json": json.dumps({"start": "python3 app.py"})}
    app = (await owner.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "project": b64(app_zip(project))})).json()
    aid, auth = app["app_id"], {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "deploy"})).status_code == 200
    try:
        r = await owner.post("/api/apps/manage/admin-session", headers=auth)
        assert r.status_code == 409 and "no admin page yet" in r.json()["error"] and "Add the admin" in r.json()["error"]
    finally:
        await env.orch.hosting.stop_all()
        await env.orch.hosting.remove(aid)


async def test_the_users_files_show_only_what_they_changed(tmp_path):
    lib = L.RepoLibrary(tmp_path / "lib")
    gh = client()
    gh.library = lib
    files = WorkspaceFiles(str(tmp_path / "u"), quota_bytes=2000)
    assert (await gh.clone(files, "a/menu-ui", "menu"))["status"] == "success"
    shown = lambda: {e["path"] for e in files.list("", 200, hide_base=True)["entries"] if e["type"] == "file"}
    assert shown() == {"menu/SOURCE.md"}                                   # the repo is in the library; the user's files are only their own
    assert {"menu/index.html", "menu/SOURCE.md"} <= {e["path"] for e in files.list("", 200)["entries"] if e["type"] == "file"}      # the assistant still sees the project
    before = files.usage_bytes()
    files.patch("menu/index.html", [{"old": "<h1>Menu</h1>", "new": "<h1>My Shop</h1>"}])
    assert shown() == {"menu/SOURCE.md", "menu/index.html"}                # an edited file becomes the user's own
    assert files.usage_bytes() > before
    assert b"<h1>Menu</h1>" in dict(lib.load(*lib.find("a/menu-ui"))[0])["index.html"]     # the original is untouched


ADMIN_NO_SLASH = '''import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/admin" and self.headers.get("X-Workbench-Admin") == "1"      # like a Flask route '/admin': '/admin/' is not found
        self.send_response(200 if ok else 404); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


async def test_open_admin_finds_an_admin_that_answers_without_the_trailing_slash(env, master, monkeypatch):
    monkeypatch.setenv("HOSTING_ALLOW_ROOT_PROCESS", "1")
    env.settings.update({"hosting_runtime": "process"})
    owner = await env.signup("slashless")
    project = {"app.py": ADMIN_NO_SLASH, "run.json": json.dumps({"start": "python3 app.py"})}
    app = (await owner.post("/api/apps/create", json={"name": "Slashless", "version": "1.0.0", "project": b64(app_zip(project))})).json()
    aid, auth = app["app_id"], {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "deploy"})).status_code == 200
    try:
        r = await owner.post("/api/apps/manage/admin-session", headers=auth)
        assert r.status_code == 200 and r.json()["url"].endswith("/admin") and r.json()["path"] == "/admin"
    finally:
        await env.orch.hosting.stop_all()
        await env.orch.hosting.remove(aid)


async def test_the_new_helper_tools(tmp_path):
    from llm_agent.tools.file_manager import WorkspaceError
    files = WorkspaceFiles(str(tmp_path / "w"))
    files.write("app/menu.py", "PRICE = 9\nNAME = 'Chai'\n")
    files.write("app/notes.txt", "nothing here")
    hit = files.search("chai")["matches"]
    assert hit == [{"path": "app/menu.py", "line": 2, "text": "NAME = 'Chai'"}]
    assert files.search("price", "app/notes.txt")["matches"] == []
    with pytest.raises(WorkspaceError):
        files.search("(")
    files.delete("app/notes.txt")
    assert [e["path"] for e in files.list("")["entries"] if e["type"] == "file"] == ["app/menu.py"]
    lib = L.RepoLibrary(tmp_path / "lib")
    lib.save("restaurant", "a/menu-ui", "", [("index.html", b"x")], {"license": "MIT", "description": "Restaurant menu with online ordering"})
    lib.save("shop", "b/store", "", [("index.html", b"x")], {"license": "MIT", "description": "Simple online shop"})
    assert [r["repo"] for r in lib.search("restaurant ordering app")] == ["a/menu-ui"]
    assert lib.search("zzz") == []
