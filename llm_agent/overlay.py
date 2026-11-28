"""A copied project in a user's workspace: the original lives in ONE place (the shared repo library); the user's files are only what they changed or added.

The whole project is still unpacked inside the user's private workspace, because it has to be run and built there, but every file that is still
exactly as in the library is remembered in `.base.json` (path -> size and fingerprint) and is left out of the user's Files list, their storage
quota and the files they are shown. The moment a file is edited it stops matching, so it appears as the user's own. Nothing here is executed."""
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MANIFEST = ".base.json"
MAX_MANIFEST = 2_000_000


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def write(folder: Path, base: str, items: List[Tuple[str, bytes]]) -> None:
    """Remember which files in `folder` came unchanged from the library entry `base` ('category/key')."""
    files = {rel: [len(data), fingerprint(data)] for rel, data in items}
    (folder / MANIFEST).write_text(json.dumps({"base": base, "files": files}), "utf-8")


def load(folder: Path) -> Optional[Dict[str, Any]]:
    try:
        f = folder / MANIFEST
        if not f.is_file() or f.stat().st_size > MAX_MANIFEST:
            return None
        d = json.loads(f.read_text("utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("files"), dict) else None
    except (OSError, ValueError):
        return None


def untouched(manifest: Dict[str, Any], rel: str, path: Path) -> bool:
    """True when the file is still exactly the library's copy."""
    want = manifest["files"].get(rel)
    if not want:
        return False
    try:
        if path.stat().st_size != want[0]:
            return False
        return fingerprint(path.read_bytes()) == want[1]
    except OSError:
        return False
