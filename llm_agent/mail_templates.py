"""The e-mails the system sends: a plain-text part and a styled HTML part (table layout and inline CSS, so mail apps show it properly)."""
from html import escape
from typing import Tuple


def verification(brand: str, code: str, minutes: int) -> Tuple[str, str, str]:
    """(subject, text, html) for an e-mail verification code."""
    b, spaced = escape(brand), " ".join(code)
    subject = f"{code} is your {brand} verification code"
    text = (f"Your {brand} verification code is {code}\n\nIt works for {minutes} minutes. Enter it on the Profile page to confirm your e-mail address.\n\n"
            f"If you did not ask for this, you can ignore this message. Nobody can use the code without your account.\n\n{brand}")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"></head>
<body style="margin:0;padding:0;background:#f1f4f9;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">Your verification code is {escape(code)}. It works for {minutes} minutes.</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f4f9;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:520px;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e3e8f0;">
<tr><td style="background:#3b4fd8;padding:22px 28px;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:19px;font-weight:700;color:#ffffff;">{b}</td></tr>
<tr><td style="padding:30px 28px 8px;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1c2333;">
<h1 style="margin:0 0 10px;font-size:21px;line-height:1.3;color:#1c2333;">Confirm your e-mail address</h1>
<p style="margin:0 0 22px;font-size:15px;line-height:1.6;color:#4a5468;">Use this code on the Profile page to verify your e-mail. It works for {minutes} minutes.</p>
<div style="background:#f1f4ff;border:1px solid #d5dcff;border-radius:12px;padding:18px 10px;text-align:center;font-family:Consolas,Menlo,monospace;font-size:34px;font-weight:700;letter-spacing:6px;color:#2b3bb5;">{escape(spaced)}</div>
<p style="margin:22px 0 6px;font-size:13.5px;line-height:1.6;color:#6b7488;">Did not ask for this? You can ignore this e-mail. Nobody can use the code without access to your account, and it expires on its own.</p>
</td></tr>
<tr><td style="padding:18px 28px 26px;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:12px;line-height:1.5;color:#8b94a7;border-top:1px solid #eef1f6;">
Sent by {b}. This is an automatic message, please do not reply.</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, html
