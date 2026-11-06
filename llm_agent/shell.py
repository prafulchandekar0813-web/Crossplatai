"""A shell for the assistant, in a temporary environment per chat.

Each chat gets its own folder (home, tmp, logs, python venv) under data/shell_envs/. Commands start in the user's project folder, so the files
the assistant writes are the files it runs. Apps started in the background are tracked per chat, shown through the preview proxy, and stopped
when the chat is deleted, on a timer, or when the server stops. The folder is thrown away with the chat and on every server restart.

Isolation, strongest first:
  bubblewrap  (needs `bwrap` and user namespaces)  the command sees only /usr, its project, its chat home: not the server's files or secrets
  basic       plain process: scrubbed environment, resource limits, own process group. It CAN read anything the server user can read, so it is
              refused when the server runs as root, and otherwise only when an admin ticks "allow without isolation" (trusted users only)
On top of both, shell_guard.py refuses dangerous commands. Read its docstring: that list is a safety net, not a sandbox.
"""
import asyncio
import logging
import os
import random
import resource
import secrets
import shutil
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_agent.security import valid_identifier
from llm_agent import connections
from llm_agent.shell_guard import Blocked, Guard

log = logging.getLogger("shell")
MAX_OUTPUT = 20000
PORT_RANGE = (20000, 29999)
APP_ID = __import__("re").compile(r"^[0-9a-f]{12}$")
ENV_IDLE_SECONDS = 6 * 3600


class ShellError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass
class App:
    id: str
    user_id: str
    session_id: str
    port: int
    command: str
    cwd: str
    started: float
    expires: float
    proc: Any = None


@dataclass
class Env:
    user_id: str
    session_id: str
    dir: Path
    last_used: float
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    app: Optional[App] = None

    @property
    def home(self) -> Path:
        return self.dir / "home"

    @property
    def tmp(self) -> Path:
        return self.dir / "tmp"

    @property
    def logs(self) -> Path:
        return self.dir / "logs"


def _wait_port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _tail(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 5
    return text[:head] + f"\n... ({len(text) - limit} characters left out) ...\n" + text[-(limit - head):]


class ShellManager:
    def __init__(self, settings, data_dir: Path, clock: Callable[[], float] = time.time):
        self.settings, self.root, self.clock = settings, data_dir / "shell_envs", clock
        self.envs: Dict[Tuple[str, str], Env] = {}
        self.apps: Dict[str, App] = {}
        self.is_root = hasattr(os, "geteuid") and os.geteuid() == 0     # overridable in tests
        self._bwrap: Optional[bool] = None
        self.on_blocked = None                    # async (user_id, command) called when the guard refuses a command

    @property
    def cfg(self):
        return self.settings.effective

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.shell_enabled)

    # ---- availability ----------------------------------------------------------------------------
    async def _has_bwrap(self) -> bool:
        if self._bwrap is None:
            self._bwrap = False
            if shutil.which("bwrap"):
                try:
                    p = await asyncio.create_subprocess_exec("bwrap", "--unshare-user", "--ro-bind", "/", "/", "true",
                                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    self._bwrap = (await asyncio.wait_for(p.wait(), 10)) == 0
                except (OSError, asyncio.TimeoutError):
                    self._bwrap = False
        return self._bwrap

    async def status(self) -> Dict[str, Any]:
        """What the admin screen and the assistant need to know."""
        if not self.enabled:
            return {"enabled": False, "available": False, "isolation": None, "reason": "Switched off. An admin can enable it under Admin > Apps."}
        if await self._has_bwrap():
            return {"enabled": True, "available": True, "isolation": "bubblewrap", "reason": ""}
        if self.is_root:
            return {"enabled": True, "available": False, "isolation": None,
                    "reason": "The server runs as root and bubblewrap (bwrap) is not available, so commands can't be run safely. "
                              "Run the server as a normal user, or install bubblewrap."}
        if not self.cfg.shell_allow_unisolated:
            return {"enabled": True, "available": False, "isolation": None,
                    "reason": "Bubblewrap (bwrap) is not installed. Install it (recommended), or an admin can tick “allow without isolation” for trusted users only."}
        return {"enabled": True, "available": True, "isolation": "basic", "reason": ""}

    async def require(self) -> str:
        st = await self.status()
        if not st["available"]:
            raise ShellError(st["reason"], 403)
        return st["isolation"]

    # ---- environments ----------------------------------------------------------------------------
    def env(self, user_id: str, session_id: str) -> Env:
        if not (valid_identifier(user_id) and valid_identifier(session_id)):
            raise ShellError("Invalid chat.")
        key = (user_id, session_id)
        env = self.envs.get(key)
        if env is None:
            d = self.root / user_id / session_id
            for sub in ("home", "tmp", "logs", "home/bin", "home/data"):
                (d / sub).mkdir(parents=True, exist_ok=True)
            for name, body in (("python", 'exec python3 "$@"'), ("pip", 'exec python3 -m pip "$@"')):
                f = d / "home" / "bin" / name
                if not f.exists():
                    f.write_text(f"#!/bin/sh\n{body}\n")
                    f.chmod(0o755)
            env = self.envs[key] = Env(user_id, session_id, d, self.clock())
        env.last_used = self.clock()
        return env

    def _vars(self, env: Env, bwrap: bool, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        home, tmp = ("/home/app", "/tmp") if bwrap else (str(env.home), str(env.tmp))
        v = {"HOME": home, "TMPDIR": tmp, "DATA_DIR": f"{home}/data", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
             "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CACHE_DIR": f"{home}/.cache/pip", "HOST": "127.0.0.1", "TERM": "dumb",
             "PATH": f"{home}/venv/bin:{home}/bin:/usr/local/bin:/usr/bin:/bin"}
        v.update(extra or {})
        return v

    def _limits(self, seconds: int):
        mem = self.cfg.shell_memory_mb * 1024 * 1024

        def apply():
            os.setsid()
            for res, val in ((resource.RLIMIT_CPU, seconds + 5), (resource.RLIMIT_FSIZE, 200 * 1024 * 1024), (resource.RLIMIT_NPROC, 512),
                             (resource.RLIMIT_NOFILE, 512), (resource.RLIMIT_CORE, 0)):
                try:
                    resource.setrlimit(res, (val, val))
                except (ValueError, OSError):
                    pass
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem * 4, mem * 4))   # address space, generous: the real cap is bubblewrap/cgroups
            except (ValueError, OSError):
                pass
        return apply

    def _bwrap_argv(self, env: Env, workspace: Path, cwd_rel: str, variables: Dict[str, str], command: str) -> List[str]:
        a = ["bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try", "--die-with-parent", "--new-session"]
        if not self.cfg.shell_network:
            a.append("--unshare-net")
        for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32"):
            if os.path.islink(d):
                a += ["--symlink", os.readlink(d), d]
            elif os.path.isdir(d):
                a += ["--ro-bind", d, d]
        for f in ("/etc/resolv.conf", "/etc/ssl", "/etc/ca-certificates", "/etc/alternatives", "/etc/mime.types", "/etc/localtime", "/etc/ld.so.cache", "/etc/hosts"):
            if os.path.exists(f):
                a += ["--ro-bind", f, f]
        a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(workspace), "/work", "--bind", str(env.home), "/home/app",
              "--chdir", "/work" + (("/" + cwd_rel) if cwd_rel else ""), "--clearenv"]
        for k, val in variables.items():
            a += ["--setenv", k, val]
        return a + ["bash", "-c", command]

    def _plan(self, isolation: str, env: Env, files, cwd: Path, command: str, extra: Optional[Dict[str, str]], seconds: int):
        """(argv, spawn kwargs, guard) for one command."""
        bwrap = isolation == "bubblewrap"
        rel = files.rel(cwd)
        if bwrap:
            guard = Guard([Path("/work")], Path("/home/app"), Path("/tmp"))
            guard_cwd = Path("/work") / rel
            argv = self._bwrap_argv(env, files.root, rel, self._vars(env, True, extra), command)
            kwargs: Dict[str, Any] = {"cwd": None, "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, "start_new_session": True}
        else:
            guard = Guard([files.root], env.home, env.tmp)
            guard_cwd = cwd
            argv = ["bash", "-c", command]
            kwargs = {"cwd": str(cwd), "env": self._vars(env, False, extra), "preexec_fn": self._limits(seconds)}
        try:
            guard.check(command, guard_cwd)
        except Blocked as e:
            raise ShellError(f"Blocked: {e}")
        return argv, kwargs

    # ---- run a command ---------------------------------------------------------------------------
    async def run(self, user_id: str, session_id: str, files, cwd_rel: str, command: str, timeout: int = 60) -> Dict[str, Any]:
        isolation = await self.require()
        env = self.env(user_id, session_id)
        try:
            cwd = files.resolve(cwd_rel or "", allow_root=True)
        except Exception as e:
            raise ShellError(str(e))
        if not cwd.is_dir():
            raise ShellError("That working folder does not exist.")
        timeout = max(1, min(int(timeout or 60), self.cfg.shell_timeout_seconds))
        try:
            argv, kw = self._plan(isolation, env, files, cwd, command, None, timeout)
        except ShellError as e:
            if self.on_blocked and str(e).startswith("Blocked:"):
                await self.on_blocked(user_id, command)
            raise
        async with env.sem:
            started = self.clock()
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, **kw)
            chunks: List[bytes] = []
            size = 0

            async def pump():
                nonlocal size
                while True:
                    data = await proc.stdout.read(65536)
                    if not data:
                        return
                    if size < 1_000_000:
                        chunks.append(data)
                        size += len(data)

            timed_out = False
            try:
                await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
            except asyncio.TimeoutError:
                timed_out = True
                self._kill(proc)
                await proc.wait()
        env.last_used = self.clock()
        out = b"".join(chunks).decode("utf-8", "replace")
        result = {"exit_code": proc.returncode, "output": _tail(out), "seconds": round(self.clock() - started, 1), "timed_out": timed_out}
        if timed_out:
            result["note"] = (f"Stopped after {timeout}s. A server or anything that waits forever must be started with the background option "
                              "(it then runs until you stop it).")
        quota = self.cfg.workspace_quota_mb * 1024 * 1024
        if await asyncio.to_thread(files.usage_bytes) > quota:
            result["warning"] = "Your project folder is over its storage limit. Delete files you don't need (for example node_modules or a venv inside the project)."
        return result

    @staticmethod
    def _kill(proc, sig: int = signal.SIGKILL) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # ---- a server in the background, shown in the preview ---------------------------------------
    def _free_port(self) -> int:
        used = {a.port for a in self.apps.values()}
        for _ in range(200):
            port = random.randint(*PORT_RANGE)
            if port in used:
                continue
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise ShellError("No free port for the app. Try again.", 503)

    async def start_app(self, user_id: str, session_id: str, files, cwd_rel: str, command: str) -> Dict[str, Any]:
        isolation = await self.require()
        env = self.env(user_id, session_id)
        try:
            cwd = files.resolve(cwd_rel or "", allow_root=True)
        except Exception as e:
            raise ShellError(str(e))
        if not cwd.is_dir():
            raise ShellError("That working folder does not exist.")
        await self.reap()
        if env.app:
            await self.stop_app(env.app.id)
        if len(self.apps) >= self.cfg.shell_max_apps:
            raise ShellError("The server is already running as many apps as it allows. Try again in a few minutes.", 503)
        port = self._free_port()
        try:
            argv, kw = self._plan(isolation, env, files, cwd, command, {**connections.load(env.dir / "connections.json"), "PORT": str(port)}, 24 * 3600)
        except ShellError as e:
            if self.on_blocked and str(e).startswith("Blocked:"):
                await self.on_blocked(user_id, command)
            raise
        app_id = secrets.token_hex(6)
        log_path = env.logs / f"{app_id}.log"
        with open(log_path, "wb") as logf:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=logf, stderr=asyncio.subprocess.STDOUT, **kw)
        app = App(app_id, user_id, session_id, port, command, files.rel(cwd), self.clock(), self.clock() + self.cfg.shell_minutes * 60, proc)
        self.apps[app_id], env.app = app, app
        end = self.clock() + 25
        while self.clock() < end:
            if proc.returncode is not None:
                text = self.logs(app_id)
                await self.stop_app(app_id)
                raise ShellError("The app stopped right after starting.\n" + text[-1500:], 422)
            if await asyncio.to_thread(_wait_port, port):
                return self.public(app)
            await asyncio.sleep(0.4)
        text = self.logs(app_id)
        await self.stop_app(app_id)
        raise ShellError(f"The app did not start listening on port {port} within 25 seconds. It must listen on the PORT environment variable "
                         "(host 0.0.0.0 or 127.0.0.1), for example app.run(host='0.0.0.0', port=int(os.environ['PORT'])).\n" + text[-1500:], 422)

    def public(self, app: App) -> Dict[str, Any]:
        return {"id": app.id, "path": app.cwd, "start": app.command, "port": app.port, "seconds_left": max(0, int(app.expires - self.clock()))}

    def get(self, app_id: str) -> Optional[App]:
        app = self.apps.get(app_id) if APP_ID.match(app_id or "") else None
        return app if app and app.expires > self.clock() and app.proc.returncode is None else None

    def connections(self, user_id: str, session_id: str) -> Dict[str, str]:
        return connections.load(self.env(user_id, session_id).dir / "connections.json")

    def set_connections(self, user_id: str, session_id: str, incoming: Dict[str, str]) -> Dict[str, str]:
        path = self.env(user_id, session_id).dir / "connections.json"
        merged = connections.merge(connections.load(path), incoming)
        connections.save(path, merged)
        return merged

    def current(self, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        env = self.envs.get((user_id, session_id))
        app = env.app if env else None
        return self.public(app) if app and self.get(app.id) else None

    def logs(self, app_id: str, tail: int = 8000) -> str:
        app = self.apps.get(app_id)
        env = self.envs.get((app.user_id, app.session_id)) if app else None
        if not env:
            return ""
        try:
            return (env.logs / f"{app_id}.log").read_text(errors="replace")[-tail:]
        except OSError:
            return ""

    async def probe(self, app_id: str, path: str = "/") -> Dict[str, Any]:
        import httpx
        app = self.get(app_id)
        if not app:
            return {"status": None, "text": ""}
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                r = await c.get(f"http://127.0.0.1:{app.port}{path}")
            return {"status": r.status_code, "text": r.text[:2000]}
        except httpx.HTTPError as e:
            return {"status": None, "text": f"No answer: {type(e).__name__}"}

    async def stop_app(self, app_id: str) -> None:
        app = self.apps.pop(app_id, None)
        if not app:
            return
        env = self.envs.get((app.user_id, app.session_id))
        if env and env.app is app:
            env.app = None
        if app.proc and app.proc.returncode is None:
            self._kill(app.proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(app.proc.wait(), 3)
            except asyncio.TimeoutError:
                self._kill(app.proc)
                await app.proc.wait()

    # ---- housekeeping ----------------------------------------------------------------------------
    async def stop_chat(self, user_id: str, session_id: str, remove: bool = True) -> None:
        env = self.envs.pop((user_id, session_id), None)
        if env and env.app:
            await self.stop_app(env.app.id)
        if remove:
            shutil.rmtree(self.root / user_id / session_id, ignore_errors=True) if valid_identifier(user_id) and valid_identifier(session_id) else None

    async def stop_user(self, user_id: str) -> None:
        for (u, s) in [k for k in self.envs if k[0] == user_id]:
            await self.stop_chat(u, s)
        if valid_identifier(user_id):
            shutil.rmtree(self.root / user_id, ignore_errors=True)

    async def reap(self) -> None:
        now = self.clock()
        for app in [a for a in self.apps.values() if a.expires <= now or (a.proc and a.proc.returncode is not None)]:
            await self.stop_app(app.id)
        for key, env in [(k, e) for k, e in self.envs.items() if not e.app and now - e.last_used > ENV_IDLE_SECONDS]:
            await self.stop_chat(*key)

    async def cleanup_orphans(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)   # nothing survives a restart: environments are temporary

    async def stop_all(self) -> None:
        for app_id in list(self.apps):
            await self.stop_app(app_id)
        shutil.rmtree(self.root, ignore_errors=True)
