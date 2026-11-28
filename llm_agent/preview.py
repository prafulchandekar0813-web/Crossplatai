"""Live preview of web apps the assistant wrote into a user's workspace.

The page is served from short-lived signed links (the page cannot send the login cookie once it is sandboxed),
read-only, and every response carries a CSP `sandbox` so the app runs with no access to this site, to the user's
session or to the API. Only static files (HTML, CSS, JavaScript, images) are served; server code is never executed.
"""
import base64
import hashlib
import hmac
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

TOKEN_TTL_SECONDS = 2 * 3600
SANDBOX_CSP = "sandbox allow-scripts allow-forms allow-modals allow-popups; frame-ancestors 'self'"

# Sandboxed pages have no storage; give simple apps an in-memory stand-in instead of crashing.
STORAGE_SHIM = ("<script>(function(){function mk(){var d={};return{getItem:function(k){return k in d?d[k]:null},"
                "setItem:function(k,v){d[k]=String(v)},removeItem:function(k){delete d[k]},clear:function(){d={}},"
                "key:function(i){return Object.keys(d)[i]||null},get length(){return Object.keys(d).length}}}"
                "['localStorage','sessionStorage'].forEach(function(n){try{window[n].getItem('x')}catch(e){"
                "try{Object.defineProperty(window,n,{value:mk(),configurable:true})}catch(_){}}})})();</script>")

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/wasm", ".wasm")

_SECRET = secrets.token_bytes(32)  # per process: links stop working after a restart, which only means "reload the preview"


def _sig(payload: bytes) -> str:
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:32]


def make_token(user_id: str, now: Optional[float] = None) -> str:
    payload = f"{user_id}|{int((time.time() if now is None else now) + TOKEN_TTL_SECONDS)}".encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + _sig(payload)


def read_token(token: str, now: Optional[float] = None) -> Optional[str]:
    """Return the user id the link was issued to, or None if it is forged or expired."""
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if not hmac.compare_digest(sig, _sig(payload)):
            return None
        user_id, exp = payload.decode().rsplit("|", 1)
        return user_id if int(exp) >= (time.time() if now is None else now) else None
    except (ValueError, UnicodeError):
        return None


def pick_file(target: Path) -> Optional[Path]:
    """A folder means its index.html."""
    if target.is_dir():
        target = target / "index.html"
    return target if target.is_file() else None


def render(target: Path) -> Tuple[bytes, str]:
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    data = target.read_bytes()
    if ctype == "text/html":
        text = data.decode("utf-8", errors="replace")
        lower = text.lower()
        i = lower.find("<head")
        i = lower.find(">", i) + 1 if i != -1 else 0
        data = (text[:i] + STORAGE_SHIM + text[i:]).encode("utf-8")
        ctype = "text/html; charset=utf-8"
    elif ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
        ctype += "; charset=utf-8"
    return data, ctype
