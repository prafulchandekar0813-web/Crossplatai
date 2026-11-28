"""Reading a copied project before changing it: framework, database, admin, content; and the rule that a copied repository is not edited before that."""
import json

import pytest

from llm_agent import project_audit
from llm_agent.project_gate import check, locked_folder
from llm_agent.tools.file_manager import WorkspaceError, WorkspaceFiles


def b(**kw):
    return {k.replace("__", "/").replace("_dot_", "."): (v if isinstance(v, bytes) else v.encode()) for k, v in kw.items()}


FLASK = {"app.py": b"import os\nfrom flask import Flask\nfrom flask_admin import Admin\napp = Flask(__name__)\nADMIN_PASSWORD = 'admin'\n"
                   b"app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))\n",
         "requirements.txt": b"flask==3.0\nflask-admin\nsqlalchemy\npsycopg2-binary\n",
         "menu.json": b"[]", "static/app.js": b"const items=[{name:'a',price:1},{name:'b',price:2},{name:'c',price:3}]"}


def test_a_flask_project_with_its_own_admin_is_described():
    r = project_audit.audit(FLASK)
    assert r["framework"] == "Flask" and r["language"] == "python" and r["hosting"]["support"] == "yes"
    assert r["start"]["command"] == "python app.py" and r["start"]["listens_on_port_env"]
    assert r["database"]["engines"] == ["postgres"] and r["database"]["orm"] == "SQLAlchemy"
    assert r["admin"]["exists"] and r["admin"]["kind"] == "Flask-Admin" and r["admin"]["path"] == "/admin/" and r["admin"]["default_passwords_in"] == ["app.py"]
    assert not r["admin"]["trusts_manage_login"] and r["content"]["data_files"] == ["menu.json"] and r["content"]["hard_coded_in"] == ["static/app.js"]
    assert any("default admin password" in n for n in r["notes"])
    assert project_audit.audit({**FLASK, "app.py": FLASK["app.py"] + b"# request.headers.get('X-Workbench-Admin')\n"})["admin"]["trusts_manage_login"]


def test_other_project_kinds():
    static = project_audit.audit({"index.html": b"<h1>x</h1>", "css/a.css": b"a{}"})
    assert static["framework"].startswith("Static") and static["admin"]["exists"] is False and static["hosting"]["support"] == "yes"
    vite = project_audit.audit({"package.json": json.dumps({"dependencies": {"react": "18"}, "scripts": {"build": "vite build"}}).encode(), "src/App.jsx": b"x"})
    assert vite["framework"] == "React" and vite["needs_build"] and vite["hosting"]["support"] == "build"
    node = project_audit.audit({"package.json": json.dumps({"dependencies": {"express": "4", "mongoose": "8"}, "scripts": {"start": "node s.js"}}).encode(), "s.js": b"app.get('/admin', f)"})
    assert node["framework"] == "Express" and node["hosting"]["support"] == "node" and node["database"]["engines"] == ["mongodb"] and node["admin"]["exists"]
    assert node["start"]["command"] == "npm start"
    django = project_audit.audit({"manage.py": b"x", "site/settings.py": b"INSTALLED_APPS=['django.contrib.admin']", "requirements.txt": b"django\n"})
    assert django["framework"] == "Django" and django["admin"]["kind"] == "Django admin"
    php = project_audit.audit({"index.php": b"<?php", "composer.json": b"{}"})
    assert php["hosting"]["support"] == "no" and "cannot be hosted" in php["hosting"]["why"]


def test_project_md_carries_the_marker_and_the_facts():
    md = project_audit.render_md(project_audit.audit(FLASK), "shop")
    assert md.startswith(project_audit.MARK) and "Flask" in md and "Flask-Admin at /admin/" in md and "NOT yet tied to the Manage login" in md


def test_a_copied_repository_is_locked_until_it_is_analysed(tmp_path):
    files = WorkspaceFiles(str(tmp_path))
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop/SOURCE.md").write_text("copy")
    (tmp_path / "shop/app.py").write_text("x")
    (tmp_path / "mine").mkdir()
    assert locked_folder(files, "shop/app.py") == "shop" and locked_folder(files, "shop/deep/new.py") == "shop" and locked_folder(files, "mine/app.py") == ""
    with pytest.raises(WorkspaceError, match="analyze_project"):
        check(files, ["shop/app.py"])
    (tmp_path / "shop/PROJECT.md").write_text("I wrote this myself, no marker")
    assert locked_folder(files, "shop/app.py") == "shop"        # the assistant cannot unlock it by writing the file itself
    (tmp_path / "shop/PROJECT.md").write_text(project_audit.MARK + "\n# Project")
    assert locked_folder(files, "shop/app.py") == ""
    check(files, ["shop/app.py", None, "mine/x"])


async def test_the_tools_enforce_the_lock_and_analyze_unlocks(tmp_path):
    from llm_agent.tools.registry import build_registry
    from tests.test_edit_tools import _ctx
    ctx, _ = _ctx(tmp_path)
    reg = build_registry()
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop/SOURCE.md").write_text("copy")
    (tmp_path / "shop/app.py").write_text("from flask import Flask\n")
    with pytest.raises(WorkspaceError, match="analyze_project"):
        await reg["write_file"].handler(ctx, {"path": "shop/new.py", "content": "x"})
    with pytest.raises(WorkspaceError):
        await reg["patch_files"].handler(ctx, {"files": [{"path": "shop/app.py", "edits": [{"old": "a", "new": "b"}]}]})
    res = await reg["analyze_project"].handler(ctx, {"path": "shop"})
    assert res["status"] == "success" and res["report"]["framework"] == "Flask" and (tmp_path / "shop/PROJECT.md").exists()
    await reg["write_file"].handler(ctx, {"path": "shop/new.py", "content": "x"})
    assert (tmp_path / "shop/new.py").exists()
