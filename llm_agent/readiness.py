"""The "ready for build and Manage app" gate for a project that began as a copied repository (it has SOURCE.md).

Building the Android and Windows apps and hosting stay locked until every required check passes; advice items are shown but do not block.
Projects the assistant wrote itself have no SOURCE.md and are not affected. Everything here reads the project's files; the sandbox run is a separate, live check.
"""
import hashlib
import re
from typing import Any, Dict, List, Optional

from llm_agent import appkit, buildcheck, datastore, project_audit, repo_scan


def _item(id: str, ok: bool, detail: str, required: bool = True) -> Dict[str, Any]:
    return {"id": id, "ok": ok, "required": required, "detail": detail}


def digest(files: Dict[str, bytes]) -> str:
    """A fingerprint of the project as it would be built: the files a zip carries, without the notes (*.md) the assistant keeps updating."""
    h = hashlib.sha256()
    for name in sorted(n for n in files if not appkit.is_skipped(n) and not n.lower().endswith(".md")):
        h.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return h.hexdigest()


def check(files: Dict[str, bytes], ran: Optional[bool] = None, node_ok: bool = False) -> Dict[str, Any]:
    """ran: did this exact project start in the sandbox (True/False), or is that unknown because the sandbox is off (None)."""
    if "SOURCE.md" not in files:
        return {"applies": False, "ready": True, "checks": [], "missing": []}
    out: List[Dict[str, Any]] = []
    md = files.get("PROJECT.md", b"")[:300].decode("utf-8", "replace")
    analysed = project_audit.MARK in md
    out.append(_item("analysed", analysed, "PROJECT.md was written by analyze_project." if analysed else "Call analyze_project on the project: it reads it and writes PROJECT.md."))
    items = [(n, b) for n, b in files.items()]
    scan = repo_scan.scan(items)
    blocked = scan["verdict"] == "block"
    out.append(_item("scan", not blocked, "No unsafe code found." + (" Some risky-looking code remains: " + "; ".join(repo_scan.plain(scan, 3)) if scan["verdict"] == "warn" else "")
                     if not blocked else "Unsafe code was found after the changes: " + "; ".join(repo_scan.plain(scan, 4))))
    report = project_audit.audit({n: b for n, b in files.items() if n not in ("PROJECT.md",)})
    sup = report["hosting"]["support"]
    can_host = sup == "yes" or (sup in ("build", "node") and node_ok)
    out.append(_item("hosting", can_host, "It can be hosted here." if can_host else (report["hosting"]["why"] or "It cannot be hosted here.")))
    if report["language"] == "python":
        bc = buildcheck.check({n: b.decode("utf-8", "replace") for n, b in files.items() if len(b) <= 300_000 and not n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"))})
        out.append(_item("build", bc["ok"], "check_build passes." if bc["ok"] else "check_build found: " + " ".join(bc["problems"][:3])))
        if report["start"].get("problem") or not report["start"]["command"]:
            out.append(_item("start", False, report["start"].get("problem") or "It has no start command (run.json or a standard entry file such as app.py)."))
        elif not report["start"]["listens_on_port_env"]:
            out.append(_item("port", False, "It must listen on 0.0.0.0 and the PORT environment variable."))
    if report["language"] in ("python", "node") or report["database"]["engines"]:
        choice = datastore.read_choice(files.get("data.json", b""))
        out.append(_item("data", bool(choice), "Data is saved: " + (choice["mode"] + ", " + choice["engine"]) if choice else
                         "Ask the user where to save data and write data.json (use_skill data-storage)."))
    admin, has_manage = report["admin"], "manage.json" in files
    if admin["exists"]:
        problems = []
        if admin["default_passwords_in"]:
            problems.append("a default password is still in " + ", ".join(admin["default_passwords_in"]))
        if not admin["trusts_manage_login"]:
            problems.append("it does not yet trust the Manage login (X-Workbench-Admin)")
        out.append(_item("admin", not problems, "Its admin is tied to the Manage login." if not problems else "The admin is not protected: " + "; ".join(problems) + ". Use use_skill admin-access."))
    else:
        out.append(_item("admin", False, "The project has no admin. Add one inside the app (an /admin area that trusts the Manage login) where the owner manages everything "
                                         "inside the app: its content, orders, users. Use use_skill admin-access, section B."))
    hard = report["content"]["hard_coded_in"]
    if has_manage or hard:
        out.append(_item("content", not (hard and not has_manage), "No sample content is written inside the code." if not hard else
                         "Content may still be written inside code: " + ", ".join(hard[:4]) + ". Serve it from the admin or Manage app instead.", required=not has_manage))
    if report["language"] in ("python", "node") and (admin["exists"] or report["database"]["engines"]):
        seeded = bool(re.search(r"\bseed|starter|INSERT\s+INTO|\bsample\b", "\n".join(b[:80_000].decode("utf-8", "replace") for n, b in files.items()
                                                                                    if n.lower().endswith((".py", ".js", ".sql", ".json"))), re.I))
        out.append(_item("seed", seeded, "It fills itself with starter content on first start." if seeded else
                         "The app opens empty. Fill its database once on first start with 12 to 20 realistic starter entries (marked sample) so the owner has content to edit in the admin.", required=False))
    if ran is not None:
        out.append(_item("ran", ran, "It started in the sandbox and its page answered." if ran else
                         "Run it once in the sandbox with these exact files: bash with background=true, then look at the page. Run it again after the last change."))
    lic = any(n.upper().startswith(("LICENSE", "COPYING")) or n.lower().startswith("licenses/") for n in files)
    out.append(_item("licence", lic, "The licence text is kept." if lic else "Keep the repository's LICENSE file."))
    text = "\n".join(b[:60_000].decode("utf-8", "replace") for n, b in files.items() if n.lower().endswith((".html", ".jinja", ".jinja2", ".js", ".jsx", ".vue", ".py", ".md", ".txt")))[:900_000]
    out.append(_item("credit", bool(re.search(r"based on\s+\S+/\S+", text, re.I)), "The source is credited." if re.search(r"based on\s+\S+/\S+", text, re.I) else
                     "Credit the source on an About screen: 'Based on owner/repo, licence'.", required=False))
    missing = [c["detail"] for c in out if c["required"] and not c["ok"]]
    return {"applies": True, "ready": not missing, "checks": out, "missing": missing}
