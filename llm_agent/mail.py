"""Sending e-mail (verification codes). Three ways, picked from the environment in this order:

1. Brevo  (HTTPS, works on hosts that block mail ports, such as Render): BREVO_API_KEY, and MAIL_USER = a sender address verified in Brevo.
2. Resend (HTTPS): RESEND_API_KEY, and MAIL_FROM (an address on a domain verified in Resend; "onboarding@resend.dev" only reaches your own address).
3. Gmail SMTP: MAIL_USER (the Gmail address) and MAIL_APP_PASSWORD (16-character app password; spaces are ignored).
   Tries port 465 and then 587. Many hosts (Render's free plan included) block both, and the error says so.
"""
import asyncio
import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Callable, Optional, Tuple

import httpx

HOST = "smtp.gmail.com"
BREVO_URL, RESEND_URL = "https://api.brevo.com/v3/smtp/email", "https://api.resend.com/emails"


class MailError(Exception):
    pass


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


class Mailer:
    def __init__(self, from_name: Callable[[], str] = lambda: "Agent Workbench", client: Optional[httpx.AsyncClient] = None):
        self.from_name = from_name
        self.client = client or httpx.AsyncClient(timeout=20)

    @property
    def provider(self) -> str:
        if _env("BREVO_API_KEY") and _env("MAIL_USER"):
            return "brevo"
        if _env("RESEND_API_KEY") and (_env("MAIL_FROM") or _env("MAIL_USER")):
            return "resend"
        if _env("MAIL_USER") and _env("MAIL_APP_PASSWORD"):
            return "gmail"
        return ""

    @property
    def enabled(self) -> bool:
        return bool(self.provider)

    async def send(self, to: str, subject: str, text: str, html: str = "") -> None:
        provider = self.provider
        if not provider:
            raise MailError("E-mail is not set up on this server yet. The administrator needs to add the mail settings (see README, Profile).")
        if provider == "gmail":
            await asyncio.to_thread(self._gmail, to, subject, text, html)
        else:
            await self._api(provider, to, subject, text, html)

    # ---- HTTPS providers ------------------------------------------------------------------------
    async def _api(self, provider: str, to: str, subject: str, text: str, html: str) -> None:
        name = self.from_name()
        try:
            if provider == "brevo":
                r = await self.client.post(BREVO_URL, headers={"api-key": _env("BREVO_API_KEY")},
                                           json={"sender": {"name": name, "email": _env("MAIL_USER")}, "to": [{"email": to}],
                                                 "subject": subject, "textContent": text, "htmlContent": html or None})
            else:
                sender = _env("MAIL_FROM") or _env("MAIL_USER")
                r = await self.client.post(RESEND_URL, headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}"},
                                           json={"from": f"{name} <{sender}>", "to": [to], "subject": subject, "text": text, "html": html or None})
        except httpx.HTTPError as e:
            raise MailError(f"Could not reach {provider.title()} ({type(e).__name__}). Check the server's internet access and try again.")
        if r.status_code >= 400:
            why = ""
            try:
                body = r.json()
                why = str(body.get("message") or body.get("error") or "")[:200]
            except ValueError:
                pass
            hint = {401: "The API key was refused. Check it.", 403: "The sender address is not verified with the provider."}.get(r.status_code, "")
            raise MailError(f"{provider.title()} refused the message (HTTP {r.status_code}). {hint} {why}".strip())

    # ---- Gmail over SMTP ------------------------------------------------------------------------
    def _gmail(self, to: str, subject: str, text: str, html: str) -> None:
        user, password = _env("MAIL_USER"), "".join(_env("MAIL_APP_PASSWORD").split())
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = f"{self.from_name()} <{user}>", to, subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        blocked: Tuple[str, ...] = ()
        for port in (465, 587):
            try:
                if port == 465:
                    smtp = smtplib.SMTP_SSL(HOST, port, context=ssl.create_default_context(), timeout=12)
                else:
                    smtp = smtplib.SMTP(HOST, port, timeout=12)
                with smtp:
                    if port == 587:
                        smtp.starttls(context=ssl.create_default_context())
                    smtp.login(user, password)
                    smtp.send_message(msg)
                return
            except smtplib.SMTPAuthenticationError:
                raise MailError("Gmail refused the login. Use a 16-character Google app password (2-step verification must be on), "
                                "and MAIL_USER must be the same Gmail address.")
            except smtplib.SMTPRecipientsRefused:
                raise MailError("Gmail did not accept that recipient address.")
            except (socket.timeout, TimeoutError, ConnectionError, socket.gaierror, OSError) as e:
                blocked += (f"{port}: {type(e).__name__}",)
            except smtplib.SMTPException as e:
                raise MailError(f"Gmail rejected the message ({type(e).__name__}).")
        raise MailError("This server cannot reach Gmail's mail ports (" + ", ".join(blocked) + "). Hosts such as Render's free plan block "
                        "outgoing mail ports. Use an HTTPS mail service instead: set BREVO_API_KEY (and MAIL_USER as a verified sender), or RESEND_API_KEY and MAIL_FROM.")
