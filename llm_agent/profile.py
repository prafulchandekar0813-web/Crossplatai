"""A user's profile (name, phone, e-mail) and e-mail verification with a one-time code."""
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable, Dict

from llm_agent.db import Database
from llm_agent import mail_templates
from llm_agent.mail import MailError, Mailer

OTP_TTL = 600            # seconds a code stays valid
OTP_TRIES = 5
OTP_GAP = 60             # seconds between two codes
OTP_PER_HOUR = 5
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{5,18}[0-9]$")


class ProfileError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _hash(salt: str, code: str) -> str:
    return hashlib.sha256((salt + code).encode()).hexdigest()


class Profiles:
    def __init__(self, db: Database, mailer: Mailer, brand: Callable[[], str] = lambda: "Agent Workbench",
                 clock: Callable[[], float] = time.time):
        self.db, self.mailer, self.brand, self.clock = db, mailer, brand, clock

    async def get(self, user_id: str) -> Dict[str, Any]:
        row = await self.db.fetchone("SELECT full_name, phone, email, email_verified FROM users WHERE user_id=?", (user_id,))
        if not row:
            raise ProfileError("Account not found", 404)
        return {"user_id": user_id, "name": row["full_name"], "phone": row["phone"], "email": row["email"],
                "email_verified": bool(row["email_verified"]), "email_ready": self.mailer.enabled}

    async def update(self, user_id: str, name: str, phone: str, email: str) -> Dict[str, Any]:
        name, phone, email = " ".join((name or "").split()), (phone or "").strip(), (email or "").strip().lower()
        if len(name) > 60:
            raise ProfileError("The name can be up to 60 characters.")
        if phone and not PHONE_RE.match(phone):
            raise ProfileError("Enter the phone number with digits only, optionally starting with + and a country code.")
        if email and not EMAIL_RE.match(email):
            raise ProfileError("That e-mail address does not look right.")
        before = await self.get(user_id)
        verified = before["email_verified"] and email == before["email"]
        await self.db.execute("UPDATE users SET full_name=?, phone=?, email=?, email_verified=? WHERE user_id=?",
                              (name, phone, email, int(verified), user_id))
        if not verified:
            await self.db.execute("DELETE FROM email_otps WHERE user_id=?", (user_id,))
        return await self.get(user_id)

    async def send_code(self, user_id: str) -> Dict[str, Any]:
        prof = await self.get(user_id)
        if not prof["email"]:
            raise ProfileError("Save your e-mail address first.")
        if prof["email_verified"]:
            raise ProfileError("This e-mail address is already verified.")
        if not self.mailer.enabled:
            raise ProfileError("E-mail is not set up on this server yet. The administrator needs to add the mail settings (see the README, Profile section).", 503)
        now = self.clock()
        row = await self.db.fetchone("SELECT sent_at FROM email_otps WHERE user_id=?", (user_id,))
        sent = [t for t in json.loads(row["sent_at"]) if now - t < 3600] if row else []
        if sent and now - sent[-1] < OTP_GAP:
            raise ProfileError(f"A code was just sent. You can ask for another in {int(OTP_GAP - (now - sent[-1])) + 1} seconds.", 429)
        if len(sent) >= OTP_PER_HOUR:
            raise ProfileError("Too many codes requested. Try again in an hour.", 429)
        code, salt = f"{secrets.randbelow(10**6):06d}", secrets.token_hex(8)
        await self.db.execute(
            "INSERT INTO email_otps (user_id, email, salt, code_hash, expires_at, attempts, sent_at) VALUES (?,?,?,?,?,0,?) "
            "ON CONFLICT(user_id) DO UPDATE SET email=excluded.email, salt=excluded.salt, code_hash=excluded.code_hash, "
            "expires_at=excluded.expires_at, attempts=0, sent_at=excluded.sent_at",
            (user_id, prof["email"], salt, _hash(salt, code), now + OTP_TTL, json.dumps(sent + [now])))
        try:
            subject, text, html = mail_templates.verification(self.brand(), code, OTP_TTL // 60)
            await self.mailer.send(prof["email"], subject, text, html)
        except MailError as e:
            await self.db.execute("UPDATE email_otps SET sent_at=? WHERE user_id=?", (json.dumps(sent), user_id))  # a failed send must not use up the user's tries
            raise ProfileError(str(e), 502)
        return {"sent": True, "to": prof["email"]}

    async def verify_code(self, user_id: str, code: str) -> Dict[str, Any]:
        prof = await self.get(user_id)
        row = await self.db.fetchone("SELECT email, salt, code_hash, expires_at, attempts FROM email_otps WHERE user_id=?", (user_id,))
        if not row or row["email"] != prof["email"] or row["expires_at"] < self.clock():
            raise ProfileError("That code has expired. Ask for a new one.")
        if row["attempts"] >= OTP_TRIES:
            raise ProfileError("Too many wrong codes. Ask for a new one.", 429)
        code = "".join((code or "").split())
        if not hmac.compare_digest(_hash(row["salt"], code), row["code_hash"]):
            await self.db.execute("UPDATE email_otps SET attempts=attempts+1 WHERE user_id=?", (user_id,))
            raise ProfileError("That code is not right.")
        await self.db.execute("UPDATE users SET email_verified=1 WHERE user_id=?", (user_id,))
        await self.db.execute("DELETE FROM email_otps WHERE user_id=?", (user_id,))
        return await self.get(user_id)
