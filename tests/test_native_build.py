"""Branded native builds through GitHub Actions (GitHub is mocked) and the app address / logo the shell reads."""
import io
import json
import zipfile

import httpx

from tests.test_appkit import make_kit


def gh_zip(name, data=b"BINARY"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, data)
    return b.getvalue()


class FakeGitHub:
    def __init__(self):
        self.dispatched, self.run_status, self.conclusion, self.seen_tokens = [], "queued", None, set()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle), follow_redirects=True)

    def handle(self, req: httpx.Request):
        self.seen_tokens.add(req.headers.get("authorization"))
        path = req.url.path
        if path.endswith("/dispatches"):
            self.dispatched.append(json.loads(req.content))
            return httpx.Response(204)
        if path.endswith("/actions/runs"):
            if not self.dispatched:
                return httpx.Response(200, json={"workflow_runs": []})
            rid = self.dispatched[-1]["inputs"]["request_id"]
            return httpx.Response(200, json={"workflow_runs": [{"id": 77, "display_title": f"build x {rid}", "status": self.run_status,
                                                                "conclusion": self.conclusion, "html_url": "https://github.com/o/r/actions/runs/77"}]})
        if path.endswith("/runs/77/artifacts"):
            return httpx.Response(200, json={"artifacts": [
                {"name": "apk", "size_in_bytes": 10, "archive_download_url": "https://api.github.com/dl/apk"},
                {"name": "exe", "size_in_bytes": 10, "archive_download_url": "https://api.github.com/dl/exe"}]})
        if path == "/dl/apk":
            return httpx.Response(200, content=gh_zip("app-release.apk", b"APKDATA"))
        if path == "/dl/exe":
            return httpx.Response(200, content=gh_zip("app-windows.zip", b"EXEDATA"))
        return httpx.Response(404)


async def setup(env, monkeypatch, public="https://workbench.example.com"):
    monkeypatch.setenv("BUILD_GITHUB_TOKEN", "ghp_secret")
    env.settings.update({"build_github_repo": "o/r", "public_url": public})
    gh = FakeGitHub()
    env.orch.builds.client = gh.client
    owner = await env.signup("seller")
    app = (await make_kit(owner)).json()
    return owner, app, {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}, gh


async def test_build_runs_to_completion_and_files_download(env, monkeypatch):
    owner, app, auth, gh = await setup(env, monkeypatch)
    r = await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["apk", "exe"]})
    assert r.status_code == 200, r.text
    inputs = gh.dispatched[0]["inputs"]
    assert inputs["app_id"] == app["app_id"] and inputs["api_base"] == "https://workbench.example.com"
    assert inputs["package_id"] == f"com.appworkbench.a{app['app_id']}" and inputs["platforms"] == "apk,exe" and inputs["app_name"] == "Green Shop"
    assert gh.seen_tokens == {"Bearer ghp_secret"}
    # still running
    gh.run_status = "in_progress"
    b = (await owner.post("/api/apps/manage/shell-builds", headers=auth)).json()["builds"][0]
    assert b["status"] == "building" and b["run_url"].startswith("https://github.com/")
    # a second request while one runs is refused
    assert (await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["apk"]})).status_code == 409
    gh.run_status, gh.conclusion = "completed", "success"
    b = (await owner.post("/api/apps/manage/shell-builds", headers=auth)).json()["builds"][0]
    assert b["status"] == "done" and sorted(b["files"]) == ["apk", "exe"]
    apk = await owner.post(f"/api/apps/manage/shell-build/file?build_id={b['id']}&kind=apk", headers=auth)
    assert apk.content == b"APKDATA" and ".apk" in apk.headers["content-disposition"]
    exe = await owner.post(f"/api/apps/manage/shell-build/file?build_id={b['id']}&kind=exe", headers=auth)
    assert exe.content == b"EXEDATA"
    # without the app's password nobody can fetch it
    other = await env.signup("other")
    wrong = {"X-App-Id": app["app_id"], "X-App-Password": "wrong-pass-word-here"}
    assert (await other.post(f"/api/apps/manage/shell-build/file?build_id={b['id']}&kind=apk", headers=wrong)).status_code in (401, 403)
    assert (await owner.post(f"/api/apps/manage/shell-build/file?build_id={b['id']}&kind=../x", headers=auth)).status_code in (404, 422)


async def test_failed_build_is_reported_and_daily_limit_applies(env, monkeypatch):
    owner, app, auth, gh = await setup(env, monkeypatch)
    env.settings.update({"builds_per_app_per_day": 2})
    await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["apk"]})
    gh.run_status, gh.conclusion = "completed", "failure"
    b = (await owner.post("/api/apps/manage/shell-builds", headers=auth)).json()["builds"][0]
    assert b["status"] == "failed" and "failed" in b["error"]
    assert (await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["apk"]})).status_code == 200
    gh.run_status, gh.conclusion = "completed", "failure"
    await owner.post("/api/apps/manage/shell-builds", headers=auth)
    assert (await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["apk"]})).status_code == 429


async def test_inputs_are_validated_and_setup_required(env, monkeypatch):
    owner, app, auth, gh = await setup(env, monkeypatch)
    go = lambda **kw: owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["apk"], **kw})
    for bad in ("com.shop", "com.shop.app;rm -rf /", "com.1shop.app", "com.shop.app\nx"):
        assert (await go(package_id=bad)).status_code == 400, bad
    assert (await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["ios"]})).status_code == 400
    assert gh.dispatched == []
    assert (await go(package_id="com.myshop.app")).status_code == 200
    assert gh.dispatched[0]["inputs"]["package_id"] == "com.myshop.app"
    from llm_agent.ci_build import CIBuilder
    assert CIBuilder.clean_name('Tea "Shop" & <b>Co</b>; $(x)') == "Tea Shop bCob x"
    # http address, no token, no repo
    env.settings.update({"public_url": "http://localhost:8000"})
    assert (await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["exe"]})).status_code in (400, 409)
    monkeypatch.delenv("BUILD_GITHUB_TOKEN")
    r = await owner.post("/api/apps/manage/shell-builds", headers=auth)
    assert r.json()["available"] is False
    assert (await owner.post("/api/apps/manage/shell-build", headers=auth, json={"platforms": ["exe"]})).status_code == 503


async def test_app_address_setting_and_public_logo(env, monkeypatch):
    owner, app, auth, gh = await setup(env, monkeypatch)
    ok = await owner.post("/api/apps/manage/settings", headers=auth, json={"title": "Shop", "backend_url": "https://shop.example.com/app"})
    assert ok.status_code == 200, ok.text
    for bad in ("http://shop.example.com", "https://u:p@shop.example.com", "javascript:alert(1)", "ftp://x.com"):
        assert (await owner.post("/api/apps/manage/settings", headers=auth, json={"backend_url": bad})).status_code == 400, bad
    await owner.post("/api/apps/manage/publish", headers=auth)
    visitor = await env.new_client()
    logo = await visitor.get(f"/api/catalog/{app['app_id']}/logo")
    assert logo.status_code == 200 and logo.content[:4] == b"\x89PNG"
    assert (await visitor.get("/api/catalog/zzzzzzzzzz/logo")).status_code == 404


async def test_admin_can_download_the_build_repo_and_others_cannot(env):
    from tests.test_reporter import admin_client
    a = await admin_client(env)
    r = await a.get("/api/admin/build-repo.zip")
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert ".github/workflows/build-shell.yml" in names and "shell/lib/main.dart" in names and ".github/scripts/prepare.py" in names
    c = await env.signup("nobody")
    assert (await c.get("/api/admin/build-repo.zip")).status_code == 403
