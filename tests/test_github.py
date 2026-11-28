"""Finding open-source UIs on GitHub and copying one into the workspace, safely and with its licence."""
import io
import stat
import zipfile

import httpx
import pytest

from llm_agent.tools.file_manager import WorkspaceFiles
from llm_agent.tools.github_repos import GitHubRepos, parse_repo


def make_zip(extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("menu-ui-main/index.html", "<h1>Menu</h1>")
        z.writestr("menu-ui-main/css/site.css", "body{margin:0}")
        z.writestr("menu-ui-main/LICENSE", "MIT License\nCopyright")
        z.writestr("menu-ui-main/img/hero.png", b"\x89PNG" + b"0" * 50)
        z.writestr("menu-ui-main/node_modules/x/index.js", "bad")
        z.writestr("menu-ui-main/tool.exe", "MZ")
        z.writestr("menu-ui-main/big.js", "x" * 2_000_000)
        z.writestr("menu-ui-main/../../evil.txt", "escape")
        link = zipfile.ZipInfo("menu-ui-main/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "/etc/passwd")
        for n, d in (extra or {}).items():
            z.writestr("menu-ui-main/" + n, d)
    return buf.getvalue()


def client(license_id="MIT", archive=None, search_status=200):
    def handler(req):
        url = str(req.url)
        if "/search/repositories" in url:
            if search_status != 200:
                return httpx.Response(search_status, headers={"x-ratelimit-remaining": "0"}, json={})
            return httpx.Response(200, json={"items": [
                {"full_name": "a/menu-ui", "description": "Menu", "stargazers_count": 900, "language": "JavaScript", "license": {"spdx_id": "MIT"}, "size": 800, "pushed_at": "2026-01-02T00:00:00Z", "topics": ["menu"]},
                {"full_name": "b/gpl-ui", "stargazers_count": 5000, "license": {"spdx_id": "GPL-3.0"}, "size": 100},
                {"full_name": "c/nolicense", "stargazers_count": 3000, "license": None, "size": 100},
                {"full_name": "d/huge", "stargazers_count": 100, "license": {"spdx_id": "MIT"}, "size": 900000}]})
        if url.endswith("/repos/a/menu-ui"):
            return httpx.Response(200, json={"name": "menu-ui", "default_branch": "main", "size": 800, "stargazers_count": 900, "license": {"spdx_id": license_id}})
        if url.endswith("/repos/a/missing"):
            return httpx.Response(404, json={})
        if "codeload.github.com/a/menu-ui/zip/refs/heads/main" in url:
            return httpx.Response(200, content=archive if archive is not None else make_zip())
        return httpx.Response(500)
    return GitHubRepos(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_repository_names_are_parsed_strictly():
    assert parse_repo("a/menu-ui") == ("a", "menu-ui")
    assert parse_repo("https://github.com/a/menu-ui.git") == ("a", "menu-ui")
    assert parse_repo("https://github.com/a/menu-ui/tree/main/src") == ("a", "menu-ui")
    for bad in ("", "menu", "http://evil.test/a/b", "a/b/c d"):
        with pytest.raises(Exception):
            parse_repo(bad)


async def test_search_keeps_only_reusable_projects():
    r = await client().search("restaurant menu ui")
    assert r["status"] == "success" and [x["repo"] for x in r["results"]] == ["a/menu-ui"] and "3 more" in r["note"]
    assert (await client().search("hi"))["status"] == "error"
    limited = await client(search_status=403).search("restaurant menu ui")
    assert limited["status"] == "error" and "limit" in limited["error"]


async def test_clone_copies_safely_and_records_the_source(tmp_path):
    files = WorkspaceFiles(str(tmp_path))
    r = await client().clone(files, "a/menu-ui", "")
    assert r["status"] == "success" and r["dest"] == "menu-ui" and r["license"] == "MIT"
    assert (tmp_path / "menu-ui/index.html").read_text() == "<h1>Menu</h1>"
    assert (tmp_path / "menu-ui/img/hero.png").exists() and (tmp_path / "menu-ui/LICENSE").exists()
    assert "github.com/a/menu-ui" in (tmp_path / "menu-ui/SOURCE.md").read_text()
    for gone in ("node_modules", "tool.exe", "big.js", "link"):
        assert not (tmp_path / "menu-ui" / gone).exists()
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()
    assert r["skipped"] >= 4 and "index.html" in r["overview"]["entry_points"] and "css/site.css" in r["overview"]["ui_files"]


async def test_clone_one_folder_and_refuse_bad_licences_and_missing_repos(tmp_path):
    files = WorkspaceFiles(str(tmp_path))
    only = await client(archive=make_zip({"ui/app.html": "<p>x</p>"})).clone(files, "a/menu-ui", "shop/ui", "ui")
    assert only["status"] == "success" and (tmp_path / "shop/ui/app.html").exists() and not (tmp_path / "shop/ui/index.html").exists()
    gpl = await client(license_id="GPL-3.0").clone(files, "a/menu-ui", "x")
    assert gpl["status"] == "error" and "licence" in gpl["error"] and not (tmp_path / "x").exists()
    assert "does not exist" in (await client().clone(files, "a/missing", "x"))["error"]
    assert (await client().clone(files, "not a repo", "x"))["status"] == "error"
    bad = await client(archive=b"not a zip").clone(files, "a/menu-ui", "y")
    assert bad["status"] == "error" and "zip" in bad["error"]


async def test_clone_respects_the_workspace_quota(tmp_path):
    files = WorkspaceFiles(str(tmp_path), quota_bytes=100)
    r = await client().clone(files, "a/menu-ui", "")
    assert r["status"] == "error" and "quota" in r["error"]


def test_the_new_tools_are_offered_and_the_skill_is_listed():
    from llm_agent.skills import BUILTIN_DIR
    from llm_agent.tools.registry import build_registry, definitions
    names = {d["function"]["name"] for d in definitions(build_registry(), "orchestrator", True, True, False)}
    assert {"github_search", "github_clone", "research_web", "web_search", "read_url"} <= names
    assert (BUILTIN_DIR / "github-ui.md").exists()
