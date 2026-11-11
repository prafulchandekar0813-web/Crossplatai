"""Pre-flight check of an app project before it is shown to the user as "ready to build".
Pure functions over {path: text}: no files, no network. Findings are plain sentences the assistant can act on."""
import json
import re
from typing import Any, Dict, List

from llm_agent import builders

ENTRY = ("app.py", "main.py", "server.py", "manage.py")
SECRETS = [(re.compile(r"sk_live_[0-9A-Za-z]{10,}"), "a live Stripe key"), (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key"),
           (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "a GitHub token"), (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "a Slack token"),
           (re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-/+=]{16,}[\"']"), "a key, token or password written in the code")]
ENV_USE = re.compile(r"""os\.environ(?:\.get\(|\[)\s*["']([A-Z][A-Z0-9_]*)["']|os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']""")
SYSTEM_VARS = {"PORT", "DATA_DIR", "HOME", "PATH", "HOST", "WORKBENCH_NO_UI", "DEBUG"}
ABS_URL = re.compile(r"""(?:fetch\(\s*|src=|href=|action=|url\()\s*["']?/(?![/\s"')])(?!\*)""")


def check(files: Dict[str, str]) -> Dict[str, Any]:
    problems: List[str] = []
    warnings: List[str] = []
    py = {p: t for p, t in files.items() if p.endswith(".py")}
    code = "\n".join(py.values())
    reqs = [ln.strip() for ln in files.get("requirements.txt", "").splitlines() if ln.strip() and not ln.strip().startswith("#")]

    builder = builders.detect(code, reqs, "manage.py" in files)
    if builder and builder.family == "gui":
        problems.append(f"{builder.label} draws its own screens, but the Android and Windows app shows a web address. Rebuild this as a web app (Flask serving HTML) so it can be shown.")
    if not py:
        problems.append("There is no Python backend. Write app.py (Flask) that serves the pages and an /api.")
    entry = next((e for e in ENTRY if e in files), None)
    start = None
    if "run.json" in files:
        try:
            start = str(json.loads(files["run.json"]).get("start") or "")
        except ValueError:
            problems.append("run.json is not valid JSON. It should look like {\"start\": \"python app.py\"}.")
    if not entry and not start:
        problems.append("No start file. Use app.py, main.py or server.py at the top, or add run.json with {\"start\": \"python yourfile.py\"}.")
    if py and not reqs:
        problems.append("requirements.txt is missing. List the packages the backend imports (for example flask).")
    elif builder and builder.imports and not any(re.split(r"[=<>!~\[; ]", r, 1)[0].lower() in {i.lower() for i in builder.imports} for r in reqs):
        warnings.append(f"The code uses {builder.label} but requirements.txt does not list it.")
    if py and not re.search(r"""environ(?:\.get\(|\[)\s*["']PORT["']|getenv\(\s*["']PORT["']|\$PORT|--port""", code + (start or "")):
        problems.append("The app must listen on the port given in the PORT environment variable: port=int(os.environ[\"PORT\"]) (host 0.0.0.0).")
    if re.search(r"""host\s*=\s*["']127\.0\.0\.1["']|host\s*=\s*["']localhost["']""", code):
        warnings.append("The app listens only on 127.0.0.1. Use host=\"0.0.0.0\" so the preview can reach it.")
    if re.search(r"debug\s*=\s*True", code):
        warnings.append("debug=True is on. Turn it off: it exposes a code console to anyone who can reach the app.")

    for path, text in files.items():
        if path.startswith(("preview/", "tests/")) or path.endswith((".md", ".example", ".lock")):
            continue
        for rx, what in SECRETS:
            if rx.search(text):
                problems.append(f"{path} contains {what}. Never put real keys in code: read them from an environment variable.")
                break

    front = {p: t for p, t in files.items() if p.endswith((".html", ".js", ".css"))}
    bad_paths = sorted({p for p, t in front.items() if ABS_URL.search(t)})
    if bad_paths:
        warnings.append("Root-relative addresses (\"/api/items\", src=\"/app.js\") break in the preview and in the built app. "
                        "Use relative ones (\"api/items\", \"static/app.js\"). Found in: " + ", ".join(bad_paths[:5]))
    if not any(p.endswith(".html") for p in files):
        warnings.append("There is no HTML page. The backend should serve at least an index.html (templates/ or static/).")

    used = sorted({a or b for p, t in py.items() for a, b in ENV_USE.findall(t)} - SYSTEM_VARS)
    example = files.get(".env.example", "")
    if used and not example:
        warnings.append("Add a .env.example listing these settings with dummy values: " + ", ".join(used))
    elif used:
        listed = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=", example, re.M))
        missing = [u for u in used if u not in listed]
        if missing:
            warnings.append(".env.example does not list: " + ", ".join(missing))
    if re.search(r"""@app\.(route|get|post)\(\s*["']/(admin|dashboard)""", code):
        warnings.append("This app has its own /admin page. The platform gives the owner an admin dashboard in Manage app (products, heading, colour, "
                        "announcement, usage, inbox), so do not build another unless the user asked for something Manage app does not do.")
    if "workbench_client" not in code and "api/" in "".join(front.values()):
        warnings.append("Content the owner will change (products, texts) should come from workbench_client (Client().sync(\"products\")), not be hardcoded.")

    return {"ok": not problems, "framework": builder.label if builder else None, "problems": problems, "warnings": warnings,
            "files_checked": len(files)}
