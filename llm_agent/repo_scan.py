"""Fixed-rule safety scan for a repository that was downloaded to be used as an app.

No AI and no network here: the files are only read as text and matched against rules for things real attacks use (download-and-run,
reverse shells, miners, decode-and-run, stealing keys, escaping a container, installs that run code). The result is
  block  - never used: the copy is thrown away
  warn   - allowed only after the user says so ("run anyway")
  clean  - nothing found
A clean result is not a promise that a repository is safe. It lowers the risk; the container limits and a second scan of the changed code do the rest.
"""
import os
import re
from typing import Any, Dict, List, Tuple

LEVELS = {"clean": 0, "warn": 1, "block": 2}
MAX_TEXT = 600_000
MAX_FINDINGS = 60
CODE = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".php", ".rb", ".pl", ".go", ".rs", ".java", ".kt", ".dart", ".cs", ".lua"}
WEB = {".html", ".htm", ".vue", ".svelte", ".ejs", ".hbs", ".jinja", ".jinja2"}
SCRIPT = {".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd"}
SCRIPT_NAMES = {"dockerfile", "makefile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
CONFIG = {".yml", ".yaml", ".toml", ".cfg", ".ini"}
BAD_BYTES = re.compile("[‪-‮⁦-⁩]")                      # hides the real order of code (Trojan Source)
NETWORK = re.compile(r"requests\.|urllib|httpx|aiohttp|fetch\(|axios|http\.request|https\.request|XMLHttpRequest|socket\.|net\.connect", re.I)
ENV_DUMP = re.compile(r"dict\(\s*os\.environ\s*\)|os\.environ\.items\(\)|json\.dumps\(\s*(dict\()?os\.environ|JSON\.stringify\(\s*process\.env|Object\.(entries|keys|values)\(\s*process\.env", re.I)


def kind_of(path: str) -> str:
    name = path.lower().rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1]
    if name == "package.json":
        return "pkg"
    if name in SCRIPT_NAMES:
        return "script"
    if ext in CODE:
        return "code"
    if ext in WEB:
        return "web"
    if ext in SCRIPT:
        return "script"
    if ext in CONFIG:
        return "config"
    if name in ("requirements.txt", "setup.py", "pyproject.toml"):
        return "python-pkg"
    return ""


def _r(level: str, rule: str, why: str, pattern: str, kinds: str, minified: bool = False, flags: int = re.I) -> Tuple[str, str, str, "re.Pattern", set, bool]:
    return level, rule, why, re.compile(pattern, flags), set(kinds.split()), minified


RULES = [
    # --- never allowed -------------------------------------------------------------------------------------------------------
    _r("block", "pipe-to-shell", "It downloads a script from the internet and runs it straight away.",
       r"(curl|wget|Invoke-WebRequest|iwr)\b[^\n|;]{0,200}\|\s*(sudo\s+)?(ba|z|da)?sh\b", "code web pkg"),
    _r("block", "reverse-shell", "It opens a remote control channel back to someone else's computer.",
       r"/dev/tcp/|bash\s+-i\s*>&|\bnc(at)?\s+(-\w+\s+)*-e\s|socat\b[^\n]{0,80}exec:|mkfifo\s+/tmp/\w+", "code web pkg script config"),
    _r("block", "miner", "It contains cryptocurrency-mining code.", r"stratum\+tcp://|xmrig|coinhive|cryptonight|minero\.cc|webminer", "code web pkg script config", True),
    _r("block", "decode-and-run", "It hides code in an encoded text and runs it.",
       r"\b(exec|eval)\s*\(\s*(base64\.b64decode|codecs\.decode|zlib\.decompress|marshal\.loads|bytes\.fromhex|bytearray\.fromhex)"
       r"|eval\s*\(\s*(atob|Buffer\.from)\s*\(|new\s+Function\s*\(\s*atob|child_process[^\n]{0,60}Buffer\.from\([^\n]{0,40}base64", "code web pkg", True),
    _r("block", "key-theft", "It reads private keys or saved passwords from the server.",
       r"authorized_keys|\.ssh/id_|/etc/shadow|\.aws/credentials|\.git-credentials|\.docker/config\.json|wallet\.dat|Login Data|\.config/gcloud", "code script"),
    _r("block", "container-escape", "It tries to reach outside its container (Docker control, host namespaces, privileged mode).",
       r"/var/run/docker\.sock|\bnsenter\b|chroot\s+/host|privileged:\s*true|--privileged|cap_add:\s*\[?\s*(ALL|SYS_ADMIN)", "code script config"),
    _r("block", "destructive", "It contains commands that wipe a disk or the whole server.",
       r"rm\s+-rf\s+(--no-preserve-root\s+)?(/|~|\$HOME)(\s|\"|'|$|\*)|\bmkfs\.\w+|dd\s+if=\S+\s+of=/dev/|:\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", "code script web"),
    _r("block", "hidden-characters", "It uses invisible text-direction characters that can hide what the code really does.", BAD_BYTES.pattern, "code web pkg script config", True, 0),
    # --- needs the user's say-so ---------------------------------------------------------------------------------------------
    _r("warn", "pipe-to-shell-script", "A script downloads something from the internet and runs it.",
       r"(curl|wget)\b[^\n|;]{0,200}\|\s*(sudo\s+)?(ba|z|da)?sh\b", "script config"),
    _r("warn", "runs-programs", "It starts other programs on the server (os.system, shell=True or exec).",
       r"\bos\.system\s*\(|\bos\.popen\s*\(|shell\s*=\s*True|child_process\W{1,3}(exec|execSync)\b|\bexecSync\s*\(|Runtime\.getRuntime\(\)\.exec", "code"),
    _r("warn", "dynamic-code", "It runs text as code (eval / exec) or loads untrusted data formats (pickle).",
       r"(?<![\w.])eval\s*\(|(?<![\w.])exec\s*\(|pickle\.loads?\s*\(|yaml\.load\s*\((?![^)]*Loader\s*=\s*(yaml\.)?Safe)|marshal\.loads", "code"),
    _r("warn", "env-dump", "It reads every environment variable on the server, where secret keys live.", ENV_DUMP.pattern, "code"),
    _r("warn", "chat-webhook", "It sends data to a chat or paste service (Discord, Telegram, a paste site or a tunnel).",
       r"discord(app)?\.com/api/webhooks/|api\.telegram\.org/bot|pastebin\.com/api|transfer\.sh|\bngrok\b|serveo\.net|webhook\.site", "code web script"),
    _r("warn", "raw-ip", "It talks to a server by raw IP address.",
       r"https?://(?!127\.|10\.|192\.168\.|0\.0\.0\.0|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)\d{1,3}(\.\d{1,3}){3}", "code web script"),
    _r("warn", "real-key", "It contains what looks like a real secret key or private key.",
       r"AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{32,}|xox[bap]-[A-Za-z0-9-]{20,}", "code web script config pkg", False, 0),
    _r("warn", "sudo", "A script asks for administrator rights on the server.", r"(^|[;&|\s])sudo\s+\w", "script", False, re.I | re.M),
    _r("warn", "direct-install", "It installs packages from a web address or a git link instead of a package registry.",
       r"^\s*(git\+|https?://|--extra-index-url|--index-url|-e\s)", "python-pkg", False, re.I | re.M),
]
PKG_HOOKS = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepack")
HOOK_BAD = re.compile(r"curl|wget|powershell|node\s+-e|python\s+-c|https?://|\bsh\s+-c|base64", re.I)
SETUP_RISK = re.compile(r"cmdclass|subprocess|os\.system|urlopen|requests\.|urllib", re.I)
BLOB = re.compile(r"[A-Za-z0-9+/=]{3000,}")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _minified(path: str, text: str) -> bool:
    if path.lower().endswith((".min.js", ".min.css", ".bundle.js")):
        return True
    return len(text) > 4000 and len(text) / (text.count("\n") + 1) > 600


def _binary(data: bytes) -> bool:
    head = data[:1100]
    if head[:4] in (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return True
    return head[:2] == b"MZ" and b"PE\x00\x00" in head


def scan(items: List[Tuple[str, bytes]]) -> Dict[str, Any]:
    """items: (path, bytes) pairs. Returns {"verdict", "findings": [{level,file,rule,why,line}], "scanned"}."""
    findings: List[Dict[str, Any]] = []
    seen = set()

    def add(level: str, path: str, rule: str, why: str, line: int = 0) -> None:
        if (path, rule) not in seen and len(findings) < MAX_FINDINGS:
            seen.add((path, rule))
            findings.append({"level": level, "file": path, "rule": rule, "why": why, "line": line})

    scanned = 0
    for path, data in items:
        if BAD_BYTES.search(path):
            add("block", path, "hidden-characters", "A file name uses invisible text-direction characters.")
        if _binary(data):
            add("block", path, "program-file", "It contains a ready-made program file (not source code).")
            continue
        kind = kind_of(path)
        if not kind:
            continue
        text = data[:MAX_TEXT].decode("utf-8", "replace")
        scanned += 1
        mini = _minified(path, text)
        for level, rule, why, pattern, kinds, min_ok in RULES:
            if kind not in kinds or (mini and not min_ok):
                continue
            m = pattern.search(text)
            if m:
                add(level, path, rule, why, _line(text, m.start()))
        if kind == "code" and not mini and NETWORK.search(text) and ENV_DUMP.search(text):
            add("block", path, "env-exfiltration", "It collects every environment variable and sends data over the network, which is how keys get stolen.")
        if kind == "code" and not mini and BLOB.search(text):
            add("warn", path, "encoded-blob", "It holds a very large block of encoded text, which can hide code.")
        if kind == "pkg":
            _package_json(path, text, add)
        if path.lower().rsplit("/", 1)[-1] == "setup.py" and SETUP_RISK.search(text):
            add("warn", path, "setup-runs-code", "Its installer (setup.py) runs extra code while installing.")
    verdict = max((f["level"] for f in findings), key=lambda l: LEVELS[l], default="clean")
    return {"verdict": verdict, "findings": findings, "scanned": scanned}


def _package_json(path: str, text: str, add) -> None:
    import json
    try:
        scripts = (json.loads(text) or {}).get("scripts") or {}
    except ValueError:
        add("warn", path, "bad-package-json", "Its package.json cannot be read.")
        return
    for hook in PKG_HOOKS:
        cmd = scripts.get(hook)
        if isinstance(cmd, str) and cmd.strip():
            if HOOK_BAD.search(cmd):
                add("block", path, "install-script-download", f"Its '{hook}' step downloads or hides code that would run during install.")
            else:
                add("warn", path, "install-script", f"It runs a script during install ('{hook}': {cmd[:60]}).")


def worst(a: str, b: str) -> str:
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b


def plain(result: Dict[str, Any], limit: int = 8) -> List[str]:
    """One short sentence per distinct reason, for showing to the user."""
    out, seen = [], set()
    for f in sorted(result["findings"], key=lambda f: -LEVELS[f["level"]]):
        if f["rule"] in seen:
            continue
        seen.add(f["rule"])
        out.append(f"{f['why']} ({f['file']}{':' + str(f['line']) if f['line'] else ''})")
    return out[:limit]


def flagged_files(result: Dict[str, Any]) -> List[str]:
    return list(dict.fromkeys(f["file"] for f in result["findings"]))
