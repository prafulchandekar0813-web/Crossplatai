"""Where an app's data lives: the catalogue and address checks, local databases and backups (with a fake Docker), and the owner's choice."""
import json
import sqlite3
import time

import pytest

from llm_agent import datastore, hosting
from llm_agent.db import Database
from llm_agent.hosting import HostingError
from tests.test_hosting import FakeRuntime, master  # noqa: F401  (fixture)

PG = "postgresql://u:secretpw@db.example.com:5432/app"
KEY = json.dumps({"type": "service_account", "project_id": "p", "private_key": "-----BEGIN PRIVATE KEY-----x"})


def test_addresses_are_checked_for_the_right_database():
    assert datastore.parse("postgres", PG) == ("db.example.com", 5432)
    assert datastore.parse("mongodb", "mongodb+srv://u:p@c.mongodb.net/db") == ("c.mongodb.net", 27017)
    assert datastore.parse("redis", "rediss://:p@x.upstash.io:6380") == ("x.upstash.io", 6380)
    assert datastore.parse("firestore", KEY)[0] == "firestore.googleapis.com"
    for engine, bad in (("postgres", "mysql://u:p@h/db"), ("postgres", "not a url"), ("postgres", ""), ("postgres", "postgres://u:p@/db"),
                        ("firestore", "{}"), ("firestore", "nope"), ("redis", "redis://h:abc/0")):
        with pytest.raises(datastore.DataError):
            datastore.parse(engine, bad)
    with pytest.raises(datastore.DataError, match="online service"):
        datastore.check_engine("firestore", "local")


async def test_connection_test_never_shows_the_password(monkeypatch):
    async def up(h, p, s=6): return True
    async def down(h, p, s=6): return False
    monkeypatch.setattr(datastore, "reachable", up)
    ok = await datastore.test("postgres", PG)
    assert ok["ok"] and "secretpw" not in json.dumps(ok)
    monkeypatch.setattr(datastore, "reachable", down)
    bad = await datastore.test("postgres", PG)
    assert not bad["ok"] and "secretpw" not in json.dumps(bad) and "IP address" in bad["message"]
    assert (await datastore.test("firestore", KEY))["ok"] and not (await datastore.test("postgres", "x"))["ok"]


def test_the_choice_made_in_the_chat_is_read_defensively():
    assert datastore.read_choice(b'{"mode":"online","engine":"postgres","provider":"supabase"}') == {"mode": "provider", "engine": "postgres", "provider": "supabase"}
    assert datastore.read_choice(b'{"mode":"local","engine":"mongodb"}') == {"mode": "local", "engine": "sqlite", "provider": ""}      # local is always the .db file
    for bad in (b"", b"[]", b"{", b'{"mode":"x","engine":"postgres"}'):
        assert datastore.read_choice(bad) == {}
    assert {p["engine"] for p in datastore.PROVIDERS} - {""} <= set(datastore.ENGINES)


class FakeDocker:
    def __init__(self):
        self.calls, self.boxes, self.networks, self.volumes, self.envfiles, self.ready = [], {}, set(), set(), [], True

    async def __call__(self, argv, timeout=120, input=None):
        self.calls.append(argv)
        a = argv[1:]
        if a[:2] == ["network", "inspect"]:
            return (0, "") if a[2] in self.networks else (1, "")
        if a[:2] == ["network", "create"]:
            self.networks.add(a[-1])
        elif a[:2] == ["network", "rm"]:
            self.networks.discard(a[2])
        elif a[:2] == ["volume", "inspect"]:
            return (0, "") if a[2] in self.volumes else (1, "")
        elif a[:2] == ["volume", "rm"]:
            self.volumes.discard(a[-1])
        elif a[0] == "inspect":
            return (0, self.boxes[a[-1]]) if a[-1] in self.boxes else (1, "")
        elif a[0] == "run" and "-d" in a:
            self.boxes[a[a.index("--name") + 1]] = "running"
            self.envfiles.append(open(a[a.index("--env-file") + 1]).read())
        elif a[0] == "run":                                           # a one-off helper: tar create or extract
            if "czf" in a:
                dest = a[a.index("-v", a.index("-v") + 1) + 1].split(":")[0] + "/" + a[a.index("czf") + 1].rsplit("/", 1)[1]
                open(dest, "wb").write(b"tgz")
        elif a[0] == "exec":
            return (0, "") if self.ready else (1, "not ready")
        elif a[0] == "stop":
            self.boxes[a[-1]] = "exited"
        elif a[0] == "start":
            self.boxes[a[-1]] = "running"
        elif a[:2] == ["rm", "-f"]:
            self.boxes.pop(a[-1], None)
        return 0, ""


@pytest.fixture
def dock(env, master, monkeypatch):
    async def ready(port, seconds=0, alive=None): return True
    monkeypatch.setattr(hosting, "wait_ready", ready)
    rt, d = FakeRuntime(), FakeDocker()
    rt.name = "docker"
    m = hosting.HostingManager(env.orch.db, env.orch.app_secrets, env.settings, env.orch.data_dir / "h", runtime=rt, clock=lambda: 1_700_000_000.0, runner=d)
    async def nosleep(_): return None
    m.rt, m.dk = rt, d
    return m


async def test_default_is_a_sqlite_file_and_the_app_is_told_where(dock):
    await dock.deploy("app1", {"app.py": b"x"})
    e = dock.rt.started[0][2]
    assert e["DATABASE_ENGINE"] == "sqlite" and e["DATABASE_URL"] == "sqlite:////data/app.db"
    assert (await dock.info("app1"))["data"]["mode"] == "local"


async def test_a_provider_address_is_tested_saved_encrypted_and_handed_to_the_app(dock, monkeypatch):
    async def down(h, p, s=6): return False
    monkeypatch.setattr(datastore, "reachable", down)
    with pytest.raises(HostingError, match="Could not reach"):
        await dock.configure_data("app1", "provider", "postgres", "supabase", PG)
    assert (await dock.secrets.names("app1")) == []
    async def up(h, p, s=6): return True
    monkeypatch.setattr(datastore, "reachable", up)
    r = await dock.configure_data("app1", "provider", "postgres", "supabase", PG)
    assert r["restart_needed"] and not r["changed"]
    stored = await dock.db.fetchone("SELECT blob FROM app_secrets WHERE app_id='app1'")
    assert "secretpw" not in stored["blob"]
    view = await dock.data_view("app1")
    assert view["mode"] == "provider" and view["engine"] == "postgres" and view["has_address"] and "secretpw" not in json.dumps(view)
    await dock.deploy("app1", {"app.py": b"x"})
    assert dock.rt.started[0][2]["DATABASE_URL"] == PG and dock.rt.started[0][2]["DATABASE_ENGINE"] == "postgres"
    assert not dock.dk.calls                                       # no local database was started for a provider
    again = await dock.configure_data("app1", "local", "sqlite")
    assert again["changed"] and "not copied" in again["note"]
    assert "DATABASE_URL" not in await dock.secrets.plain("app1")  # the old provider address no longer overrides the local file


async def test_provider_mode_without_an_address_stops_before_building(dock):
    await dock.db.execute("INSERT INTO hosted_apps(app_id, data_mode, data_engine) VALUES ('app2','provider','postgres')")
    with pytest.raises(HostingError, match="no address is saved") as e:
        await dock.deploy("app2", {"app.py": b"x"})
    assert e.value.status == 409
    with pytest.raises(HostingError, match="online service"):
        await dock.configure_data("app2", "local", "firestore")
    with pytest.raises(HostingError, match="Paste the database address"):
        await dock.configure_data("app2", "provider", "mysql")


async def test_the_chat_choice_applies_once_and_never_overrides_the_owner(dock):
    assert await dock.apply_choice("app1", {"mode": "provider", "engine": "redis", "provider": "upstash"})
    assert (await dock.data_view("app1"))["engine"] == "redis"
    await dock.configure_data("app1", "local", "sqlite")
    assert not await dock.apply_choice("app1", {"mode": "provider", "engine": "redis", "provider": "upstash"})
    assert (await dock.data_view("app1"))["engine"] == "sqlite"


async def test_backups_pause_the_app_pack_the_data_folder_and_keep_thirty_days(dock):
    await dock.deploy("app1", {"app.py": b"x"})
    dock.dk.volumes |= {"wb-data-app1"}
    dock.dk.boxes["wb-app-app1"] = "running"                       # the fake runtime does not use the fake Docker, so say the app container is up
    made = await dock.backup_create("app1")
    assert made["files"] == [made["name"] + "-data.tgz"]
    assert [c[-1] for c in dock.dk.calls if c[1] == "stop"][-1:] == ["wb-app-app1"] and dock.dk.boxes["wb-app-app1"] == "running"
    listing = dock.backups.list("app1")
    assert len(listing) == 1 and listing[0]["name"] == made["name"] and len(listing[0]["files"]) == 1
    assert await dock.backup_restore("app1", made["name"]) == ["data"]
    with pytest.raises(HostingError, match="Choose a backup"):
        await dock.backup_restore("app1", "../../etc")
    with pytest.raises(HostingError, match="no longer exists"):
        await dock.backup_restore("app1", "20200101-000000")
    old = 1_700_000_000.0 - 40 * 86400                              # the fixture clock
    import os
    for f in dock.backups.dir("app1").glob("*.tgz"):
        os.utime(f, (old, old))
    assert dock.backups.prune("app1") == 1 and dock.backups.list("app1") == []
    assert await dock.backup_all() == 1


async def test_removing_the_app_removes_its_backups(dock):
    await dock.deploy("app1", {"app.py": b"x"})
    dock.dk.volumes.add("wb-data-app1")
    await dock.backup_create("app1")
    await dock.remove("app1")
    assert not dock.backups.dir("app1").exists()


def test_an_old_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE hosted_apps (app_id TEXT PRIMARY KEY, desired TEXT NOT NULL DEFAULT 'stopped', data_mode TEXT NOT NULL DEFAULT 'local', image TEXT NOT NULL DEFAULT '', "
              "host_port INTEGER NOT NULL DEFAULT 0, deployed_at REAL NOT NULL DEFAULT 0, last_started REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')")
    c.execute("INSERT INTO hosted_apps(app_id) VALUES ('a1')")
    c.commit()
    c.close()
    db = Database(str(path))
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(hosted_apps)")}
    assert {"data_engine", "data_provider", "data_configured"} <= cols
    assert dict(db._conn.execute("SELECT data_engine, data_configured FROM hosted_apps").fetchone()) == {"data_engine": "sqlite", "data_configured": 0}


async def test_the_owner_chooses_where_data_lives_through_the_api(env, master, monkeypatch):
    from tests.test_appkit import b64
    from tests.test_apps import make_zip
    from tests.test_hosting import PROJECT
    async def up(h, p, s=6): return True
    monkeypatch.setattr(datastore, "reachable", up)
    owner = await env.signup("dbowner")
    app = (await owner.post("/api/apps/create", json={"name": "Tea Shop", "version": "1.0.0", "project": b64(make_zip(PROJECT))})).json()
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    base = "/api/apps/manage/hosting"
    view = (await owner.post(base, headers=auth)).json()
    assert view["data"]["mode"] == "local" and view["data"]["engine"] == "sqlite" and {e["id"] for e in view["data"]["engines"]} >= {"postgres", "firestore"}
    assert (await owner.post(base + "/data/test", headers=auth, json={"engine": "postgres", "value": PG})).json()["ok"] is True
    assert (await owner.post(base + "/data/test", headers=auth, json={"engine": "postgres", "value": "mysql://x"})).json()["ok"] is False
    saved = await owner.post(base + "/data", headers=auth, json={"mode": "provider", "engine": "postgres", "provider": "neon", "value": PG})
    assert saved.status_code == 200 and "secretpw" not in saved.text and saved.json()["data"]["has_address"] and saved.json()["restart_needed"]
    bad = await owner.post(base + "/data", headers=auth, json={"mode": "local", "engine": "firestore"})
    assert bad.status_code == 400 and "online service" in bad.json()["error"]
    assert (await owner.post(base + "/data", headers=auth, json={"mode": "local"})).status_code == 200     # the older form still works
    assert (await owner.post(base + "/backups", headers=auth)).json() == {"backups": []}
    assert (await owner.post(base + "/backups/create", headers=auth)).status_code in (409, 503)             # nothing deployed / no Docker
    assert (await owner.post(base + "/data", json={"mode": "local", "engine": "sqlite"})).status_code in (401, 403)


def test_javascript_projects_get_a_dockerfile_that_never_runs_install_scripts():
    pkg = lambda **k: {"package.json": json.dumps(k).encode()}
    spa = hosting.node_plan(pkg(dependencies={"react": "18"}, scripts={"build": "vite build"}))
    assert spa["kind"] == "static"
    df = hosting.node_dockerfile(spa, lockfile=True)
    assert df.count("FROM node") == 2 and "npm ci --ignore-scripts" in df and "npm run build" in df and "serve@14.2.4" in df and "USER 10001" in df and "-s /site" in df
    api = hosting.node_plan(pkg(dependencies={"express": "4"}, scripts={"start": "node s.js"}))
    assert api["kind"] == "server" and not api["build"]
    dfs = hosting.node_dockerfile(api, lockfile=False)
    assert "npm install --ignore-scripts" in dfs and "npm run build" not in dfs and '"npm start"' in dfs
    assert hosting.node_plan({**pkg(scripts={"start": "x"}), "app.py": b"x"}) is None            # a Python entry file wins
    assert hosting.node_plan({}) is None and hosting.node_plan({"package.json": b"{"}) is None and hosting.node_plan(pkg(name="x")) is None


async def test_javascript_hosting_needs_docker(dock):
    js = {"package.json": json.dumps({"dependencies": {"express": "4"}, "scripts": {"start": "node s.js"}}).encode(), "s.js": b"x"}
    dock.rt.name = "process"
    with pytest.raises(HostingError, match="Docker"):
        await dock.deploy("app1", js)
    dock.rt.name = "docker"
    await dock.deploy("app1", js)
    assert dock.rt.started
