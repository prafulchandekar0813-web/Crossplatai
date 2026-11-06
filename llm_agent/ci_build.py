"""Branded app builds through GitHub Actions.

The server never compiles anything. For one app it starts the `build-shell.yml` workflow in the admin's build repository
(see llm_agent/shell_template/README.md), follows the run, and when GitHub has finished it downloads the APK / EXE and keeps
them for the owner to download from Manage app. The GitHub token comes from the environment (BUILD_GITHUB_TOKEN), never from
config.json. Every value sent to the workflow is validated here first; the workflow validates it again."""
import io
import json
import os
import re
import secrets
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

API = "https://api.github.com"
PACKAGE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,4}$")
NAME_BAD = re.compile(r"[^A-Za-z0-9 ._-]")
KINDS = {"apk": ".apk", "exe": ".zip"}       # file extension of what the owner downloads
MAX_DOWNLOAD = 400 * 1024 * 1024
START_TIMEOUT, RUN_TIMEOUT = 15 * 60, 2 * 3600


class BuildError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class CIBuilder:
    def __init__(self, db, settings, root: Path, client: Optional[httpx.AsyncClient] = None, clock=time.time):
        self.db, self.settings, self.root, self.clock = db, settings, root, clock
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=True)

    @property
    def cfg(self):
        return self.settings.effective

    def token(self) -> str:
        return os.environ.get("BUILD_GITHUB_TOKEN", "").strip()

    def status(self) -> Dict[str, Any]:
        if not self.cfg.build_github_repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", self.cfg.build_github_repo):
            return {"available": False, "reason": "App building is not set up on this server yet. The administrator needs to add the build repository."}
        if not self.token():
            return {"available": False, "reason": "App building is not set up on this server yet (no build token)."}
        return {"available": True, "reason": ""}

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "llm-agent-workbench"}

    # ---- request a build ------------------------------------------------------------------------
    @staticmethod
    def clean_name(name: str) -> str:
        return " ".join(NAME_BAD.sub("", name or "").split())[:30] or "App"

    async def request(self, app_row: Dict[str, Any], api_base: str, platforms: List[str], package_id: str = "") -> Dict[str, Any]:
        st = self.status()
        if not st["available"]:
            raise BuildError(st["reason"], 503)
        platforms = sorted({p for p in platforms if p in KINDS})
        if not platforms:
            raise BuildError("Choose Android, Windows or both.")
        if not api_base.startswith("https://"):
            raise BuildError("The built app needs this server's https address. Ask the administrator to set the public address (https) first.")
        app_id = app_row["app_id"]
        package_id = (package_id or f"com.appworkbench.a{app_id}").strip().lower()
        if not PACKAGE.match(package_id) or len(package_id) > 80:
            raise BuildError("The package ID must look like com.yourshop.app (lower-case letters, digits, underscores, at least three parts).")
        busy = await self.db.fetchone("SELECT 1 FROM app_builds WHERE app_id=? AND status IN ('queued','building')", (app_id,))
        if busy:
            raise BuildError("A build for this app is already running. Wait for it to finish.", 409)
        today = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_builds WHERE app_id=? AND created_at>?", (app_id, self.clock() - 86400))
        if today["n"] >= self.cfg.builds_per_app_per_day:
            raise BuildError(f"At most {self.cfg.builds_per_app_per_day} builds per app per day. Try again tomorrow.", 429)
        meta = json.loads(app_row.get("meta") or "{}")
        accent = (meta.get("settings") or {}).get("accent") or "#2563eb"
        request_id = secrets.token_hex(6)
        now = self.clock()
        build_id = await self.db.insert(
            "INSERT INTO app_builds (app_id, request_id, platforms, package_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (app_id, request_id, ",".join(platforms), package_id, "queued", now, now))
        inputs = {"app_id": app_id, "api_base": api_base.rstrip("/"), "app_name": self.clean_name(app_row["name"]), "package_id": package_id,
                  "accent": accent, "platforms": ",".join(platforms), "request_id": request_id}
        try:
            r = await self.client.post(f"{API}/repos/{self.cfg.build_github_repo}/actions/workflows/{self.cfg.build_workflow}/dispatches",
                                       headers=self._headers(), json={"ref": self.cfg.build_ref, "inputs": inputs})
            if r.status_code != 204:
                raise BuildError(f"GitHub refused to start the build ({r.status_code}). Check the repository, workflow name and token.", 502)
        except httpx.HTTPError:
            await self._finish(build_id, "failed", "Could not reach GitHub.")
            raise BuildError("Could not reach GitHub.", 502)
        except BuildError as e:
            await self._finish(build_id, "failed", str(e))
            raise
        return await self.get(build_id)

    # ---- follow it ------------------------------------------------------------------------------
    async def _finish(self, build_id: int, status: str, error: str = "", files: Optional[List[str]] = None, run_url: str = "") -> None:
        await self.db.execute("UPDATE app_builds SET status=?, error=?, files=?, run_url=CASE WHEN ?<>'' THEN ? ELSE run_url END, updated_at=? WHERE id=?",
                              (status, error[:300], json.dumps(files or []), run_url, run_url, self.clock(), build_id))

    async def get(self, build_id: int) -> Dict[str, Any]:
        r = await self.db.fetchone("SELECT * FROM app_builds WHERE id=?", (build_id,))
        d = dict(r)
        d["files"] = json.loads(d["files"])
        return d

    async def refresh(self, build: Dict[str, Any]) -> Dict[str, Any]:
        if build["status"] not in ("queued", "building") or not self.status()["available"]:
            return build
        age = self.clock() - build["created_at"]
        try:
            r = await self.client.get(f"{API}/repos/{self.cfg.build_github_repo}/actions/runs", headers=self._headers(),
                                      params={"event": "workflow_dispatch", "per_page": 50})
            runs = r.json().get("workflow_runs", []) if r.status_code == 200 else []
            run = next((x for x in runs if build["request_id"] in str(x.get("display_title", ""))), None)
            if not run:
                if age > START_TIMEOUT:
                    await self._finish(build["id"], "failed", "GitHub never started the build.")
                return await self.get(build["id"])
            url = str(run.get("html_url") or "")
            if run.get("status") != "completed":
                if age > RUN_TIMEOUT:
                    await self._finish(build["id"], "failed", "The build took too long.", run_url=url)
                else:
                    await self.db.execute("UPDATE app_builds SET status='building', run_url=?, updated_at=? WHERE id=?", (url, self.clock(), build["id"]))
                return await self.get(build["id"])
            if run.get("conclusion") != "success":
                await self._finish(build["id"], "failed", f"The build failed ({run.get('conclusion')}). Open the run log for details.", run_url=url)
                return await self.get(build["id"])
            files = await self._download(build, int(run["id"]))
            if files:
                await self._finish(build["id"], "done", files=files, run_url=url)
            else:
                await self._finish(build["id"], "failed", "The build finished but produced no files.", run_url=url)
        except (httpx.HTTPError, ValueError, KeyError):
            pass                      # try again on the next look
        return await self.get(build["id"])

    async def _download(self, build: Dict[str, Any], run_id: int) -> List[str]:
        r = await self.client.get(f"{API}/repos/{self.cfg.build_github_repo}/actions/runs/{run_id}/artifacts", headers=self._headers())
        arts = {a["name"]: a for a in r.json().get("artifacts", []) if not a.get("expired")}
        out = []
        folder = self.root / build["app_id"]
        folder.mkdir(parents=True, exist_ok=True)
        for kind in build["platforms"].split(","):
            art = arts.get(kind)
            if not art or int(art.get("size_in_bytes", 0)) > MAX_DOWNLOAD:
                continue
            resp = await self.client.get(art["archive_download_url"], headers=self._headers())
            if resp.status_code != 200 or len(resp.content) > MAX_DOWNLOAD:
                continue
            try:                       # GitHub wraps every artifact in a zip: keep the file inside
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    names = [n for n in z.namelist() if not n.endswith("/")]
                    if len(names) != 1 or z.getinfo(names[0]).file_size > MAX_DOWNLOAD:
                        continue
                    (folder / f"{build['id']}-{kind}{KINDS[kind]}").write_bytes(z.read(names[0]))
            except zipfile.BadZipFile:
                continue
            out.append(kind)
        return out

    async def list_for(self, app_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT id FROM app_builds WHERE app_id=? ORDER BY id DESC LIMIT 10", (app_id,))
        builds = [await self.get(r["id"]) for r in rows]
        return [await self.refresh(b) for b in builds]

    def file_path(self, app_id: str, build_id: int, kind: str) -> Optional[Path]:
        if kind not in KINDS or not re.fullmatch(r"[a-z0-9]{3,32}", app_id):
            return None
        p = self.root / app_id / f"{int(build_id)}-{kind}{KINDS[kind]}"
        return p if p.is_file() else None

    async def aclose(self) -> None:
        await self.client.aclose()
