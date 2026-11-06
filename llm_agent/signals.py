"""Cheap, deterministic misuse signals. The Reporter uses them as a floor: the model reviewer can raise the
risk level but never lower it below what these rules found, so text that talks the reviewer round cannot hide misuse."""
import re
from typing import Dict, List

LEVELS = ("none", "low", "medium", "high")

# (category, level, pattern) — matched case-insensitively against what the user typed or the command they ran.
RULES = [
    ("malware", "high", r"\b(ransomware|keylogger|botnet|rootkit|remote access trojan|\brat\b.*(build|write|make)|credential stealer|infostealer|crypto ?miner)\b"),
    ("malware", "medium", r"\b(reverse shell|bind shell|payload|obfuscat\w+ (a )?(virus|malware)|disable (antivirus|defender))\b"),
    ("phishing / fraud", "high", r"\b(phishing|fake (login|bank|paypal|invoice|receipt)|clone (of )?(a )?(bank|login)|steal (passwords?|credentials|cards?|cookies)|carding|credit card dump)\b"),
    ("phishing / fraud", "medium", r"\b(spoof\w*|impersonat\w+|scam|fake reviews?|fake (id|passport|certificate))\b"),
    ("attack on others", "high", r"\b(ddos|denial of service|brute[- ]?force (attack|login|password)|sql injection (on|against)|exploit (for|against)|hack into|crack (the )?password|port ?scan(ning)? (the )?(internet|network))\b"),
    ("attack on others", "medium", r"\b(nmap|sqlmap|hydra|metasploit|hashcat|john the ripper)\b"),
    ("abuse of this system", "high", r"\b(ignore (all |your )?(previous|prior|above) instructions|reveal (your )?system prompt|print (your )?system prompt|jailbreak|developer mode|bypass (the )?(guard|filter|block ?list|sandbox|safety))\b"),
    ("abuse of this system", "medium", r"\b(escape (the )?(sandbox|container|environment)|other users?'? (files|data|chats)|admin (password|hash|token)|config\.json|app\.db|\.env\b|/etc/(passwd|shadow)|/proc/\d*)"),
    ("resource abuse", "medium", r"\b(infinite loop|fork bomb|mine (bitcoin|crypto|monero)|stress test the server|spam (bot|emails?|messages?)|mass (mailer|email))\b"),
    ("weapons / violence", "high", r"\b(build|make|synthesi[sz]e) (a )?(bomb|explosive|nerve agent|bioweapon|untraceable gun)\b"),
]
_COMPILED = [(c, lvl, re.compile(p, re.I | re.S)) for c, lvl, p in RULES]


def scan(text: str) -> List[Dict[str, str]]:
    """Every rule that matches `text`, as {category, level, match}."""
    out = []
    for cat, lvl, rx in _COMPILED:
        m = rx.search(text or "")
        if m:
            out.append({"category": cat, "level": lvl, "match": m.group(0)[:60]})
    return out


def top(levels) -> str:
    """The highest of several risk levels ('none' when empty)."""
    best = 0
    for lv in levels:
        if lv in LEVELS:
            best = max(best, LEVELS.index(lv))
    return LEVELS[best]
