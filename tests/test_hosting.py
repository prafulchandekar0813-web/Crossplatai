"""Hosting a managed app's backend: the vault for the owner's keys, the Docker flags, the lifecycle, and the public proxy."""
import base64
import json
import os
import secrets as pysecrets

import pytest

from llm_agent import hosting
from llm_agent.vault import AppSecrets, Vault, VaultError, check_pair
from tests.test_appkit import b64
from tests.test_apps import make_zip

SERVER = '''import os, json
import workbench_client      # added by the platform next to the app's own files
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, "key": os.environ.get("STRIPE_KEY", ""), "data": os.environ.get("DATA_DIR", "")}).encode()
        ctype = "text/html" if self.path == "/" else "application/json"
        if self.path == "/": body = b"<html><head></head><body><link href='/a.css'>shop</body></html>"
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): print("served", self.path, "key was", os.environ.get("STRIPE_KEY", ""), flush=True)
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''
PROJECT = {"app.py": SERVER, "run.json": json.dumps({"start": "python3 app.py"})}


def new_key():
    return base64.urlsafe_b64encode(pysecrets.token_bytes(32)).decode()


@pytest.fixture
def master(monkeypatch):
    k = new_key()
    monkeypatch.setenv("SECRETS_MASTER_KEY", k)
    monkeypatch.delenv("SECRETS_MASTER_KEY_OLD", raising=False)
    return k


# ---- vault ------------------------------------------------------------------------------------------

def test_vault_roundtrip_binds_value_to_app_and_name(master):
    v = Vault()
    blob = v.encrypt("app1", "STRIPE_KEY", "sk_live_abc")
    assert "sk_live_abc" not in blob and blob.startswith("v1.")
    assert v.decrypt("app1", "STRIPE_KEY", blob) == "sk_live_abc"
    for app, name in (("app2", "STRIPE_KEY"), ("app1", "OTHER_KEY")):         # moved ciphertext must not open
        with pytest.raises(VaultError):
            v.decrypt(app, name, blob)
    head, kid, payload = blob.split(".")
    raw = bytearray(base64.urlsafe_b64decode(payload))
    raw[-1] ^= 1
    with pytest.raises(VaultError):
        v.decrypt("app1", "STRIPE_KEY", f"{head}.{kid}.{base64.urlsafe_b64encode(bytes(raw)).decode()}")
    assert v.encrypt("app1", "STRIPE_KEY", "sk_live_abc") != blob              # fresh nonce every time


def test_vault_needs_a_valid_master_key_and_wrong_key_fails(monkeypatch, master):
    v = Vault()
    blob = v.encrypt("a", "K", "val")
    monkeypatch.setenv("SECRETS_MASTER_KEY", new_key())
    with pytest.raises(VaultError):
        v.decrypt("a", "K", blob)
    for bad in ("", "short", "!!!"):
        monkeypatch.setenv("SECRETS_MASTER_KEY", bad)
        assert not v.available()
        with pytest.raises(VaultError) as e:
            v.encrypt("a", "K", "x")
        assert e.value.status == 503


async def test_rotation_reencrypts_under_the_new_key(env, monkeypatch, master):
    s = AppSecrets(env.orch.db)
    await s.set("app1", "DB_URL", "postgres://u:p@h/db")
    old = master
    monkeypatch.setenv("SECRETS_MASTER_KEY", new_key())
    monkeypatch.setenv("SECRETS_MASTER_KEY_OLD", old)
    assert (await s.plain("app1"))["DB_URL"] == "postgres://u:p@h/db"            # still readable during rotation
    assert await s.rotate() == 1 and await s.rotate() == 0
    monkeypatch.delenv("SECRETS_MASTER_KEY_OLD")
    assert (await s.plain("app1"))["DB_URL"] == "postgres://u:p@h/db"


async def test_secret_names_and_limits(env, master):
    s = AppSecrets(env.orch.db)
    for bad in ("lower", "PATH", "", "A B", "X" * 80):
        with pytest.raises(VaultError):
            check_pair(bad, "v")
    with pytest.raises(VaultError):
        await s.set("a", "GOOD", "   ")
    await s.set("a", "GOOD", "v")
    assert [n["name"] for n in await s.names("a")] == ["GOOD"] and "v" not in json.dumps(await s.names("a"))
    row = await env.orch.db.fetchone("SELECT blob FROM app_secrets WHERE app_id='a'")
    assert "v" != row["blob"] and (await s.delete("a", "GOOD")) and not await s.delete("a", "GOOD")


# ---- docker runtime: exact flags, secrets never on the command line ------------------------------------

async def test_docker_run_is_locked_down_and_the_env_file_is_deleted(tmp_path):
    seen = []
    state = {}

    async def runner(argv, timeout=120, input=None):
        seen.append(argv)
        if "--env-file" in argv:
            path = argv[argv.index("--env-file") + 1]
            state["perm"] = oct(os.stat(path).st_mode & 0o777)
            state["body"] = open(path).read()
            state["path"] = path
        return 0, ""
    rt = hosting.DockerRuntime(tmp_path, runner)
    await rt.start("abc123", "wb-app-abc123:1", 9001, {"STRIPE_KEY": "sk_live_zzz", "BAD": "a\nb"}, hosting.Limits(256, 0.5, "appnet"))
    run = next(a for a in seen if a[:2] == ["docker", "run"])
    flat = " ".join(run)
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--user 10001", "--pids-limit 128", "--memory 256m", "--cpus 0.5",
                 "127.0.0.1:9001:8000", "--network appnet", "wb-data-abc123:/data"):
        assert flag in flat, flag
    assert "--privileged" not in flat and "docker.sock" not in flat and "sk_live_zzz" not in flat      # key only via the env file
    assert state["perm"] == "0o600" and "STRIPE_KEY=sk_live_zzz" in state["body"] and "BAD" not in state["body"]
    assert not os.path.exists(state["path"])


def test_dockerfile_runs_unprivileged_and_requirements_are_cleaned():
    d = hosting.dockerfile("python3 app.py", True)
    assert "USER 10001" in d and "ENV PORT=8000" in d and "requirements.txt" in d
    assert "--index-url" not in hosting.clean_requirements("flask\n--index-url http://evil\n-r other.txt\nrequests\n")


# ---- manager lifecycle with a fake runtime -----------------------------------------------------------

class FakeRuntime:
    name = "fake"

    def __init__(self):
        self.up, self.log = {}, "boot ok"
        self.started = []

    async def available(self):
        return True, ""

    async def deploy(self, app_id, files, start, rev):
        return f"img:{rev}"

    async def start(self, app_id, image, port, env, limits):
        self.up[app_id] = True
        self.started.append((app_id, port, dict(env)))

    async def stop(self, app_id, remove=False):
        self.up[app_id] = False

    async def state(self, app_id):
        return "running" if self.up.get(app_id) else "stopped"

    async def logs(self, app_id, lines=200):
        return self.log

    async def remove(self, app_id):
        self.up.pop(app_id, None)


@pytest.fixture
def mgr(env, master, monkeypatch):
    async def ready(port, seconds=0, alive=None):
        return True
    monkeypatch.setattr(hosting, "wait_ready", ready)
    clock = [1000.0]
    rt = FakeRuntime()
    m = hosting.HostingManager(env.orch.db, env.orch.app_secrets, env.settings, env.orch.data_dir / "h", runtime=rt, clock=lambda: clock[0])
    m.rt, m.clock_box = rt, clock
    return m


async def test_deploy_start_sleep_wake_stop(mgr):
    files = {"app.py": b"print(1)"}
    await mgr.secrets.set("app1", "STRIPE_KEY", "sk_live_q")
    await mgr.deploy("app1", files)
    assert (await mgr.info("app1"))["state"] == "running"
    assert mgr.rt.started[0][2] == {"STRIPE_KEY": "sk_live_q"}                  # decrypted only for the container
    assert "sk_live_q" not in json.dumps(await mgr.info("app1"))
    mgr.rt.log = "error: bad key sk_live_q"
    assert "sk_live_q" not in await mgr.logs("app1") and "***" in await mgr.logs("app1")
    mgr.clock_box[0] += 31 * 60                                                 # nobody visits for half an hour
    assert await mgr.reap() == 1 and (await mgr.info("app1"))["state"] == "sleeping"
    port = await mgr.endpoint("app1")                                           # a visit wakes it
    assert port and (await mgr.info("app1"))["state"] == "running"
    await mgr.stop("app1")                                                      # the owner stopped it: visits do not wake it
    assert await mgr.endpoint("app1") is None and (await mgr.info("app1"))["state"] == "stopped"
    await mgr.start("app1")
    assert await mgr.endpoint("app1")
    await mgr.remove("app1")
    assert await mgr.endpoint("app1") is None and await mgr.secrets.names("app1") == []


async def test_failed_start_reports_the_log_without_secrets(mgr, monkeypatch):
    async def never(port, seconds=0, alive=None):
        return False
    monkeypatch.setattr(hosting, "wait_ready", never)
    await mgr.secrets.set("a1", "TOKEN", "tok-123456")
    mgr.rt.log = "crashed using tok-123456"
    with pytest.raises(hosting.HostingError) as e:
        await mgr.deploy("a1", {"app.py": b"x"})
    info = await mgr.info("a1")
    assert info["state"] == "failed" and "tok-123456" not in info["error"] and "tok-123456" not in str(e.value)


async def test_running_apps_cap_and_unknown_modes(mgr, env):
    env.settings.update({"hosting_max_running": 1})
    await mgr.deploy("a1", {"app.py": b"x"})
    with pytest.raises(hosting.HostingError) as e:
        await mgr.deploy("a2", {"app.py": b"x"})
    assert e.value.status == 503
    with pytest.raises(hosting.HostingError):
        await mgr.set_data_mode("a1", "cloud")
    await mgr.set_data_mode("a1", "provider")
    assert (await mgr.info("a1"))["data_mode"] == "provider"


def test_process_runtime_refuses_root_unless_allowed(monkeypatch, tmp_path):
    import asyncio
    rt = hosting.ProcessRuntime(tmp_path)
    monkeypatch.delenv("HOSTING_ALLOW_ROOT_PROCESS", raising=False)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        assert asyncio.run(rt.available())[0] is False


# ---- end to end through the API with a real process --------------------------------------------------

async def test_owner_hosts_the_app_and_visitors_reach_it(env, master, monkeypatch):
    from tests.test_appkit import make_kit
    monkeypatch.setenv("HOSTING_ALLOW_ROOT_PROCESS", "1")
    env.settings.update({"hosting_runtime": "process"})
    owner = await env.signup("hoster")
    app = (await owner.post("/api/apps/create", json={"name": "Tea Shop", "version": "1.0.0", "project": b64(make_zip(PROJECT))})).json()
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    st = (await owner.post("/api/apps/manage/hosting", headers=auth)).json()
    assert st["state"] == "not deployed" and st["available"] and st["secrets"] == [] and st["vault"]
    saved = await owner.post("/api/apps/manage/hosting/secret", headers=auth, json={"name": "STRIPE_KEY", "value": "sk_live_topsecret"})
    assert saved.status_code == 200 and "sk_live_topsecret" not in saved.text and saved.json()["secrets"][0]["name"] == "STRIPE_KEY"
    bad = await owner.post("/api/apps/manage/hosting/secret", headers=auth, json={"name": "bad name", "value": "x"})
    assert bad.status_code == 400 and bad.json()["error"]                       # the page shows this text, so it must be in "error"
    assert (await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "start"})).status_code == 409   # not deployed yet
    try:
        dep = await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "deploy"})
        assert dep.status_code == 200, dep.text
        assert dep.json()["state"] == "running" and dep.json()["address"].endswith(f"/live/{app['app_id']}/")
        visitor = await env.new_client()
        page = await visitor.get(f"/live/{app['app_id']}/")
        assert page.status_code == 200 and "shop" in page.text and f'/live/{app["app_id"]}/a.css' in page.text      # prefix rewritten
        assert page.headers["content-security-policy"].startswith("sandbox")
        data = await visitor.get(f"/live/{app['app_id']}/api/x?y=1")
        assert data.json()["path"] == "/api/x?y=1" and data.json()["key"] == "sk_live_topsecret"                  # the app got its key
        assert data.json()["data"].endswith("data")
        # the installed app is pointed at the live address, never at a draft
        cat = (await visitor.get(f"/api/catalog/{app['app_id']}/products")).json()
        assert cat["settings"]["backend_url"].endswith(f"/live/{app['app_id']}/")
        # logs hide the key; another owner cannot touch this app
        logs = (await owner.post("/api/apps/manage/hosting/logs", headers=auth)).json()["logs"]
        assert "served" in logs and "sk_live_topsecret" not in logs
        other = await env.signup("other")
        assert (await other.post("/api/apps/manage/hosting", headers={"X-App-Id": app["app_id"], "X-App-Password": "wrong"})).status_code == 401
        stop = await owner.post("/api/apps/manage/hosting/action", headers=auth, json={"action": "stop"})
        assert stop.json()["state"] == "stopped"
        assert (await visitor.get(f"/live/{app['app_id']}/")).status_code == 503
    finally:
        await env.orch.hosting.stop_all()
    assert (await owner.post("/api/apps/manage/hosting/secret/delete", headers=auth, json={"name": "STRIPE_KEY"})).status_code == 200
    assert (await owner.post("/api/apps/manage/hosting/secret/delete", headers=auth, json={"name": "STRIPE_KEY"})).status_code == 404
    assert (await visitor.get("/live/nonexistent1/")).status_code == 404
