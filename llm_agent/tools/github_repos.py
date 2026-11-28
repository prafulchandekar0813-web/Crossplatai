"""Find open-source projects on GitHub and bring a copy into the user's workspace, so a new app can start from a real, good-looking UI.

Only public repositories with a permissive licence (MIT, Apache-2.0, BSD, ISC, ...) are cloned: those allow reuse and changes as long as the
licence text is kept. The copy is downloaded as a zip (no git needed), checked (size, file count, paths, links) and written with a SOURCE.md that
records where it came from. Only github.com, api.github.com and codeload.github.com are contacted. An optional GITHUB_TOKEN raises the search limit."""
import asyncio
import io
import os
import re
import stat
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import httpx

from llm_agent import repo_review, repo_scan
from llm_agent import overlay
from llm_agent.repo_library import LibraryError, RepoLibrary
from llm_agent.tools.file_manager import WorkspaceError, WorkspaceFiles

API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"
PERMISSIVE = {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC", "UNLICENSE", "0BSD", "CC0-1.0", "MIT-0", "ZLIB", "BSL-1.0"}
SKIP_DIRS = {".git", ".github", ".idea", ".vscode", "node_modules", "__pycache__", "venv", ".venv", ".next", ".nuxt", "coverage", "__tests__", "tests", "test"}
SKIP_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".class", ".jar", ".zip", ".gz", ".tar", ".7z", ".rar", ".mp4", ".mov", ".avi", ".mkv", ".mp3",
            ".wav", ".psd", ".sketch", ".fig", ".dmg", ".apk", ".iso", ".pdf", ".ai", ".eps", ".map", ".lock"}
MAX_FILES, MAX_FILE, MAX_TOTAL, MAX_DOWNLOAD, MAX_REPO_KB = 1500, 1_500_000, 25_000_000, 40_000_000, 60_000
_REPO = re.compile(r"^(?:https?://(?:www\.)?github\.com/)?([\w.-]{1,39})/([\w.-]{1,100}?)(?:\.git)?(?:/(?:tree|blob)/[^\s]*)?/?$", re.I)


class GitHubError(Exception):
    pass


def parse_repo(text: str) -> Tuple[str, str]:
    m = _REPO.match((text or "").strip())
    if not m:
        raise GitHubError("Give the repository as owner/name, for example facebook/react, or its github.com address.")
    return m.group(1), m.group(2)


def permissive(spdx: str) -> bool:
    return (spdx or "").upper() in PERMISSIVE


class GitHubRepos:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, library: Optional[RepoLibrary] = None):
        self.library = library
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8), follow_redirects=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "AgentWorkbench", "X-GitHub-Api-Version": "2022-11-28"}
        if os.environ.get("GITHUB_TOKEN", "").strip():
            h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"].strip()
        return h

    async def _api(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            r = await self._http.get(API + path, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            raise GitHubError(f"Could not reach GitHub ({type(e).__name__}).")
        if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("GitHub's search limit was reached for now (about 10 searches a minute). Wait a minute, or the administrator can add GITHUB_TOKEN.")
        if r.status_code == 404:
            raise GitHubError("That repository does not exist or is private.")
        if r.status_code >= 400:
            raise GitHubError(f"GitHub answered HTTP {r.status_code}.")
        return r.json()

    # ---- search ---------------------------------------------------------------------------------
    async def search(self, query: str, language: str = "") -> Dict[str, Any]:
        query = " ".join((query or "").split())[:200]
        if len(query) < 3:
            return {"status": "error", "error": "Give a few words to search for, for example 'restaurant menu ordering ui'."}
        q = f"{query} archived:false fork:false stars:>20" + (f" language:{re.sub(r'[^A-Za-z0-9+#.-]', '', language)[:20]}" if language else "")
        try:
            data = await self._api("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 20})
        except GitHubError as e:
            return {"status": "error", "error": str(e)}
        rows = []
        for it in data.get("items", []):
            spdx = ((it.get("license") or {}).get("spdx_id") or "")
            rows.append({"repo": it["full_name"], "description": (it.get("description") or "")[:200], "stars": it.get("stargazers_count", 0),
                         "language": it.get("language") or "", "license": spdx or "none", "usable": permissive(spdx), "size_kb": it.get("size", 0),
                         "updated": (it.get("pushed_at") or "")[:10], "topics": (it.get("topics") or [])[:6]})
        usable = [r for r in rows if r["usable"] and r["size_kb"] <= MAX_REPO_KB]
        skipped = len(rows) - len(usable)
        out: Dict[str, Any] = {"status": "success", "query": query, "results": usable[:8]}
        if skipped:
            out["note"] = f"{skipped} more result(s) were left out because their licence is not permissive or they are too big to copy."
        if not usable:
            out["note"] = "No reusable repository matched. Try other words, a different language, or describe the UI differently."
        return out

    # ---- clone ----------------------------------------------------------------------------------
    async def _obtain(self, repo: str, subdir: str, ask: Any) -> Dict[str, Any]:
        """The project's files and the safety verdict: from the shared library when it is already there, otherwise downloaded from GitHub.
        Raises GitHubError for anything the user should be told (licence, size, nothing usable)."""
        owner, name = parse_repo(repo)
        subdir = subdir.strip("/")
        hit = self.library.find(f"{owner}/{name}", subdir) if self.library else None
        if hit:
            items, meta = await asyncio.to_thread(self.library.load, *hit)
            result = await asyncio.to_thread(repo_scan.scan, items)           # the fixed rules run again on every use
            verdict = {"verdict": repo_scan.worst(result["verdict"], meta.get("verdict", "clean")), "scan": result["verdict"],
                       "ai": meta.get("ai", "library"), "reasons": meta.get("reasons", [])}
            return {"items": items, "skipped": 0, "license_text": None, "full": meta.get("repo", f"{owner}/{name}"), "spdx": meta.get("license", ""),
                    "branch": meta.get("branch", "main"), "stars": meta.get("stars", 0), "description": meta.get("description", ""), "result": result, "verdict": verdict, "from_library": hit}
        info = await self._api(f"/repos/{owner}/{name}")
        spdx = (info.get("license") or {}).get("spdx_id") or ""
        if not permissive(spdx):
            raise GitHubError(f"{owner}/{name} has the licence '{spdx or 'none'}', which does not clearly allow reuse in your own app. "
                              "Only MIT, Apache-2.0, BSD, ISC and similar licences are copied. Search for another project.")
        if info.get("size", 0) > MAX_REPO_KB:
            raise GitHubError(f"{owner}/{name} is {info['size'] // 1024} MB, too big to copy. Choose a smaller project, or use only its folder with subdir.")
        branch = info.get("default_branch") or "main"
        blob = await self._download(owner, info["name"], branch)
        items, skipped, license_text = await asyncio.to_thread(self._unpack, blob, subdir)
        if not items:
            raise GitHubError("Nothing usable was found in that repository" + (f" under '{subdir}'." if subdir else "."))
        if license_text is not None and not any(n.lower().startswith("license") for n, _ in items):
            items.append(("LICENSE", license_text))
        result = await asyncio.to_thread(repo_scan.scan, items)
        verdict = await repo_review.review(ask, items, result) if ask is not None else {"verdict": result["verdict"], "scan": result["verdict"], "ai": "off", "reasons": []}
        return {"items": items, "skipped": skipped, "license_text": None, "full": f"{owner}/{info['name']}", "spdx": spdx, "branch": branch,
                "stars": info.get("stargazers_count", 0), "description": (info.get("description") or "")[:200], "result": result, "verdict": verdict, "from_library": None}

    def _save(self, got: Dict[str, Any], category: str, subdir: str) -> Optional[str]:
        """Keep a checked project in the shared library (never a blocked one). Returns 'category/key'."""
        if not self.library or got["verdict"]["verdict"] == "block":
            return None
        cat, key = self.library.save(category, got["full"], subdir.strip("/"), got["items"], {
            "license": got["spdx"], "branch": got["branch"], "stars": got["stars"], "description": got.get("description", ""), "verdict": got["verdict"]["verdict"], "ai": got["verdict"]["ai"],
            "reasons": repo_scan.plain(got["result"])[:6] + got["verdict"]["reasons"][:6]})
        return f"{cat}/{key}"

    async def add_to_library(self, repo: str, category: str, subdir: str = "", ask: Any = None) -> Dict[str, Any]:
        """The administrator's way to add a project: same download and safety check, saved only when it is clean."""
        if not self.library:
            return {"status": "error", "error": "The library is not set up."}
        try:
            got = await self._obtain(repo, subdir, ask)
            reasons = repo_scan.plain(got["result"]) + got["verdict"]["reasons"]
            if got["verdict"]["verdict"] != "clean":
                return {"status": "refused", "repo": got["full"], "reasons": reasons or ["The reviewer judged it unsafe."]}
            where = got["from_library"] and "/".join(got["from_library"]) or await asyncio.to_thread(self._save, got, category, subdir)
        except (GitHubError, LibraryError) as e:
            return {"status": "error", "error": str(e)}
        return {"status": "success", "repo": got["full"], "path": where, "files": len(got["items"])}

    # ---- clone ----------------------------------------------------------------------------------
    async def clone(self, files: WorkspaceFiles, repo: str, dest: str, subdir: str = "", user_id: str = "", ask: Any = None, approvals: Any = None,
                    category: str = "") -> Dict[str, Any]:
        """Get the project (shared library first, else GitHub), check it (fixed rules, then the model), keep a checked copy in the shared library,
        and write a copy into the user's own project folder. The copy IS the app: it keeps its framework and structure and is changed in place."""
        try:
            got = await self._obtain(repo, subdir, ask)
            items, full, spdx, verdict, result = list(got["items"]), got["full"], got["spdx"], got["verdict"], got["result"]
            reasons = repo_scan.plain(result) + verdict["reasons"]
            if verdict["verdict"] == "block":
                return {"status": "blocked", "repo": full, "reasons": reasons or ["The reviewer judged it unsafe."], "nothing_copied": True,
                        "next": "Tell the user in one line that this project was refused for safety and why, then use the next best result from github_search. Do not try to copy it again."}
            if verdict["verdict"] == "warn":
                if not (approvals and approvals.approved(user_id, full)):
                    if approvals:
                        approvals.ask(user_id, full)
                    return {"status": "needs_approval", "repo": full, "reasons": reasons, "nothing_copied": True,
                            "next": "Nothing was copied. Tell the user, in plain words and without jargon, what was found (the reasons above), and that they can reply exactly "
                                    "'run anyway' to use it or 'pick another' to look for another project. Do NOT call github_clone for it again until they have said 'run anyway'; "
                                    "you cannot approve it for them."}
                approvals.used(user_id, full)
            dest = (dest or full.split("/")[-1]).strip().strip("/")
            if dest.startswith("refs/") or dest == "refs":
                raise GitHubError("Copy the project into its own project folder (the default), not into refs/: the copy is the app and is changed in place.")
            existing = files.resolve(dest, allow_root=True)
            if existing.is_dir() and any(existing.iterdir()):
                raise GitHubError(f"The folder '{dest}' already has files. Choose another dest, or ask the user before replacing it.")
            saved = got["from_library"] and "/".join(got["from_library"]) or await asyncio.to_thread(self._save, got, category, subdir)
            note = "approved by the user after a warning" if verdict["verdict"] == "warn" else "no problems found"
            source = (f"# Source\n\nThis folder is a copy of https://github.com/{full} (branch {got['branch']}).\n"
                      f"Licence: {spdx}. Keep the licence file and this note when you reuse or change the code.\n\n"
                      f"Safety scan: {note} (rules: {result['verdict']}, AI review: {verdict['ai']}).\n\n"
                      "This is the app. Keep its framework and folder structure and change it in place (only the files you change or add are the user's own; the rest stay as the shared original); do not build a second structure beside it.\n")
            base_items = list(items)
            items.append(("SOURCE.md", source.encode()))
            await asyncio.to_thread(files.import_files, dest, items, not saved)      # a project that lives in the library is not charged to the user (overlay.py)
            if saved:
                await asyncio.to_thread(overlay.write, files.resolve(dest, allow_root=True), saved, base_items)
        except (GitHubError, WorkspaceError, LibraryError) as e:
            return {"status": "error", "error": str(e)}
        return {"status": "success", "repo": full, "license": spdx, "stars": got["stars"], "dest": dest, "library": saved,
                "files": len(items), "skipped": got["skipped"], "overview": self._overview(items), "scan": {"verdict": verdict["verdict"], "notes": reasons},
                "next": (f"The project is ready to work on in '{dest}'. The shared original stays untouched in the library; the user's own files are only the ones you change or add, so open and edit just the files the request needs "
                         f"(read_file, patch_file) and leave every other file alone. Do not edit anything yet: call analyze_project on '{dest}' first. It reads the whole project, "
                         "finds the framework, the data and the admin, and writes PROJECT.md. Then change the project in place with the user's request, keeping its framework.")}

    async def _download(self, owner: str, name: str, branch: str) -> bytes:
        try:
            async with self._http.stream("GET", f"{CODELOAD}/{owner}/{name}/zip/refs/heads/{branch}") as r:
                if r.status_code >= 400:
                    raise GitHubError(f"The download failed (HTTP {r.status_code}).")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_DOWNLOAD:
                        raise GitHubError("The repository is too big to download.")
                return bytes(buf)
        except httpx.HTTPError as e:
            raise GitHubError(f"The download failed ({type(e).__name__}).")

    @staticmethod
    def _unpack(blob: bytes, subdir: str) -> Tuple[List[Tuple[str, bytes]], int, Optional[bytes]]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            raise GitHubError("The download was not a valid zip file.")
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_TOTAL * 4:
            raise GitHubError("The repository is too big to copy.")
        out: List[Tuple[str, bytes]] = []
        skipped, total, license_text = 0, 0, None
        for i in infos:
            parts = i.filename.split("/")[1:]                              # drop the "repo-branch/" top folder
            rel = "/".join(parts)
            if not rel or ".." in parts or rel.startswith("/") or "\\" in rel or "\x00" in rel:
                continue
            if stat.S_ISLNK(i.external_attr >> 16):                        # links could point outside the folder
                skipped += 1
                continue
            if parts[-1].lower().startswith(("license", "licence")) and len(parts) == 1:
                license_text = zf.read(i)[:60000]
            if subdir:
                if rel != subdir and not rel.startswith(subdir + "/"):
                    continue
                rel = rel[len(subdir):].lstrip("/")
            if any(p in SKIP_DIRS for p in parts[:-1]) or os.path.splitext(rel)[1].lower() in SKIP_EXT or i.file_size > MAX_FILE:
                skipped += 1
                continue
            if len(out) >= MAX_FILES or total + i.file_size > MAX_TOTAL:
                skipped += 1
                continue
            out.append((rel, zf.read(i)))
            total += i.file_size
        return out, skipped, license_text

    @staticmethod
    def _overview(items: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        names = [n for n, _ in items]
        def has(*ext): return [n for n in names if n.lower().endswith(ext)]
        top = sorted({n.split("/")[0] + ("/" if "/" in n else "") for n in names})[:40]
        ui = (has(".html", ".jinja", ".jinja2", ".ejs", ".hbs", ".vue", ".svelte", ".jsx", ".tsx") + has(".css", ".scss", ".less"))[:25]
        pkg = next((b for n, b in items if n == "package.json"), b"")
        return {"top_level": top, "ui_files": ui, "needs_build": b'"build"' in pkg and not has("index.html") if pkg else False,
                "entry_points": [n for n in names if n.lower() in ("index.html", "app.py", "main.py", "server.py", "package.json", "readme.md")][:6]}
