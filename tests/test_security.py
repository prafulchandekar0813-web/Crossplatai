"""Regression tests for the vulnerabilities found in the first review, plus auth / isolation."""
import os

import pytest

from llm_agent.tools.file_manager import WorkspaceError, WorkspaceFiles
from llm_agent.tools.rag_search import KnowledgeBase, KnowledgeError
from tests.conftest import ADMIN_PASSWORD


# ---- workspace sandbox -------------------------------------------------------------------------------
def make_ws(tmp_path, **kw):
    return WorkspaceFiles(str(tmp_path / "ws"), **kw)


def test_sibling_directory_prefix_escape_is_blocked(tmp_path):
    (tmp_path / "ws_evil").mkdir()
    ws = make_ws(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.write("../ws_evil/x.txt", "pwn")
    assert not (tmp_path / "ws_evil" / "x.txt").exists()


def test_filename_traversal_is_blocked(tmp_path):
    ws = make_ws(tmp_path)
    for bad in ("../../outside.txt", "a/../../outside.txt", "..\\..\\outside.txt"):
        with pytest.raises(WorkspaceError):
            ws.write(bad, "pwn")
    assert not list(tmp_path.glob("outside.txt"))


def test_absolute_path_is_treated_as_workspace_relative(tmp_path):
    ws = make_ws(tmp_path)
    res = ws.write("/etc/evil.txt", "x")
    assert res["path"] == "etc/evil.txt" and (tmp_path / "ws" / "etc" / "evil.txt").exists()
    assert not os.path.exists("/etc/evil.txt")


def test_symlink_escape_is_blocked(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    ws = make_ws(tmp_path)
    os.symlink(outside, tmp_path / "ws" / "link.txt")
    os.symlink(tmp_path, tmp_path / "ws" / "linkdir")
    with pytest.raises(WorkspaceError):
        ws.read("link.txt")
    with pytest.raises(WorkspaceError):
        ws.write("linkdir/new.txt", "x")
    assert all(e["path"] not in ("link.txt", "linkdir") for e in ws.list("")["entries"])


def test_size_and_quota_limits(tmp_path):
    ws = make_ws(tmp_path, max_file_bytes=100, quota_bytes=150)
    with pytest.raises(WorkspaceError, match="limit"):
        ws.write("big.txt", "x" * 101)
    ws.write("a.txt", "x" * 100)
    with pytest.raises(WorkspaceError, match="quota"):
        ws.write("b.txt", "x" * 80)
    ws.write("a.txt", "y" * 90)  # overwriting counts the replaced bytes only once


def test_read_in_parts_and_binary(tmp_path):
    ws = make_ws(tmp_path)
    ws.write("t.txt", "abcdefghij")
    first = ws.read("t.txt", 0, 4)
    assert first["content"] == "abcd" and first["next_offset"] == 4
    assert ws.read("t.txt", 4, 100)["next_offset"] is None
    (tmp_path / "ws" / "b.bin").write_bytes(b"\x00\x01\x02")
    assert ws.read("b.bin")["binary"] is True


# ---- knowledge base ----------------------------------------------------------------------------------
async def test_kb_upload_filename_cannot_escape(tmp_path):
    kb = KnowledgeBase(str(tmp_path / "kb"))
    res = await kb.add_document("../../evil.md", b"# Title\nhello world")
    assert res["filename"] == "evil.md"
    assert not (tmp_path / "evil.md").exists() and (tmp_path / "kb" / "sources" / "evil.md").exists()
    with pytest.raises(KnowledgeError):
        await kb.add_document("run.exe", b"MZ")
    assert kb.delete_document("../../kb/sources/evil.md") is True  # sanitised to the basename, stays inside
    assert kb.delete_document("../../../etc/passwd") is False


# ---- authentication & isolation ----------------------------------------------------------------------
async def test_endpoints_require_login(env):
    for method, url in (("GET", "/api/sessions"), ("GET", "/api/workspace"), ("GET", "/api/memory"),
                        ("GET", "/api/admin/config"), ("GET", "/api/knowledge"), ("POST", "/api/admin/prune")):
        r = await env.client.request(method, url)
        assert r.status_code == 401, (method, url)


async def test_user_cannot_use_admin_endpoints_and_config_hides_secrets(env):
    user = await env.signup("alice")
    assert (await user.get("/api/admin/config")).status_code == 403
    assert (await user.put("/api/admin/config", json={})).status_code == 403
    admin = await env.new_client()
    r = await admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    await admin.put("/api/admin/config", json={"openai": {"api_key": "sk-secret-123"}})
    resp = await admin.get("/api/admin/config")
    assert "sk-secret-123" not in resp.text and "password_hash" not in resp.text
    assert resp.json()["config"]["openai"]["api_key_set"] is True and "api_key" not in resp.json()["config"]["openai"]
    assert "sk-secret-123" not in (await env.client.get("/api/bootstrap")).text


async def test_users_are_isolated(env):
    alice, bob = await env.signup("alice"), await env.signup("bob")
    events = await env.chat(alice, "sess1", "build me a todo app")
    run_id = events[0]["run_id"]
    # bob cannot see alice's run, chat, files or memory
    assert (await bob.get(f"/api/runs/{run_id}")).status_code == 404
    assert (await bob.get(f"/api/runs/{run_id}/events")).status_code == 404
    assert (await bob.post(f"/api/runs/{run_id}/cancel")).status_code == 404
    assert (await bob.get("/api/sessions/sess1/messages")).json()["messages"] == []
    assert (await bob.get("/api/workspace")).json()["entries"] == []
    assert any(e["path"] == "todo" for e in (await alice.get("/api/workspace")).json()["entries"])
    assert (await bob.get("/api/workspace/file", params={"path": "todo/backend/app.py"})).status_code == 404
    assert (await bob.get("/api/workspace/download", params={"path": "todo/backend/app.py"})).status_code == 404


async def test_workspace_api_blocks_traversal(env):
    alice = await env.signup("alice")
    for path in ("../bob/x", "../../etc/passwd", "a/../../b"):
        assert (await alice.get("/api/workspace/file", params={"path": path})).status_code in (400, 404)
        assert (await alice.delete("/api/workspace/file", params={"path": path})).status_code == 400
        assert (await alice.get("/api/workspace/download", params={"path": path})).status_code == 400


async def test_cross_site_request_is_blocked(env):
    alice = await env.signup("alice")
    r = await alice.post("/api/sessions/s1/chat", json={"prompt": "hi"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await alice.post("/api/auth/logout", headers={"Origin": "http://test"})
    assert r.status_code == 200


async def test_login_throttle_and_cookie_flags(env):
    await env.signup("alice")
    c = await env.new_client()
    for _ in range(5):
        assert (await c.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"})).status_code == 401
    assert (await c.post("/api/auth/login", json={"username": "alice", "password": "password-123"})).status_code == 429
    c2 = await env.new_client()
    r = await c2.post("/api/auth/login", json={"username": "alice", "password": "password-123"})
    assert r.status_code in (200, 429)
    cookie = r.headers.get("set-cookie", "") if r.status_code == 200 else ""
    assert not cookie or ("httponly" in cookie.lower() and "samesite=strict" in cookie.lower())


async def test_registration_rules(env):
    c = await env.new_client()
    for bad in ("ab", "has space", "../x", "x" * 40):
        assert (await c.post("/api/auth/register", json={"username": bad, "password": "password-123"})).status_code == 400
    assert (await c.post("/api/auth/register", json={"username": "okname", "password": "short"})).status_code == 400
    assert (await c.post("/api/auth/register", json={"username": "admin", "password": "password-123"})).status_code == 409
    await env.signup("Carol")
    assert (await (await env.new_client()).post("/api/auth/register", json={"username": "carol", "password": "password-123"})).status_code == 409


async def test_admin_default_password_is_not_admin(env):
    c = await env.new_client()
    assert (await c.post("/api/auth/login", json={"username": "admin", "password": "admin"})).status_code == 401


async def test_security_headers(env):
    r = await env.client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'self'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers


# ---- app preview -------------------------------------------------------------------------------------
async def test_preview_serves_sandboxed_static_files_only_to_the_owner(env):
    alice = await env.signup("alice")
    ws = env.orch.workspace_for("alice")
    ws.write("app/index.html", "<html><head></head><body><link rel=stylesheet href=style.css>hi</body></html>")
    ws.write("app/style.css", "body{color:red}")
    ws.write("app/server.py", "print('x')")
    r = await alice.post("/api/preview", json={"path": "app"})  # a folder means its index.html
    assert r.status_code == 200 and r.json()["path"] == "app/index.html"
    url = r.json()["url"]
    page = await env.client.get(url)  # no cookie / no auth: the signed link is the credential
    assert page.status_code == 200 and "localStorage" in page.text and "hi" in page.text
    assert page.headers["content-security-policy"].startswith("sandbox allow-scripts") and "allow-same-origin" not in page.headers["content-security-policy"]
    css = await env.client.get(url.rsplit("/", 1)[0] + "/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    # traversal, forged and cross-user links all fail
    base = url.rsplit("/app/", 1)[0]
    assert (await env.client.get(base + "/../../etc/passwd")).status_code in (400, 404)
    assert (await env.client.get(base + "/app/%2e%2e/%2e%2e/x")).status_code in (400, 404)
    assert (await env.client.get("/preview/forged.token/app/index.html")).status_code == 403
    bob = await env.signup("bob")
    assert (await bob.post("/api/preview", json={"path": "app/index.html"})).status_code == 404
    bob_link = (await bob.post("/api/preview", json={"path": "."})).status_code
    assert bob_link == 404  # no index.html in bob's workspace
    from llm_agent import preview
    assert preview.read_token(preview.make_token("alice", now=0)) is None  # expired long ago


async def test_folder_zip_download_is_scoped_and_ignores_symlinks(env):
    import io
    import zipfile
    alice, bob = await env.signup("alice"), await env.signup("bob")
    ws = env.orch.workspace_for("alice")
    ws.write("app/main.py", "print(1)")
    ws.write("app/static/a.css", "x{}")
    ws.write("other/keep.txt", "no")
    secret = ws.root.parent / "secret.txt"
    secret.write_text("top secret")
    os.symlink(secret, ws.root / "app" / "leak.txt")
    r = await alice.get("/api/workspace/zip", params={"path": "app"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "app.zip" in r.headers["content-disposition"]
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == {"main.py", "static/a.css"}
    everything = set(zipfile.ZipFile(io.BytesIO((await alice.get("/api/workspace/zip")).content)).namelist())
    assert {"app/main.py", "other/keep.txt"} <= everything and "app/leak.txt" not in everything
    assert (await bob.get("/api/workspace/zip", params={"path": "app"})).status_code == 404
    assert (await alice.get("/api/workspace/zip", params={"path": "../x"})).status_code == 400


async def test_patch_file_is_atomic_and_exact(tmp_path):
    ws = WorkspaceFiles(str(tmp_path / "ws"))
    ws.write("a.py", "x = 1\ny = 2\nx = 1\n")
    with pytest.raises(WorkspaceError, match="3 times|2 times"):
        ws.patch("a.py", [{"old": "x = 1", "new": "x = 9"}])
    with pytest.raises(WorkspaceError, match="not found"):
        ws.patch("a.py", [{"old": "y = 2", "new": "y = 3"}, {"old": "nope", "new": "z"}])
    assert ws.read("a.py")["content"] == "x = 1\ny = 2\nx = 1\n"  # the failed batch changed nothing
    r = ws.patch("a.py", [{"old": "x = 1", "new": "x = 9", "replace_all": True}, {"old": "y = 2", "new": "y = 3"}])
    assert r["replacements"] == 3 and ws.read("a.py")["content"] == "x = 9\ny = 3\nx = 9\n"
    with pytest.raises(WorkspaceError):
        ws.patch("missing.py", [{"old": "a", "new": "b"}])
    with pytest.raises(WorkspaceError):
        ws.patch("../outside.py", [{"old": "a", "new": "b"}])


async def test_patch_tool_and_folder_delete_via_api(env):
    c = await env.signup("alice")
    ws = env.orch.workspace_for("alice")
    ws.write("proj/sub/a.txt", "hello")
    ws.write("keep/b.txt", "stay")
    assert (await c.delete("/api/workspace/file", params={"path": "proj"})).status_code == 200
    paths = {e["path"] for e in (await c.get("/api/workspace")).json()["entries"]}
    assert not any(p.startswith("proj") for p in paths) and "keep/b.txt" in paths
    from llm_agent.tools.registry import build_registry, validate_args
    spec = build_registry()["patch_file"]
    assert validate_args(spec, {"path": "a", "edits": [{"old": "a", "new": "b"}]}) is None
    assert validate_args(spec, {"path": "a"}) is not None


async def test_edit_lines_replace_insert_delete_and_validation(tmp_path):
    ws = WorkspaceFiles(str(tmp_path / "ws"))
    ws.write("f.txt", "one\ntwo\nthree\nfour\n")
    numbered = ws.read("f.txt", numbered=True)
    assert numbered["content"].startswith("    1| one") and numbered["total_lines"] == 4
    ws.edit_lines("f.txt", [{"start_line": 2, "end_line": 2, "new_text": "TWO"},      # replace
                            {"start_line": 4, "end_line": 4, "new_text": ""},         # delete
                            {"start_line": 1, "end_line": 0, "new_text": "zero"}])    # insert before line 1
    assert ws.read("f.txt")["content"] == "zero\none\nTWO\nthree\n"
    ws.edit_lines("f.txt", [{"start_line": 5, "end_line": 4, "new_text": "end"}])    # append
    assert ws.read("f.txt")["content"] == "zero\none\nTWO\nthree\nend\n"
    ws.edit_lines("f.txt", [{"start_line": 1, "end_line": 2, "new_text": "a\nb\nc"}])  # one line becomes three
    assert ws.read("f.txt")["content"] == "a\nb\nc\nTWO\nthree\nend\n"
    before = ws.read("f.txt")["content"]
    for bad in ([{"start_line": 99, "end_line": 99, "new_text": "x"}],
                [{"start_line": 1, "end_line": 3, "new_text": "x"}, {"start_line": 2, "end_line": 2, "new_text": "y"}],
                [{"start_line": "1", "end_line": 1, "new_text": "x"}], []):
        with pytest.raises(WorkspaceError):
            ws.edit_lines("f.txt", bad)
    assert ws.read("f.txt")["content"] == before
    ws.write("nonl.txt", "a\nb")  # no trailing newline stays that way
    ws.edit_lines("nonl.txt", [{"start_line": 2, "end_line": 2, "new_text": "B"}])
    assert ws.read("nonl.txt")["content"] == "a\nB"
    ws.write("win.txt", "a\r\nb\r\n")
    ws.edit_lines("win.txt", [{"start_line": 1, "end_line": 1, "new_text": "x\ny"}])
    assert ws.read("win.txt")["content"] == "x\r\ny\r\nb\r\n"


async def test_insert_text_positions_and_paragraphs(tmp_path):
    ws = WorkspaceFiles(str(tmp_path / "ws"))
    ws.write("d.md", "# Title\n\nIntro text.\n\n## Usage\nRun it.\n")
    ws.insert("d.md", "Added after the intro.", "after_text", anchor="Intro text.", paragraph=True)
    assert ws.read("d.md")["content"] == "# Title\n\nIntro text.\n\nAdded after the intro.\n\n## Usage\nRun it.\n"
    ws.insert("d.md", "LAST", "end")
    ws.insert("d.md", "FIRST", "start")
    ws.insert("d.md", "before usage", "before_text", anchor="## Usage")
    out = ws.read("d.md")["content"].splitlines()
    assert out[0] == "FIRST" and out[-1] == "LAST" and out[out.index("## Usage") - 1] == "before usage"
    ws.write("c.py", "a = 1\nb = 2\n")
    r = ws.insert("c.py", "x = 0\ny = 0", "after_line", line=1)
    assert ws.read("c.py")["content"] == "a = 1\nx = 0\ny = 0\nb = 2\n" and r["inserted_at_line"] == 2
    ws.write("empty.txt", "")
    ws.insert("empty.txt", "hello", "end")
    assert ws.read("empty.txt")["content"] == "hello\n"
    for bad in (dict(where="after_text", anchor="zzz"), dict(where="after_line", line=99), dict(where="middle"), dict(where="after_text")):
        with pytest.raises(WorkspaceError):
            ws.insert("c.py", "t", **bad)
    ws.write("dup.txt", "same\nsame\n")
    with pytest.raises(WorkspaceError, match="2 lines"):
        ws.insert("dup.txt", "t", "after_text", anchor="same")
