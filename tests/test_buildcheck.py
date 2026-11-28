"""check_build: the assistant's pre-flight check of a project, and what the prompt tells it about the platform."""
from llm_agent import buildcheck
from llm_agent.config import SystemConfig
from llm_agent.core.prompts import build_system_prompt
from llm_agent.tools.file_manager import WorkspaceFiles

GOOD = {
    "app.py": 'import os\nfrom flask import Flask, jsonify\ntry:\n    from workbench_client import Client\nexcept ImportError:\n    Client = None\napp = Flask(__name__)\n'
              'API = os.environ.get("API_BASE_URL", "")\n@app.get("/api/items")\ndef items():\n    return jsonify([])\n'
              'app.run(host="0.0.0.0", port=int(os.environ["PORT"]))\n',
    "requirements.txt": "flask>=3,<4\n",
    "templates/index.html": '<script src="static/app.js"></script>',
    "static/app.js": 'fetch("api/items")',
    ".env.example": "API_BASE_URL=https://example.invalid\n",
    "manage.json": '{"sections": [{"name": "menu", "label": "Menu", "fields": [{"key": "title", "label": "Dish", "type": "text"}]}]}',
}


def test_good_project_passes():
    r = buildcheck.check(GOOD)
    assert r["ok"] and r["framework"] == "Flask" and r["problems"] == [] and r["warnings"] == []


def test_problems_and_warnings_are_specific():
    bad = dict(GOOD)
    bad["app.py"] = ('from flask import Flask\napp = Flask(__name__)\nSTRIPE = "sk_live_abcdefghijklmnop1234"\nSECRET = os.environ.get("MY_KEY")\n'
                     '@app.route("/admin")\ndef a(): pass\napp.run(host="127.0.0.1", port=5000, debug=True)\n')
    bad["static/app.js"] = 'fetch("/api/items")'
    del bad[".env.example"], bad["requirements.txt"]
    r = buildcheck.check(bad)
    text = " | ".join(r["problems"] + r["warnings"])
    assert not r["ok"]
    for needle in ("requirements.txt is missing", "PORT", "Stripe key", "Root-relative", "127.0.0.1", "debug=True", ".env.example", "/admin"):
        assert needle in text, needle
    assert "sk_live_abcdefghijklmnop1234" not in text           # the secret itself is never echoed back
    assert buildcheck.check({})["ok"] is False
    assert not buildcheck.check({"run.json": "{nope", "app.py": "x=1"})["ok"]


async def test_tool_reads_the_project_folder(tmp_path):
    from llm_agent.tools.registry import build_registry
    ws = WorkspaceFiles(str(tmp_path))
    for p, t in GOOD.items():
        ws.write(f"shop/{p}", t)
    ws.write("shop/venv/lib/junk.py", "SECRET = 'AKIAABCDEFGHIJKLMNOP'")        # virtualenv contents are not checked

    class Ctx:
        files = ws
    spec = build_registry()["check_build"]
    r = await spec.handler(Ctx(), {"path": "shop"})
    assert r["ok"] is True and r["files_checked"] == len(GOOD)
    import pytest
    from llm_agent.tools.file_manager import WorkspaceError
    with pytest.raises(WorkspaceError):
        await spec.handler(Ctx(), {"path": "nope"})
    assert spec.check_args({})[1] == ""                                         # path is optional


def test_prompt_describes_the_shell_model():
    for shell in (True, False):
        cfg = SystemConfig()
        cfg.shell_enabled = shell
        p = build_system_prompt(cfg, "orchestrator", "", "u", False, [], [])
        for needle in ("Manage app", "workbench_client", "check_build", "NOT packed", "Data and accounts belong", "Inbox"):
            assert needle in p, (shell, needle)
        assert ("Preview > Connections" in p) and "Android cannot pack" not in p


def test_manage_json_is_checked():
    assert any("manage.json is missing" in w for w in buildcheck.check({k: v for k, v in GOOD.items() if k != "manage.json"})["warnings"])
    bad = dict(GOOD, **{"manage.json": '{"sections": []}'})
    assert not buildcheck.check(bad)["ok"] and any("manage.json" in p for p in buildcheck.check(bad)["problems"])
    other = dict(GOOD, **{"app.py": GOOD["app.py"] + 'r = Client().sync("bookings")\n'})
    assert any("'bookings'" in w for w in buildcheck.check(other)["warnings"])


def test_prompt_asks_for_research_and_a_rich_design_json():
    cfg = SystemConfig()
    text = build_system_prompt(cfg, "orchestrator", "", "u", True, [], [])
    assert "Research from trusted sources" in text and "do not repeat a search" in text.lower() or "Search once per topic" in text


def test_design_process_and_both_platforms_are_taught_by_the_app_design_skill():
    from llm_agent import skills as sk
    _, body = sk.parse((sk.BUILTIN_DIR / "app-design.md").read_text())
    for needle in ("DESIGN PROCESS", "design_brief.md", "permissive licence", "bottom navigation", "side navigation rail", "WorkbenchShell/windows"):
        assert needle in body, needle
    assert 'use_skill("app-design")' in build_system_prompt(SystemConfig(), "orchestrator", "", "u", True, [], [])


def test_removed_flask_calls_and_syntax_errors_are_caught_before_deploy():
    files = {"app.py": "from flask import Flask\napp = Flask(__name__)\n@app.before_first_request\ndef setup():\n    pass\n", "requirements.txt": "flask\n"}
    out = buildcheck.check(files)
    assert any("before_first_request" in p for p in out["problems"])
    assert any("without a version" in w for w in out["warnings"])
    bad = buildcheck.check({"app.py": "def x(:\n", "requirements.txt": "flask>=3,<4\n"})
    assert any("syntax error on line 1" in p for p in bad["problems"])
    ok = buildcheck.check({"app.py": "from flask import Flask\napp = Flask(__name__)\n", "requirements.txt": "flask>=3,<4\n"})
    assert not any("removed" in p or "syntax" in p for p in ok["problems"])
