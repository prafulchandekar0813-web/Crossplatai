"""Runs each managed app's backend for real, one container per app.

DockerRuntime is the production runtime. ProcessRuntime exists only so the whole flow can be tried on a host without Docker
(for example a small test server): it has NO isolation and refuses to run as root unless HOSTING_ALLOW_ROOT_PROCESS=1 is set.
Owners' keys are decrypted only here, at the moment an app starts, and are handed to the app as environment variables.
"""
import asyncio
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from llm_agent import runspec

INNER_PORT = 8000
READY_SECONDS = 40
BASE_IMAGE = "python:3.12-slim"
UID = 10001


class HostingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


Runner = Callable[..., Awaitable[Tuple[int, str]]]


async def run_cmd(argv: List[str], timeout: float = 120, input: Optional[bytes] = None) -> Tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if input else asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(input), timeout)
        return p.returncode or 0, out.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed"
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timed out"


def scrub(text: str, values) -> str:
    """Replace any owner key that appears in text, longest first, so logs never reveal one."""
    for v in sorted({v for v in values if v and len(v) >= 4}, key=len, reverse=True):
        text = text.replace(v, "***")
    return text


def clean_requirements(text: str) -> str:
    """Only plain package lines: no pip options (--index-url, -e, -r, ...) in an owner's requirements.txt."""
    return "\n".join(l for l in text.splitlines() if l.strip() and not l.strip().startswith("-")) + "\n"


def dockerfile(start: str, has_reqs: bool) -> str:
    lines = [f"FROM {BASE_IMAGE}", f"RUN useradd -m -u {UID} app && mkdir /data && chown {UID} /data", "WORKDIR /app",
             "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1"]
    if has_reqs:
        lines += ["COPY requirements.txt .", "RUN pip install -r requirements.txt"]
    lines += ["COPY . .", f"USER {UID}", f"ENV PORT={INNER_PORT} DATA_DIR=/data", f"CMD {json.dumps(['sh', '-c', start])}"]
    return "\n".join(lines) + "\n"


class Limits:
    def __init__(self, memory_mb: int, cpus: float, network: str = ""):
        self.memory_mb, self.cpus, self.network = memory_mb, cpus, network


# ---------------------------------------------------------------------------------------------------- docker
class DockerRuntime:
    name = "docker"

    def __init__(self, root: Path, runner: Runner = run_cmd):
        self.root, self.run = root, runner

    @staticmethod
    def cname(app_id: str) -> str:
        return f"wb-app-{app_id}"

    async def available(self) -> Tuple[bool, str]:
        rc, out = await self.run(["docker", "info", "--format", "{{.ServerVersion}}"], 15)
        return (True, "") if rc == 0 else (False, "Docker is not running on this server, so apps cannot be hosted.")

    async def deploy(self, app_id: str, files: Dict[str, bytes], start: str, rev: int) -> str:
        src = self.root / app_id / "src"
        shutil.rmtree(src, ignore_errors=True)
        for path, body in files.items():
            target = src / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(clean_requirements(body.decode("utf-8", "replace")).encode() if path == "requirements.txt" else body)
        (src / "Dockerfile").write_text(dockerfile(start, "requirements.txt" in files))
        (src / ".dockerignore").write_text(".env*\n*.pem\n*.key\n")
        image = f"wb-app-{app_id}:{rev}"
        rc, out = await self.run(["docker", "build", "-t", image, "--label", f"wb.app={app_id}", str(src)], 900)
        shutil.rmtree(src, ignore_errors=True)
        if rc != 0:
            raise HostingError("The app could not be built: " + out[-600:])
        return image

    async def start(self, app_id: str, image: str, port: int, env: Dict[str, str], limits: Limits) -> None:
        await self.stop(app_id, remove=True)
        d = self.root / app_id
        d.mkdir(parents=True, exist_ok=True)
        fd, envfile = tempfile.mkstemp(dir=d, prefix="env-")             # 0600; deleted as soon as docker has read it
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(f"{k}={v}\n" for k, v in env.items() if "\n" not in v and "\r" not in v))
            argv = ["docker", "run", "-d", "--name", self.cname(app_id), "--label", f"wb.app={app_id}",
                    "--read-only", "--tmpfs", "/tmp:rw,size=64m,nosuid", "-v", f"wb-data-{app_id}:/data",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", str(UID),
                    "--pids-limit", "128", "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m", "--cpus", str(limits.cpus),
                    "--restart", "on-failure:3", "-p", f"127.0.0.1:{port}:{INNER_PORT}", "--env-file", envfile]
            if limits.network:
                argv += ["--network", limits.network]
            rc, out = await self.run(argv + [image], 60)
        finally:
            try:
                os.unlink(envfile)
            except OSError:
                pass
        if rc != 0:
            raise HostingError("The app could not be started: " + out[-400:])

    async def stop(self, app_id: str, remove: bool = False) -> None:
        await self.run(["docker", "stop", "-t", "5", self.cname(app_id)], 30)
        if remove:
            await self.run(["docker", "rm", "-f", self.cname(app_id)], 30)

    async def state(self, app_id: str) -> str:
        rc, out = await self.run(["docker", "inspect", "-f", "{{.State.Status}}", self.cname(app_id)], 15)
        return "missing" if rc != 0 else ("running" if out.strip() == "running" else "stopped")

    async def logs(self, app_id: str, lines: int = 200) -> str:
        rc, out = await self.run(["docker", "logs", "--tail", str(lines), self.cname(app_id)], 20)
        return out

    async def remove(self, app_id: str) -> None:
        await self.stop(app_id, remove=True)
        await self.run(["docker", "volume", "rm", "-f", f"wb-data-{app_id}"], 30)
        rc, out = await self.run(["docker", "images", "-q", "--filter", f"label=wb.app={app_id}"], 15)
        for img in out.split():
            await self.run(["docker", "rmi", "-f", img], 30)
        shutil.rmtree(self.root / app_id, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------- process (testing only)
class ProcessRuntime:
    name = "process"

    def __init__(self, root: Path):
        self.root, self.procs = root, {}

    async def available(self) -> Tuple[bool, str]:
        if hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("HOSTING_ALLOW_ROOT_PROCESS") != "1":
            return False, "Process hosting is for testing and will not run as root. Use Docker, or set HOSTING_ALLOW_ROOT_PROCESS=1 on a throw-away test server."
        return True, ""

    async def deploy(self, app_id: str, files: Dict[str, bytes], start: str, rev: int) -> str:
        base = self.root / app_id
        await self.stop(app_id)
        shutil.rmtree(base / "src", ignore_errors=True)
        for path, body in files.items():
            target = base / "src" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(clean_requirements(body.decode("utf-8", "replace")).encode() if path == "requirements.txt" else body)
        (base / "data").mkdir(exist_ok=True)
        (base / "start").write_text(start)
        if "requirements.txt" in files:
            venv = base / "venv"
            rc, out = await run_cmd([sys.executable, "-m", "venv", str(venv)], 120)
            if rc == 0:
                rc, out = await run_cmd([str(venv / "bin" / "pip"), "install", "-q", "-r", str(base / "src" / "requirements.txt")], 600)
            if rc != 0:
                raise HostingError("The app's packages could not be installed: " + out[-600:])
        return f"process:{rev}"

    async def start(self, app_id: str, image: str, port: int, env: Dict[str, str], limits: Limits) -> None:
        await self.stop(app_id)
        base = self.root / app_id
        venv_bin = base / "venv" / "bin"
        path = f"{venv_bin}:{os.defpath}" if venv_bin.is_dir() else os.defpath
        full = {"PATH": path, "HOME": str(base / "data"), "LANG": "C.UTF-8", **env, "PORT": str(port), "DATA_DIR": str(base / "data"), "PYTHONUNBUFFERED": "1"}
        log = open(base / "app.log", "ab")
        self.procs[app_id] = await asyncio.create_subprocess_exec("sh", "-c", (base / "start").read_text(), cwd=base / "src", env=full,
                                                                  stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        log.close()

    async def stop(self, app_id: str, remove: bool = False) -> None:
        p = self.procs.pop(app_id, None)
        if p and p.returncode is None:
            try:
                os.killpg(p.pid, 15)
                await asyncio.wait_for(p.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(p.pid, 9)
                except ProcessLookupError:
                    pass

    async def state(self, app_id: str) -> str:
        p = self.procs.get(app_id)
        return "missing" if p is None else ("running" if p.returncode is None else "stopped")

    async def logs(self, app_id: str, lines: int = 200) -> str:
        try:
            return "\n".join((self.root / app_id / "app.log").read_text("utf-8", "replace").splitlines()[-lines:])
        except OSError:
            return ""

    async def remove(self, app_id: str) -> None:
        await self.stop(app_id)
        shutil.rmtree(self.root / app_id, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------- manager
def free_port(taken=()) -> int:
    for _ in range(50):
        p = random.randint(30000, 39999)
        if p in taken:
            continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise HostingError("No free port on this server.", 503)


async def wait_ready(port: int, seconds: float = READY_SECONDS, alive: Optional[Callable[[], Awaitable[bool]]] = None) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            if alive and not await alive():
                return False
            await asyncio.sleep(0.4)
    return False


class HostingManager:
    def __init__(self, db, app_secrets, settings, root: Path, runtime: Optional[Any] = None, clock=time.time):
        self.db, self.secrets, self.settings, self.root, self.clock = db, app_secrets, settings, root, clock
        self._runtime, self._forced = runtime, runtime is not None
        self.locks: Dict[str, asyncio.Lock] = {}
        self.touched: Dict[str, float] = {}

    @property
    def cfg(self):
        return self.settings.effective

    def runtime(self):
        if self._forced:
            return self._runtime
        kind = self.cfg.hosting_runtime
        if kind == "off":
            return None
        if self._runtime is None or self._runtime.name != kind:
            self._runtime = DockerRuntime(self.root) if kind == "docker" else ProcessRuntime(self.root)
        return self._runtime

    def limits(self) -> Limits:
        return Limits(self.cfg.hosting_memory_mb, self.cfg.hosting_cpus, self.cfg.hosting_network)

    async def available(self) -> Tuple[bool, str]:
        rt = self.runtime()
        if rt is None:
            return False, "Hosting apps is switched off on this server. The administrator can turn it on under Admin > Limits."
        return await rt.available()

    def lock(self, app_id: str) -> asyncio.Lock:
        return self.locks.setdefault(app_id, asyncio.Lock())

    async def row(self, app_id: str) -> Optional[Dict[str, Any]]:
        r = await self.db.fetchone("SELECT * FROM hosted_apps WHERE app_id=?", (app_id,))
        return dict(r) if r else None

    async def info(self, app_id: str) -> Dict[str, Any]:
        ok, reason = await self.available()
        row = await self.row(app_id)
        state = "not deployed"
        if row and row["image"]:
            rt = self.runtime()
            live = await rt.state(app_id) if rt else "missing"
            state = "running" if live == "running" else {"sleeping": "sleeping", "stopped": "stopped"}.get(row["desired"], "stopped")
            if row["last_error"] and live != "running" and row["desired"] == "running":
                state = "failed"
        return {"available": ok, "reason": reason, "state": state, "data_mode": (row or {}).get("data_mode", "local"),
                "deployed_at": (row or {}).get("deployed_at", 0), "last_started": (row or {}).get("last_started", 0), "error": (row or {}).get("last_error", ""),
                "vault": self.secrets.vault.available(), "vault_problem": self.secrets.vault.problem(), "isolation": (self.runtime().name if self.runtime() else None),
                "memory_mb": self.cfg.hosting_memory_mb, "idle_minutes": self.cfg.hosting_idle_minutes}

    async def set_data_mode(self, app_id: str, mode: str) -> None:
        if mode not in ("local", "provider"):
            raise HostingError("Choose a local database or your own provider.")
        await self.db.execute("INSERT INTO hosted_apps(app_id, data_mode) VALUES (?,?) ON CONFLICT(app_id) DO UPDATE SET data_mode=excluded.data_mode", (app_id, mode))

    async def deploy(self, app_id: str, files: Dict[str, bytes]) -> None:
        ok, reason = await self.available()
        if not ok:
            raise HostingError(reason, 503)
        try:
            start = runspec.parse_start(files.get("run.json", b"").decode("utf-8", "replace") or None, files.keys())
        except ValueError as e:
            raise HostingError(str(e))
        async with self.lock(app_id):
            row = await self.row(app_id)
            rev = int(self.clock())
            image = await self.runtime().deploy(app_id, files, start, rev)
            await self.db.execute("INSERT INTO hosted_apps(app_id, image, deployed_at, last_error) VALUES (?,?,?, '') "
                                  "ON CONFLICT(app_id) DO UPDATE SET image=excluded.image, deployed_at=excluded.deployed_at, last_error=''", (app_id, image, self.clock()))
            old = (row or {}).get("image")
            await self._start_locked(app_id)
            if old and old != image and self.runtime().name == "docker":
                await run_cmd(["docker", "rmi", old], 30)

    async def _running_count(self) -> int:
        rt = self.runtime()
        rows = await self.db.fetchall("SELECT app_id FROM hosted_apps WHERE desired='running'")
        return sum([1 for r in rows if rt and await rt.state(r["app_id"]) == "running"])

    async def _start_locked(self, app_id: str) -> None:
        rt, row = self.runtime(), await self.row(app_id)
        if rt is None or not row or not row["image"]:
            raise HostingError("Deploy the app first.", 409)
        if await rt.state(app_id) != "running" and await self._running_count() >= self.cfg.hosting_max_running:
            raise HostingError("This server is running the most apps it allows. Try again later.", 503)
        taken = {r["host_port"] for r in await self.db.fetchall("SELECT host_port FROM hosted_apps")}
        port = free_port(taken)
        env = await self.secrets.plain(app_id)
        await rt.start(app_id, row["image"], port, env, self.limits())
        alive = (lambda: self._alive(app_id))
        if not await wait_ready(port, alive=alive):
            logs = scrub(await rt.logs(app_id, 30), env.values())
            await rt.stop(app_id)
            await self.db.execute("UPDATE hosted_apps SET desired='running', last_error=? WHERE app_id=?", ("The app did not start. " + logs[-500:], app_id))
            raise HostingError("The app did not start. It must listen on 0.0.0.0 at the PORT environment variable. Last lines of its log: " + (logs.strip()[-400:] or "(empty)"), 502)
        await self.db.execute("UPDATE hosted_apps SET desired='running', host_port=?, last_started=?, last_used=?, last_error='' WHERE app_id=?",
                              (port, self.clock(), self.clock(), app_id))

    async def _alive(self, app_id: str) -> bool:
        return await self.runtime().state(app_id) == "running"

    async def start(self, app_id: str) -> None:
        ok, reason = await self.available()
        if not ok:
            raise HostingError(reason, 503)
        async with self.lock(app_id):
            await self._start_locked(app_id)

    async def stop(self, app_id: str, sleeping: bool = False) -> None:
        """sleeping=True: stopped to save memory, wakes on the next visit. Otherwise stopped on purpose and stays stopped."""
        rt = self.runtime()
        async with self.lock(app_id):
            if rt:
                await rt.stop(app_id)
            await self.db.execute("UPDATE hosted_apps SET desired=? WHERE app_id=?", ("sleeping" if sleeping else "stopped", app_id))

    async def restart(self, app_id: str) -> None:
        await self.start(app_id)

    async def endpoint(self, app_id: str) -> Optional[int]:
        """Port to proxy a visitor's request to, waking a sleeping app first. None when the app is not hosted."""
        row = await self.row(app_id)
        if not row or not row["image"]:
            return None
        rt = self.runtime()
        if rt is None:
            return None
        now = self.clock()
        if self.touched.get(app_id, 0) + 60 < now:
            self.touched[app_id] = now
            await self.db.execute("UPDATE hosted_apps SET last_used=? WHERE app_id=?", (now, app_id))
        if row["desired"] == "stopped":                                    # the owner stopped it on purpose (idle apps are "sleeping")
            return None
        if row["desired"] == "running" and row["host_port"] and await rt.state(app_id) == "running":
            return row["host_port"]
        await self.start(app_id)
        return (await self.row(app_id))["host_port"]

    async def logs(self, app_id: str, lines: int = 200) -> str:
        rt = self.runtime()
        if rt is None:
            return ""
        values = (await self.secrets.plain(app_id)).values()
        return scrub(await rt.logs(app_id, lines), values)

    async def reap(self) -> int:
        """Stop apps nobody has used for hosting_idle_minutes."""
        minutes = self.cfg.hosting_idle_minutes
        if not minutes or not self.runtime():
            return 0
        rows = await self.db.fetchall("SELECT app_id FROM hosted_apps WHERE desired='running' AND last_used<?", (self.clock() - minutes * 60,))
        for r in rows:
            await self.stop(r["app_id"], sleeping=True)
        return len(rows)

    async def reconcile(self) -> None:
        """After a server restart: start again whatever should be running and is not."""
        rt = self.runtime()
        if rt is None or not (await rt.available())[0]:
            return
        for r in await self.db.fetchall("SELECT app_id FROM hosted_apps WHERE desired='running' AND image<>''"):
            if await rt.state(r["app_id"]) != "running":
                try:
                    await self.start(r["app_id"])
                except HostingError:
                    pass

    async def remove(self, app_id: str) -> None:
        rt = self.runtime()
        async with self.lock(app_id):
            if rt:
                await rt.remove(app_id)
            await self.db.execute("DELETE FROM hosted_apps WHERE app_id=?", (app_id,))
        await self.secrets.delete_app(app_id)

    async def stop_all(self) -> None:
        for r in await self.db.fetchall("SELECT app_id FROM hosted_apps WHERE desired='running'"):
            rt = self.runtime()
            if rt and rt.name == "process":
                await rt.stop(r["app_id"])
