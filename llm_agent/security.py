"""Password hashing, token helpers, input validation and login throttling."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

MIN_PASSWORD_LEN = 8


def valid_identifier(value: str) -> bool:
    """User ids and session ids: safe for filesystem paths and URLs."""
    return isinstance(value, str) and bool(_ID_RE.match(value)) and ".." not in value


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Tokens are stored hashed so a leaked database does not leak live sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._ -]")


def safe_filename(name: str, max_len: int = 100) -> str:
    """Reduce an uploaded filename to a harmless basename."""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = _FILENAME_BAD.sub("_", base).lstrip(".").strip()
    return base[:max_len]


class LoginThrottle:
    """Sliding-window limiter for failed logins (in-memory, per process)."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)

    def _trim(self, key: str) -> Deque[float]:
        q = self._failures[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._failures.pop(key, None)
            return self._failures[key]
        return q

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.max_failures

    def record_failure(self, key: str) -> None:
        self._trim(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
