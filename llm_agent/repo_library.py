"""The shared library of copied GitHub projects, kept on the server OUTSIDE every user's files so any user's app can start from the same copy.

Layout (under data_dir/repo_library):  <category>/<owner--name>/files/...  and  <category>/<owner--name>/meta.json
Only projects that passed the safety check are saved (a blocked one never is). A user's workspace gets a copy of the files; the library itself is only
changed by the administrator (add, delete) or by the assistant saving a freshly checked project. No file here is ever executed."""
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CATEGORY = "other"
MAX_CATEGORIES = 60


class LibraryError(Exception):
    pass


def slug(text: str, default: str = DEFAULT_CATEGORY) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]
    return s or default


def key_of(repo: str, subdir: str = "") -> str:
    """owner/name (+ folder) -> a folder name that cannot escape the library."""
    base = re.sub(r"[^a-z0-9.]+", "-", repo.lower().replace("/", "--")).strip("-")[:90]
    sub = slug(subdir, "") if subdir else ""
    return f"{base}--{sub}" if sub else base


class RepoLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, category: str, key: str = "") -> Path:
        cat = slug(category)
        if key and not re.fullmatch(r"[a-z0-9.-]{1,140}", key):
            raise LibraryError("That is not a project in the library.")
        p = self.root / cat / key if key else self.root / cat
        if not p.resolve().is_relative_to(self.root.resolve()):
            raise LibraryError("Access denied.")
        return p

    # ---- read ------------------------------------------------------------------------------------
    def tree(self) -> List[Dict[str, Any]]:
        out = []
        if not self.root.is_dir():
            return out
        for cat in sorted(p for p in self.root.iterdir() if p.is_dir()):
            repos = []
            for r in sorted(p for p in cat.iterdir() if p.is_dir()):
                meta = self._meta(r)
                if meta:
                    repos.append({"key": r.name, "repo": meta.get("repo", ""), "subdir": meta.get("subdir", ""), "license": meta.get("license", ""),
                                  "description": meta.get("description", ""), "files": meta.get("files", 0), "bytes": meta.get("bytes", 0), "saved_at": meta.get("saved_at", 0),
                                  "verdict": meta.get("verdict", ""), "path": f"{cat.name}/{r.name}"})
            out.append({"category": cat.name, "repos": repos})
        return out

    def _meta(self, d: Path) -> Dict[str, Any]:
        try:
            m = json.loads((d / "meta.json").read_text("utf-8"))
            return m if isinstance(m, dict) else {}
        except (OSError, ValueError):
            return {}

    def search(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Saved projects whose name, description or category match the words of the query, best first."""
        words = [w for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2]
        scored = []
        for cat in self.tree():
            for r in cat["repos"]:
                hay = f"{r['repo']} {r.get('description', '')} {cat['category']}".lower()
                score = sum(hay.count(w) for w in words)
                if score:
                    scored.append((score, {"repo": r["repo"], "subdir": r["subdir"], "category": cat["category"], "license": r["license"], "description": r.get("description", ""), "files": r["files"]}))
        return [x for _, x in sorted(scored, key=lambda t: -t[0])[:limit]]

    def find(self, repo: str, subdir: str = "") -> Optional[Tuple[str, str]]:
        """(category, key) of a saved copy, or None."""
        key = key_of(repo, subdir)
        if not self.root.is_dir():
            return None
        for cat in self.root.iterdir():
            if (cat / key / "meta.json").is_file():
                return cat.name, key
        return None

    def load(self, category: str, key: str) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
        d = self._dir(category, key)
        meta, base = self._meta(d), d / "files"
        if not meta or not base.is_dir():
            raise LibraryError("That project is not in the library.")
        root, items = base.resolve(), []
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.is_symlink() and f.resolve().is_relative_to(root):
                items.append((f.relative_to(base).as_posix(), f.read_bytes()))
        return items, meta

    def listing(self, category: str, key: str) -> List[Dict[str, Any]]:
        items, _ = self.load(category, key)
        return [{"path": n, "bytes": len(b)} for n, b in items][:500]

    # ---- change ----------------------------------------------------------------------------------
    def save(self, category: str, repo: str, subdir: str, items: List[Tuple[str, bytes]], meta: Dict[str, Any]) -> Tuple[str, str]:
        cat, key = slug(category), key_of(repo, subdir)
        old = self.find(repo, subdir)
        if old:
            self.delete(*old)
        if not (self.root / cat).exists() and self.root.is_dir() and sum(1 for p in self.root.iterdir() if p.is_dir()) >= MAX_CATEGORIES:
            raise LibraryError("The library has too many categories. Delete an empty one first.")
        d = self._dir(cat, key)
        base = d / "files"
        base.mkdir(parents=True, exist_ok=True)
        root = base.resolve()
        for rel, data in items:
            t = (base / rel).resolve()
            if not t.is_relative_to(root):
                raise LibraryError(f"Unsafe path in the project: {rel}")
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(data)
        meta = {**meta, "repo": repo, "subdir": subdir, "files": len(items), "bytes": sum(len(b) for _, b in items), "saved_at": int(time.time())}
        (d / "meta.json").write_text(json.dumps(meta), "utf-8")
        return cat, key

    def make_category(self, name: str) -> str:
        cat = slug(name)
        self._dir(cat).mkdir(parents=True, exist_ok=True)
        return cat

    def delete(self, category: str, key: str) -> None:
        d = self._dir(category, key)
        if not d.is_dir():
            raise LibraryError("That project is not in the library.")
        shutil.rmtree(d)

    def delete_category(self, category: str) -> None:
        d = self._dir(category)
        if not d.is_dir():
            raise LibraryError("That category does not exist.")
        shutil.rmtree(d)
