"""Keeps personal data and secrets out of the shared knowledge (flags, skills and what the curator reads).

`clean` removes what it can recognise from a short note; `problems` names what is left in text that is about to become a
shared skill, so a person can see why it was refused. Both are plain functions over strings."""
import re
from typing import List

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{8,}\d)(?!\d)")
URL_SECRET = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")
LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-]{24,}\b)(?=\w*\d)(?=\w*[A-Za-z])[A-Za-z0-9_\-]{24,}\b")
KEYLIKE = re.compile(r"\b(sk|pk|rk|ghp|gho|xox[bap]|AKIA|rzp_(live|test))[-_A-Za-z0-9]{8,}")
HOME_PATH = re.compile(r"(/home|/Users|/root|C:\\Users)[/\\][^\s/\\]+")
APP_ID = re.compile(r"\b[0-9a-f]{24}\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def clean(text: str, limit: int = 300) -> str:
    t = " ".join(str(text or "").split())
    for pat, repl in ((EMAIL, "[email]"), (URL_SECRET, r"\1"), (KEYLIKE, "[key]"), (LONG_TOKEN, "[token]"), (HOME_PATH, "[path]"),
                      (APP_ID, "[id]"), (CARD, "[number]"), (PHONE, "[phone]")):
        t = pat.sub(repl, t)
    return t[:limit]


def problems(text: str) -> List[str]:
    """What a shared skill must not contain."""
    found = []
    for label, pat in (("an email address", EMAIL), ("a phone number", PHONE), ("something that looks like a key or token", KEYLIKE),
                       ("a long secret-looking string", LONG_TOKEN), ("a personal file path", HOME_PATH), ("an app id", APP_ID),
                       ("a card-like number", CARD)):
        if pat.search(text or ""):
            found.append(label)
    return found
