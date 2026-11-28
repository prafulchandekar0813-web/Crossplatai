"""Where an app keeps its data: the database types we know, the providers we show as presets, and checking an address before it is saved.

Two places are possible for every app (hosted_apps.data_mode):
  local     - on this server: always one SQLite .db file in the app's private data folder (app.db).
  provider  - the owner's own service (Supabase, Neon, Atlas, Upstash, Firebase, ...). Only an address (or key file) is needed, so any provider
              that hands out one works; the names below only add hints. The address is stored encrypted and handed to the app as an environment variable.
Checking an address is honest about its limits: we test that it is well formed and that the host answers on its port. The login itself is checked by the app.
"""
import asyncio
import json
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

LOCAL, PROVIDER = "local", "provider"


class DataError(Exception):
    pass


# engine -> label, schemes the address may use, default port, secret (environment variable) name, whether we can run it on this server
ENGINES: Dict[str, Dict[str, Any]] = {
    "sqlite": {"label": "SQLite (a file)", "schemes": ("sqlite",), "port": 0, "secret": "DATABASE_URL", "local": True},
    "postgres": {"label": "PostgreSQL", "schemes": ("postgres", "postgresql"), "port": 5432, "secret": "DATABASE_URL", "local": False},
    "mysql": {"label": "MySQL / MariaDB", "schemes": ("mysql", "mariadb", "mysql+pymysql"), "port": 3306, "secret": "DATABASE_URL", "local": False},
    "mongodb": {"label": "MongoDB", "schemes": ("mongodb", "mongodb+srv"), "port": 27017, "secret": "DATABASE_URL", "local": False},
    "redis": {"label": "Redis", "schemes": ("redis", "rediss"), "port": 6379, "secret": "REDIS_URL", "local": False},
    "couchdb": {"label": "CouchDB", "schemes": ("http", "https"), "port": 5984, "secret": "DATABASE_URL", "local": False},
    "libsql": {"label": "libSQL / Turso", "schemes": ("libsql", "https", "wss"), "port": 443, "secret": "DATABASE_URL", "local": False},
    "firestore": {"label": "Firebase Firestore", "schemes": (), "port": 0, "secret": "FIREBASE_SERVICE_ACCOUNT", "local": False},
}
PROVIDERS: List[Dict[str, str]] = [
    {"id": "supabase", "label": "Supabase", "engine": "postgres", "hint": "Project settings > Database > Connection string (URI)."},
    {"id": "neon", "label": "Neon", "engine": "postgres", "hint": "Dashboard > Connection details > the connection string."},
    {"id": "aiven", "label": "Aiven", "engine": "postgres", "hint": "Service overview > Service URI (PostgreSQL, MySQL or Redis)."},
    {"id": "rds", "label": "Amazon RDS / Aurora", "engine": "postgres", "hint": "postgres://user:password@your-host.rds.amazonaws.com:5432/dbname. Allow this server's address in the security group."},
    {"id": "cloudsql", "label": "Google Cloud SQL / Azure Database", "engine": "postgres", "hint": "Use the public address of the instance and allow this server's IP."},
    {"id": "planetscale", "label": "PlanetScale / TiDB / any MySQL", "engine": "mysql", "hint": "Connect > the mysql:// address."},
    {"id": "atlas", "label": "MongoDB Atlas", "engine": "mongodb", "hint": "Database > Connect > Drivers > the mongodb+srv:// address. Allow this server's IP in Network Access."},
    {"id": "upstash", "label": "Upstash Redis", "engine": "redis", "hint": "Database > Details > the rediss:// address."},
    {"id": "redis-cloud", "label": "Redis Cloud", "engine": "redis", "hint": "Database > Connect > the redis:// address."},
    {"id": "turso", "label": "Turso (libSQL)", "engine": "libsql", "hint": "libsql://your-db.turso.io?authToken=..."},
    {"id": "firebase", "label": "Firebase (Firestore)", "engine": "firestore", "hint": "Project settings > Service accounts > Generate new private key. Paste the whole JSON file."},
    {"id": "other", "label": "Another provider or my own server", "engine": "", "hint": "Any address that starts with the database's scheme, for example postgres://user:password@host:5432/name."},
]


def options() -> Dict[str, Any]:
    return {"engines": [{"id": k, "label": v["label"], "local": v["local"]} for k, v in ENGINES.items()], "providers": PROVIDERS}


def secret_name(engine: str) -> str:
    return ENGINES[engine]["secret"]


def check_engine(engine: str, mode: str) -> None:
    if engine not in ENGINES:
        raise DataError("Choose a database type from the list.")
    if mode not in (LOCAL, PROVIDER):
        raise DataError("Choose a database on our server or your own provider.")
    if mode == LOCAL and not ENGINES[engine]["local"]:
        raise DataError(f"{ENGINES[engine]['label']} is an online service. Choose 'my own provider' for it, or use the .db file on our server.")


def parse(engine: str, value: str) -> Tuple[str, int]:
    """Validate an address (or Firebase key file) and return (host, port). Raises DataError with a sentence the owner can act on."""
    check_engine(engine, PROVIDER)
    value = (value or "").strip()
    if not value:
        raise DataError("Paste the database address first.")
    if engine == "firestore":
        try:
            key = json.loads(value)
        except ValueError:
            raise DataError("That is not the Firebase key file. Open it in a text editor and paste everything, from the first { to the last }.")
        if not isinstance(key, dict) or key.get("type") != "service_account" or not key.get("project_id") or not key.get("private_key"):
            raise DataError("That JSON is not a service-account key. In Firebase: Project settings > Service accounts > Generate new private key.")
        return "firestore.googleapis.com", 443
    u = urlparse(value)
    want = ENGINES[engine]["schemes"]
    if u.scheme.lower() not in want:
        raise DataError(f"The address for {ENGINES[engine]['label']} should start with {' or '.join(s + '://' for s in want)}.")
    if not u.hostname:
        raise DataError("The address has no host name. It should look like scheme://user:password@host:port/name.")
    try:
        port = u.port or ENGINES[engine]["port"]
    except ValueError:
        raise DataError("The port in the address is not a number.")
    return u.hostname, port


async def reachable(host: str, port: int, seconds: float = 6) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), seconds)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def test(engine: str, value: str) -> Dict[str, Any]:
    """{"ok": bool, "message": str}: well formed, and the host answers. The password is never part of the message."""
    try:
        host, port = parse(engine, value)
    except DataError as e:
        return {"ok": False, "message": str(e)}
    if engine == "firestore":
        return {"ok": True, "message": "The key file looks right. Its permissions are checked when the app starts."}
    if await reachable(host, port):
        return {"ok": True, "message": f"{host} answered on port {port}. The username and password are checked when the app starts."}
    return {"ok": False, "message": f"Could not reach {host} on port {port}. Check the address, and that the provider allows connections from this server's IP address."}


def read_choice(raw: bytes) -> Dict[str, str]:
    """data.json in a project: what the assistant and owner agreed in the chat, {"mode": "local"|"provider", "engine": "postgres", "provider": "supabase"}."""
    try:
        d = json.loads((raw or b"{}").decode("utf-8", "replace")[:2000])
    except ValueError:
        return {}
    if not isinstance(d, dict):
        return {}
    mode = {"online": PROVIDER, "provider": PROVIDER, "local": LOCAL}.get(str(d.get("mode", "")).lower(), "")
    engine = "sqlite" if mode == LOCAL else str(d.get("engine", "")).lower()       # on our server the data is always the .db file
    try:
        check_engine(engine, mode)
    except DataError:
        return {}
    return {"mode": mode, "engine": engine, "provider": str(d.get("provider", ""))[:30]}
