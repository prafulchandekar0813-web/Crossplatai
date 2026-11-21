"""Encrypted storage for the keys owners give their hosted apps (database URLs, provider API keys).

* AES-256-GCM, a fresh random nonce per value, and the app id + name bound in as associated data, so a stored value cannot be
  copied to another app or name.
* The master key lives ONLY in the environment (SECRETS_MASTER_KEY), never in the database, config.json or a file we write.
  Without it nothing can be stored or decrypted. Generate one with:  python -m llm_agent.vault
* Rotation: put the new key in SECRETS_MASTER_KEY and the previous one in SECRETS_MASTER_KEY_OLD; values are re-encrypted
  under the new key by rotate().
* Values are write-only: no function here returns one to a browser, and nothing here logs one.
"""
import base64
import hashlib
import os
import secrets
import time
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from llm_agent import connections

MAX_VALUE = 8000
MAX_SECRETS = 30


class VaultError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _load(var: str) -> Optional[bytes]:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        return None
    return key if len(key) == 32 else None


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"id:" + key).hexdigest()[:8]


class Vault:
    """Encrypts and decrypts single values. Reads the environment on every call, so a rotated key needs no restart."""

    def keys(self) -> Dict[str, bytes]:
        out = {}
        for var in ("SECRETS_MASTER_KEY", "SECRETS_MASTER_KEY_OLD"):
            k = _load(var)
            if k:
                out[key_id(k)] = k
        return out

    def current_id(self) -> Optional[str]:
        k = _load("SECRETS_MASTER_KEY")
        return key_id(k) if k else None

    def problem(self) -> str:
        """Why keys cannot be saved right now, in words for the administrator ('' when they can)."""
        if self.available():
            return ""
        if os.environ.get("SECRETS_MASTER_KEY", "").strip():
            return "SECRETS_MASTER_KEY is set but is not a valid key: it must be exactly what `python -m llm_agent.vault` prints (44 characters, 32 random bytes), not a password."
        return "SECRETS_MASTER_KEY is not set."

    def available(self) -> bool:
        return self.current_id() is not None

    @staticmethod
    def _aad(app_id: str, name: str) -> bytes:
        return f"wb-secret:{app_id}:{name}".encode()

    def encrypt(self, app_id: str, name: str, value: str) -> str:
        key = _load("SECRETS_MASTER_KEY")
        if key is None:
            raise VaultError("Saving keys is not set up on this server: " + self.problem(), 503)
        nonce = secrets.token_bytes(12)
        ct = AESGCM(key).encrypt(nonce, value.encode(), self._aad(app_id, name))
        return f"v1.{key_id(key)}.{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, app_id: str, name: str, blob: str) -> str:
        try:
            version, kid, payload = blob.split(".", 2)
            key = self.keys()[kid]
            raw = base64.urlsafe_b64decode(payload)
            return AESGCM(key).decrypt(raw[:12], raw[12:], self._aad(app_id, name)).decode()
        except (ValueError, KeyError, InvalidTag):
            raise VaultError("A saved key could not be decrypted (wrong or missing master key).", 500)

    def is_current(self, blob: str) -> bool:
        return blob.split(".", 2)[1:2] == [self.current_id()]


def check_pair(name: str, value: str) -> Dict[str, str]:
    name = str(name or "").strip()
    try:
        connections.check({name: ""})                    # same name rules as preview connections (capitals, no system names)
    except ValueError as e:
        raise VaultError(str(e))
    value = "" if value is None else str(value)
    if not value.strip():
        raise VaultError("Enter a value.")
    if len(value) > MAX_VALUE or "\x00" in value:
        raise VaultError(f"The value is too long (at most {MAX_VALUE} characters) or not text.")
    return {name: value}


class AppSecrets:
    def __init__(self, db, vault: Optional[Vault] = None, clock=time.time):
        self.db, self.vault, self.clock = db, vault or Vault(), clock

    async def set(self, app_id: str, name: str, value: str) -> None:
        name, value = next(iter(check_pair(name, value).items()))
        have = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_secrets WHERE app_id=? AND name<>?", (app_id, name))
        if have["n"] >= MAX_SECRETS:
            raise VaultError(f"At most {MAX_SECRETS} keys per app.")
        blob = self.vault.encrypt(app_id, name, value)
        await self.db.execute("INSERT INTO app_secrets(app_id, name, blob, updated_at) VALUES (?,?,?,?) "
                              "ON CONFLICT(app_id, name) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
                              (app_id, name, blob, self.clock()))

    async def names(self, app_id: str) -> List[Dict[str, object]]:
        rows = await self.db.fetchall("SELECT name, updated_at FROM app_secrets WHERE app_id=? ORDER BY name", (app_id,))
        return [{"name": r["name"], "updated_at": r["updated_at"]} for r in rows]

    async def delete(self, app_id: str, name: str) -> bool:
        return await self.db.execute("DELETE FROM app_secrets WHERE app_id=? AND name=?", (app_id, name)) > 0

    async def delete_app(self, app_id: str) -> None:
        await self.db.execute("DELETE FROM app_secrets WHERE app_id=?", (app_id,))

    async def plain(self, app_id: str) -> Dict[str, str]:
        """Decrypted values for starting the app. Only the hosting code calls this; it never leaves the server."""
        rows = await self.db.fetchall("SELECT name, blob FROM app_secrets WHERE app_id=?", (app_id,))
        return {r["name"]: self.vault.decrypt(app_id, r["name"], r["blob"]) for r in rows}

    async def rotate(self) -> int:
        """Re-encrypt everything under the current key. Returns how many values were changed."""
        n = 0
        for r in await self.db.fetchall("SELECT app_id, name, blob FROM app_secrets"):
            if not self.vault.is_current(r["blob"]):
                new = self.vault.encrypt(r["app_id"], r["name"], self.vault.decrypt(r["app_id"], r["name"], r["blob"]))
                await self.db.execute("UPDATE app_secrets SET blob=?, updated_at=? WHERE app_id=? AND name=?", (new, self.clock(), r["app_id"], r["name"]))
                n += 1
        return n


if __name__ == "__main__":
    print("SECRETS_MASTER_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))
