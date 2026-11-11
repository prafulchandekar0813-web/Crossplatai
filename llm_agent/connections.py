"""Connection settings (API addresses, keys) the user sets for the PREVIEW of an app in one chat.

They are stored next to the chat's temporary environment, handed to the running preview app as environment variables
and nothing else: never put into a built app or the downloadable backend, never shown to the assistant, never sent to the browser in full.
Real accounts and data stay wherever the app's owner chooses to host them; the preview should use test keys."""
import json
import re
from pathlib import Path
from typing import Dict

NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED = {"PATH", "HOME", "PORT", "DATA_DIR", "HOST", "TMPDIR", "LANG", "TERM", "SHELL", "USER", "IFS", "ENV", "BASH_ENV", "PWD", "OLDPWD"}
RESERVED_PREFIXES = ("LD_", "PYTHON", "PIP_", "BASH_", "SSL_", "REQUESTS_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
SECRET = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASS|CREDENTIAL)", re.I)
MAX_ITEMS, MAX_VALUE = 30, 2000


def check(data: Dict[str, str]) -> Dict[str, str]:
    """Validated copy of `data`, or ValueError with a sentence for the user."""
    if not isinstance(data, dict):
        raise ValueError("Send a list of names and values.")
    if len(data) > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} settings.")
    out = {}
    for name, value in data.items():
        name = str(name).strip()
        if not NAME.match(name):
            raise ValueError(f"“{name}” is not a valid name. Use capital letters, digits and underscores, for example API_BASE_URL.")
        if name in RESERVED or name.startswith(RESERVED_PREFIXES):
            raise ValueError(f"“{name}” is used by the system. Pick another name.")
        value = "" if value is None else str(value)
        if len(value) > MAX_VALUE or "\x00" in value:
            raise ValueError(f"The value of {name} is too long or not text.")
        out[name] = value
    return out


def load(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text())
        return check(data)
    except (OSError, ValueError):
        return {}


def save(path: Path, data: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(path)


def merge(existing: Dict[str, str], incoming: Dict[str, str]) -> Dict[str, str]:
    """What the user submitted, except that an empty value for a secret keeps the saved one (the browser never gets it back)."""
    out = {}
    for name, value in check(incoming).items():
        out[name] = existing.get(name, "") if value == "" and SECRET.search(name) else value
    return out


def public(data: Dict[str, str]) -> list:
    """For the browser: secrets are never sent back, only whether they are set."""
    return [{"name": n, "secret": bool(SECRET.search(n)), "set": v != "", "value": "" if SECRET.search(n) else v} for n, v in sorted(data.items())]
