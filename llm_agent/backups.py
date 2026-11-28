"""Daily and on-demand backups of an app's data volumes, kept 30 days.

Every engine is backed up the same way: the app and its database container are stopped for a few seconds, each data volume is packed into a .tgz
(so SQLite files and database folders are consistent), and the containers are started again. Restoring stops them, replaces the volume contents and
starts them again. Docker only. Backups sit on this server (data folder / backups): copy that folder off the server for protection against a lost disk.
"""
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from llm_agent.datastore import DataError

Runner = Callable[..., Awaitable[Tuple[int, str]]]
KEEP_DAYS = 30
HELPER_IMAGE = "python:3.12-slim"          # already on the server: hosted apps are built from it
NAME = re.compile(r"^\d{8}-\d{6}$")


class Backups:
    def __init__(self, root: Path, runner: Runner, clock=time.time):
        self.root, self.run, self.clock = root, runner, clock

    def dir(self, app_id: str) -> Path:
        return self.root / app_id

    async def _exists(self, kind: str, name: str) -> bool:
        return (await self.run(["docker", kind, "inspect", name], 15))[0] == 0

    async def _running(self, containers: List[str]) -> List[str]:
        out = []
        for c in containers:
            rc, text = await self.run(["docker", "inspect", "-f", "{{.State.Status}}", c], 15)
            if rc == 0 and text.strip() == "running":
                out.append(c)
        return out

    async def _paused(self, containers: List[str], work: Callable[[], Awaitable[Any]]) -> Any:
        running = await self._running(containers)
        for c in running:
            await self.run(["docker", "stop", "-t", "10", c], 40)
        try:
            return await work()
        finally:
            for c in reversed(running):                       # the database starts before the app
                await self.run(["docker", "start", c], 30)

    async def create(self, app_id: str, volumes: Dict[str, str], containers: List[str]) -> Dict[str, Any]:
        """volumes: {"data": "wb-data-<id>", "db": "wb-dbvol-<id>"}; containers: the ones to pause while packing (the app first, then its database; they restart in reverse)."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        d = self.dir(app_id)
        d.mkdir(parents=True, exist_ok=True)
        made: List[str] = []

        async def pack() -> None:
            for tag, vol in volumes.items():
                if not await self._exists("volume", vol):
                    continue
                rc, out = await self.run(["docker", "run", "--rm", "-v", f"{vol}:/data:ro", "-v", f"{d.resolve()}:/backup", HELPER_IMAGE,
                                          "tar", "czf", f"/backup/{stamp}-{tag}.tgz", "-C", "/data", "."], 600)
                if rc != 0:
                    raise DataError("The backup could not be made: " + out[-300:])
                made.append(f"{stamp}-{tag}.tgz")
        await self._paused(containers, pack)
        if not made:
            raise DataError("There is no saved data to back up yet. Deploy and use the app first.")
        return {"name": stamp, "files": made}

    def list(self, app_id: str) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for f in sorted(self.dir(app_id).glob("*.tgz")) if self.dir(app_id).is_dir() else []:
            stamp = f.name.split("-", 2)
            key = "-".join(stamp[:2])
            if NAME.match(key):
                g = groups.setdefault(key, {"name": key, "files": [], "bytes": 0, "created": f.stat().st_mtime})
                g["files"].append(f.name)
                g["bytes"] += f.stat().st_size
        return sorted(groups.values(), key=lambda g: g["name"], reverse=True)

    async def restore(self, app_id: str, name: str, volumes: Dict[str, str], containers: List[str]) -> List[str]:
        if not NAME.match(name or ""):
            raise DataError("Choose a backup from the list.")
        d = self.dir(app_id)
        files = {f.name.split("-", 2)[2][:-4]: f for f in d.glob(f"{name}-*.tgz")} if d.is_dir() else {}
        if not files:
            raise DataError("That backup no longer exists.")
        restored: List[str] = []

        async def put() -> None:
            for tag, f in files.items():
                vol = volumes.get(tag)
                if not vol:
                    continue
                rc, out = await self.run(["docker", "run", "--rm", "-v", f"{vol}:/data", "-v", f"{d.resolve()}:/backup:ro", HELPER_IMAGE, "sh", "-c",
                                          f"find /data -mindepth 1 -delete && tar xzf /backup/{f.name} -C /data"], 600)
                if rc != 0:
                    raise DataError("The backup could not be restored: " + out[-300:])
                restored.append(tag)
        await self._paused(containers, put)
        return restored

    def prune(self, app_id: str) -> int:
        cutoff, gone = self.clock() - KEEP_DAYS * 86400, 0
        for f in list(self.dir(app_id).glob("*.tgz")) if self.dir(app_id).is_dir() else []:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                gone += 1
        return gone

    def delete_app(self, app_id: str) -> None:
        shutil.rmtree(self.dir(app_id), ignore_errors=True)
