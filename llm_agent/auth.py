"""Accounts and sessions.

Every API call is tied to a server-issued session token (HttpOnly cookie or Bearer header); the user id used by
the rest of the system always comes from that token, never from the request body.
"""
import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from llm_agent.db import Database
from llm_agent.security import (
    MIN_PASSWORD_LEN,
    LoginThrottle,
    hash_password,
    new_token,
    token_digest,
    valid_identifier,
    verify_password,
)
from llm_agent.settings import ConfigError, Settings

SESSION_TTL_SECONDS = 7 * 24 * 3600
MAX_PASSWORD_LEN = 256
_DUMMY_HASH = hash_password("not-a-real-password")  # equalises timing for unknown usernames


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str  # "user" | "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.throttle = LoginThrottle(max_failures=5, window_seconds=300)

    # ---- helpers ---------------------------------------------------------------------------------
    @property
    def admin_name(self) -> str:
        return self.settings.effective.admin.admin_username.strip().lower()

    @staticmethod
    def _check_new_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters", 400)
        if len(password) > MAX_PASSWORD_LEN:
            raise AuthError(f"Password must be at most {MAX_PASSWORD_LEN} characters", 400)

    async def _issue(self, principal: Principal) -> str:
        token = new_token()
        await self.db.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, role, expires_at) VALUES (?,?,?,?)",
            (token_digest(token), principal.user_id, principal.role, time.time() + SESSION_TTL_SECONDS),
        )
        return token

    # ---- accounts --------------------------------------------------------------------------------
    async def register(self, user_id: str, password: str) -> Tuple[str, Principal]:
        if not self.settings.effective.allow_registration:
            raise AuthError("Registration is closed. Ask the administrator for an account.", 403)
        uid = (user_id or "").strip().lower()
        if not valid_identifier(uid) or len(uid) < 3 or len(uid) > 32:
            raise AuthError("Username must be 3-32 characters: letters, digits, '_', '-' or '.'", 400)
        self._check_new_password(password)
        if uid == self.admin_name:
            raise AuthError("That username is taken", 409)
        pw_hash = await asyncio.to_thread(hash_password, password)
        try:
            await self.db.insert(
                "INSERT INTO users (user_id, password_hash, created_at) VALUES (?,?,?)",
                (uid, pw_hash, time.time()),
            )
        except sqlite3.IntegrityError:
            raise AuthError("That username is taken", 409)
        principal = Principal(uid, "user")
        return await self._issue(principal), principal

    async def login(self, user_id: str, password: str, client: str = "") -> Tuple[str, Principal]:
        uid = (user_id or "").strip().lower()
        password = password if isinstance(password, str) else ""
        key = f"{client}|{uid}"
        if self.throttle.blocked(key):
            raise AuthError("Too many failed attempts. Try again in a few minutes.", 429)
        if len(password) > MAX_PASSWORD_LEN:
            raise AuthError("Invalid username or password", 401)

        principal: Optional[Principal] = None
        if uid and uid == self.admin_name:
            stored = self.settings.effective.admin.admin_password_hash
            if stored and await asyncio.to_thread(verify_password, password, stored):
                principal = Principal(uid, "admin")
        elif valid_identifier(uid):
            row = await self.db.fetchone("SELECT password_hash FROM users WHERE user_id=?", (uid,))
            ok = await asyncio.to_thread(verify_password, password, row["password_hash"] if row else _DUMMY_HASH)
            if row and ok:
                principal = Principal(uid, "user")
        else:
            await asyncio.to_thread(verify_password, password, _DUMMY_HASH)

        if principal is None:
            self.throttle.record_failure(key)
            raise AuthError("Invalid username or password", 401)
        self.throttle.reset(key)
        return await self._issue(principal), principal

    async def authenticate(self, token: Optional[str]) -> Optional[Principal]:
        if not token:
            return None
        row = await self.db.fetchone(
            "SELECT user_id, role, expires_at FROM auth_sessions WHERE token_hash=?", (token_digest(token),)
        )
        if not row or row["expires_at"] < time.time():
            return None
        if row["role"] == "admin":
            if row["user_id"] != self.admin_name:  # admin account was renamed since this token was issued
                return None
            return Principal(row["user_id"], "admin")
        exists = await self.db.fetchone("SELECT 1 FROM users WHERE user_id=?", (row["user_id"],))
        return Principal(row["user_id"], "user") if exists else None

    async def logout(self, token: Optional[str]) -> None:
        if token:
            await self.db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_digest(token),))

    async def change_password(self, principal: Principal, current: str, new: str, keep_token: Optional[str]) -> None:
        self._check_new_password(new)
        if principal.is_admin:
            stored = self.settings.effective.admin.admin_password_hash
        else:
            row = await self.db.fetchone("SELECT password_hash FROM users WHERE user_id=?", (principal.user_id,))
            stored = row["password_hash"] if row else ""
        if not stored or not await asyncio.to_thread(verify_password, current or "", stored):
            raise AuthError("Current password is incorrect", 403)
        new_hash = await asyncio.to_thread(hash_password, new)
        if principal.is_admin:
            try:
                self.settings.set_admin_password_hash(new_hash)
            except ConfigError as e:
                raise AuthError(str(e), 409)
        else:
            await self.db.execute("UPDATE users SET password_hash=? WHERE user_id=?", (new_hash, principal.user_id))
        # Sign every other device out.
        await self.db.execute(
            "DELETE FROM auth_sessions WHERE user_id=? AND role=? AND token_hash<>?",
            (principal.user_id, principal.role, token_digest(keep_token or "")),
        )

    # ---- admin helpers ---------------------------------------------------------------------------
    async def list_users(self) -> List[dict]:
        rows = await self.db.fetchall(
            "SELECT u.user_id, u.created_at,"
            " (SELECT COUNT(*) FROM chat_history c WHERE c.user_id=u.user_id AND c.role='user') AS messages,"
            " (SELECT COUNT(*) FROM permanent_facts f WHERE f.user_id=u.user_id) AS facts"
            " FROM users u ORDER BY u.created_at"
        )
        return [dict(r) for r in rows]

    async def delete_account(self, user_id: str) -> bool:
        n = await self.db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await self.db.execute("DELETE FROM auth_sessions WHERE user_id=? AND role='user'", (user_id,))
        return n > 0
