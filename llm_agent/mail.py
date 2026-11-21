"""Sending e-mail through a Gmail account with an app password (used for verification codes).

Environment: MAIL_USER (the Gmail address) and MAIL_APP_PASSWORD (the 16-character Google app password; spaces are ignored).
"""
import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable

HOST, PORT = "smtp.gmail.com", 465


class MailError(Exception):
    pass


class Mailer:
    def __init__(self, from_name: Callable[[], str] = lambda: "Agent Workbench"):
        self.from_name = from_name

    @property
    def _login(self) -> tuple:
        return os.environ.get("MAIL_USER", "").strip(), "".join(os.environ.get("MAIL_APP_PASSWORD", "").split())

    @property
    def enabled(self) -> bool:
        return all(self._login)

    def _send(self, to: str, subject: str, body: str) -> None:
        user, password = self._login
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = f"{self.from_name()} <{user}>", to, subject
        msg.set_content(body)
        try:
            with smtplib.SMTP_SSL(HOST, PORT, context=ssl.create_default_context(), timeout=15) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise MailError("The mail account refused the login. Check MAIL_USER and the Google app password.")
        except (smtplib.SMTPException, OSError):
            raise MailError("The verification e-mail could not be sent right now. Try again in a moment.")

    async def send(self, to: str, subject: str, body: str) -> None:
        if not self.enabled:
            raise MailError("E-mail is not set up on this server yet. The administrator needs to add MAIL_USER and MAIL_APP_PASSWORD.")
        await asyncio.to_thread(self._send, to, subject, body)
