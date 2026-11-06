"""Managed apps: validate what the owner supplies (name, version, logo, optionally the backend project as a zip) and produce
the backend bundle the owner hosts on their own platform. Nothing here runs the project's code.

The Android / Windows app itself is a branded shell built by ci_build.py; it never contains this project.
"""
import io
import json
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

from llm_agent import builders

MAX_FILES = 2000
SKIP_DIRS = {"__macosx", ".git", ".svn", "node_modules", "__pycache__", ".idea", ".vscode", "venv", ".venv", "env", "build", "dist", ".buildozer", "bin", "preview"}
SKIP_NAMES = {".ds_store", "thumbs.db"}
SKIP_SUFFIXES = (".pem", ".key", ".pyc", ".p12", ".pfx", ".spec")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '&.\-]{0,28}[A-Za-z0-9.]$|^[A-Za-z0-9]{2}$")
VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}(\.\d{1,3})?$")
LOGO_MIN, LOGO_MAX, LOGO_BYTES = 256, 2048, 2 * 1024 * 1024


class KitError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


# ---- name / version / logo ---------------------------------------------------------------------------
def check_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 30 or not NAME_RE.match(name):
        raise KitError("App name: 2 to 30 characters, using letters, numbers, spaces and . - ' & only. "
                       "This is the name shown under the app icon on a phone, so keep it short.")
    return name


def check_version(version: str) -> str:
    version = (version or "").strip()
    if not VERSION_RE.match(version):
        raise KitError("Version must look like 1.0 or 1.0.0. Raise it every time you build a new version of the app.")
    return version


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def png_size(data: bytes) -> Tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise KitError("The logo must be a PNG image (not JPG, GIF or SVG).")
    return struct.unpack(">II", data[16:24])


def check_logo(data: bytes) -> bytes:
    if len(data) > LOGO_BYTES:
        raise KitError("The logo file is larger than 2 MB. Export it again as a smaller PNG.")
    w, h = png_size(data)
    if w != h:
        raise KitError(f"The logo must be square. Yours is {w} × {h} pixels. Crop it to a square, for example 512 × 512.")
    if not LOGO_MIN <= w <= LOGO_MAX:
        raise KitError(f"The logo is {w} × {h} pixels. Use between {LOGO_MIN} and {LOGO_MAX}; 512 × 512 or 1024 × 1024 is ideal.")
    return data


def default_logo(seed: str) -> bytes:
    """A plain generated 512×512 PNG (coloured tile with a light disc) used until the owner supplies a logo."""
    size = 512
    hue = sum(map(ord, seed)) * 37 % 360
    h6, f = divmod(hue / 60, 1)
    q, t = int(255 * 0.62 * (1 - f) + 70), int(255 * 0.62 * f + 70)
    rgb = [(255, t, 70), (q, 255, 70), (70, 255, t), (70, q, 255), (t, 70, 255), (255, 70, q)][int(h6) % 6]
    rgb = tuple(int(c * 0.72) for c in rgb)
    cx = cy = size / 2
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            inside = (x - cx) ** 2 + (y - cy) ** 2 < (size * 0.28) ** 2
            rows += bytes((255, 255, 255)) if inside else bytes(rgb)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


# ---- reading the project -----------------------------------------------------------------------------
def read_project(data: bytes, max_bytes: int) -> Dict[str, Any]:
    """Validate the zip and return {files: {path: bytes}, requirements, skipped, builder}. Never executes anything."""
    if len(data) > max_bytes:
        raise KitError(f"The zip is larger than the {max_bytes // (1024 * 1024)} MB limit.", 413)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise KitError("That is not a valid .zip file.")
    kept: List[Tuple[zipfile.ZipInfo, List[str]]] = []
    skipped = 0
    for info in z.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            raise KitError(f"The zip contains an unsafe path ({info.filename}).")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            skipped += 1
            continue
        parts = [p for p in name.split("/") if p]
        low = parts[-1].lower() if parts else ""
        if not parts or any(p.lower() in SKIP_DIRS for p in parts[:-1]) or low in SKIP_NAMES or low.startswith(".env") and low != ".env.example" or low.endswith(SKIP_SUFFIXES):
            skipped += 1
            continue
        kept.append((info, parts))
    if not kept:
        raise KitError("The zip has no files in it.")
    if len(kept) > MAX_FILES:
        raise KitError(f"The project has more than {MAX_FILES} files.")
    if sum(i.file_size for i, _ in kept) > max_bytes:
        raise KitError(f"The project is larger than {max_bytes // (1024 * 1024)} MB once unzipped.", 413)
    if all(len(p) > 1 for _, p in kept) and len({p[0] for _, p in kept}) == 1:  # one folder around everything
        kept = [(i, p[1:]) for i, p in kept]
    files: Dict[str, bytes] = {}
    for info, parts in kept:
        with z.open(info) as fh:
            files["/".join(parts)] = fh.read(info.file_size + 1)
    code = "\n".join(files[f].decode("utf-8", "replace") for f in files if f.endswith(".py"))
    reqs: List[str] = []
    if "requirements.txt" in files:
        for line in files["requirements.txt"].decode("utf-8", "replace").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                reqs.append(line)
    builder = builders.detect(code, reqs, has_manage_py="manage.py" in files)
    return {"files": files, "requirements": reqs, "skipped": skipped, "builder": builder.id if builder else None}


# ---- the backend bundle the owner hosts --------------------------------------------------------------
DEPLOY = """Hosting this app's backend
===========================
This folder is the backend of "{name}". The Android / Windows app you build in the workbench opens it at the https address
you enter in Manage app. You host it; the workbench never stores your customers' accounts or data.

1. Pick a host (for example Render, Railway, Fly.io, PythonAnywhere or Google Cloud Run) and a database or sign-in service if the app needs one.
2. Install:   pip install -r requirements.txt
3. Settings:  copy .env.example to your host's environment settings and fill in real values there. Never commit real keys.
4. Start with the command in run.json (default: python app.py). The app must listen on the PORT the host gives it.
5. Open the https address, check it works, then enter it under Manage app > App address and press Update.

workbench_client.py and app_config.json connect the app to Manage app (products, heading, colour, announcement, inbox).
app_config.json holds only the App ID and the workbench address. It contains no password.
"""


def backend_bundle(name: str, app_id: str, api_base: str, files: Dict[str, bytes]) -> bytes:
    """The owner's project plus the helper that talks to Manage app, as one zip."""
    api_base = api_base.rstrip("/")
    config = {"app_id": app_id, "name": name, "api_base": api_base, "catalog_url": f"{api_base}/api/catalog/{app_id}/products"}
    out = {**files, "workbench_client.py": (Path(__file__).parent / "kit_client.py").read_bytes(),
           "app_config.json": json.dumps(config, indent=2).encode(), "DEPLOY.txt": DEPLOY.format(name=name).encode()}
    buf = io.BytesIO()
    top = f"{slug(name)}-backend/"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, body in sorted(out.items()):
            info = zipfile.ZipInfo(top + path, date_time=(2025, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, body)
    return buf.getvalue()
