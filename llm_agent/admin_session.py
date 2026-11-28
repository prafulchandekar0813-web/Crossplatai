"""The owner's way into an app's own admin pages, and the wall that keeps everyone else out.

Admin pages of a hosted app (/admin and anything the project lists in admin.json) are never served on the app's public address. The owner opens
them from Manage app after entering the app id and password: Manage app asks for a short-lived signed session, and only requests carrying it
reach the admin paths. The proxy then tells the app "this is the owner" with an X-Workbench-Admin header, which the app trusts instead of its own login
(a visitor cannot send it: the proxy removes it from every public request, and the app listens only on a private port).
"""
import base64
import hashlib
import hmac
import posixpath
import secrets
import time
from typing import Iterable, List, Optional
from urllib.parse import unquote

TTL = 30 * 60
COOKIE = "wb_admin"
HEADER = "x-workbench-admin"
DEFAULT_PATHS = ["/admin", "/admin-api"]
_SECRET = secrets.token_bytes(32)          # per process: after a restart the owner simply opens the admin again


def _sig(payload: bytes) -> str:
    return hmac.new(_SECRET, b"admin:" + payload, hashlib.sha256).hexdigest()[:32]


def make_token(app_id: str, now: Optional[float] = None) -> str:
    payload = f"{app_id}|{int((time.time() if now is None else now) + TTL)}".encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + _sig(payload)


def read_token(token: str, now: Optional[float] = None) -> Optional[str]:
    """The app id the session was issued for, or None when it is forged or expired."""
    try:
        body, sig = (token or "").split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if not hmac.compare_digest(sig, _sig(payload)):
            return None
        app_id, exp = payload.decode().rsplit("|", 1)
        return app_id if int(exp) >= (time.time() if now is None else now) else None
    except (ValueError, UnicodeError):
        return None


def clean_paths(raw: Iterable) -> List[str]:
    """admin.json paths: '/admin', '/dashboard/' ... The default is always protected too."""
    out = list(DEFAULT_PATHS)
    for p in raw or []:
        if isinstance(p, str):
            p = "/" + p.strip().strip("/").lower()
            if 1 < len(p) <= 80 and all(c.isalnum() or c in "/-_." for c in p) and ".." not in p and p not in out:
                out.append(p)
    return out[:8]


def normal(path: str) -> str:
    """The path as an application would finally see it: decoded (twice, for %252f tricks), slashes collapsed, dots resolved, lower case."""
    p = path or ""
    for _ in range(2):
        p = unquote(p)
    p = p.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].split(";", 1)[0]
    return posixpath.normpath("/" + p.lstrip("/")).lower().rstrip("/") or "/"


def protected(path: str, paths: Iterable[str]) -> bool:
    n = normal(path)
    return any(n == p or n.startswith(p + "/") for p in (x.rstrip("/").lower() for x in paths))


def cookie_ok(cookie_header: str, app_id: str) -> bool:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and read_token(v) == app_id:
            return True
    return False


def without_cookie(cookie_header: str) -> str:
    """The Cookie header with our admin cookie removed, so the app never sees it."""
    return "; ".join(p.strip() for p in (cookie_header or "").split(";") if p.strip() and not p.strip().startswith(COOKIE + "="))
