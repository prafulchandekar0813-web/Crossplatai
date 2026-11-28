"""Sending verification e-mail: the HTML message, the HTTPS providers, and honest errors."""
import smtplib

import httpx
import pytest

from llm_agent import mail_templates
from llm_agent.mail import MailError, Mailer


def _clear(monkeypatch):
    for k in ("BREVO_API_KEY", "RESEND_API_KEY", "MAIL_USER", "MAIL_APP_PASSWORD", "MAIL_FROM"):
        monkeypatch.delenv(k, raising=False)


def test_the_message_is_professional_and_safe():
    subject, text, html = mail_templates.verification("Acme <Studio>", "123456", 10)
    assert subject.startswith("123456 is your") and "123456" in text and "10 minutes" in text
    assert "1 2 3 4 5 6" in html and "Acme &lt;Studio&gt;" in html and "<Studio>" not in html and "Confirm your e-mail address" in html


async def test_brevo_is_used_over_https_and_errors_are_explained(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BREVO_API_KEY", "k"); monkeypatch.setenv("MAIL_USER", "me@example.com")
    seen = []
    def ok(req):
        seen.append((str(req.url), req.headers["api-key"], req.read()))
        return httpx.Response(201, json={})
    m = Mailer(client=httpx.AsyncClient(transport=httpx.MockTransport(ok)))
    assert m.provider == "brevo" and m.enabled
    await m.send("to@example.com", "Hi", "text", "<b>html</b>")
    assert seen[0][0].endswith("/v3/smtp/email") and b"htmlContent" in seen[0][2]
    bad = Mailer(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "sender not valid"}))))
    with pytest.raises(MailError, match="not verified"):
        await bad.send("to@example.com", "Hi", "t")


async def test_resend_and_unconfigured(monkeypatch):
    _clear(monkeypatch)
    assert Mailer().enabled is False
    with pytest.raises(MailError, match="not set up"):
        await Mailer().send("a@b.co", "s", "t")
    monkeypatch.setenv("RESEND_API_KEY", "k"); monkeypatch.setenv("MAIL_FROM", "onboarding@resend.dev")
    m = Mailer(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "1"}))))
    assert m.provider == "resend"
    await m.send("a@b.co", "s", "t", "<p>x</p>")


async def test_gmail_says_when_the_host_blocks_mail_ports(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MAIL_USER", "me@gmail.com"); monkeypatch.setenv("MAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    def blocked(*a, **k):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(smtplib, "SMTP_SSL", blocked); monkeypatch.setattr(smtplib, "SMTP", blocked)
    with pytest.raises(MailError, match="block"):
        await Mailer().send("a@b.co", "s", "t")
    class Refuse:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): raise smtplib.SMTPAuthenticationError(535, b"bad")
    monkeypatch.setattr(smtplib, "SMTP_SSL", Refuse)
    with pytest.raises(MailError, match="app password"):
        await Mailer().send("a@b.co", "s", "t")


async def test_a_failed_send_does_not_use_up_the_users_wait(env):
    class Down:
        enabled = True
        async def send(self, *a, **k): raise MailError("down")
    env.orch.profiles.mailer = Down()
    c = await env.signup("pat")
    await c.post("/api/account/profile", json={"email": "pat@example.com"})
    assert (await c.post("/api/account/email/send")).status_code == 502
    assert (await c.post("/api/account/email/send")).status_code == 502          # not 429: the first try did not count
