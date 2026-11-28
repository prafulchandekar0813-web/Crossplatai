"""Pre-flight check of an app project before it is shown to the user as "ready to build".
Pure functions over {path: text}: no files, no network. Findings are plain sentences the assistant can act on."""
import json
import re
from typing import Any, Dict, List

from llm_agent import builders, content_schema, uilint

ENTRY = ("app.py", "main.py", "server.py", "manage.py")
SECRETS = [(re.compile(r"sk_live_[0-9A-Za-z]{10,}"), "a live Stripe key"), (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key"),
           (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "a GitHub token"), (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "a Slack token"),
           (re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-/+=]{16,}[\"']"), "a key, token or password written in the code")]
ENV_USE = re.compile(r"""os\.environ(?:\.get\(|\[)\s*["']([A-Z][A-Z0-9_]*)["']|os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']""")
SYSTEM_VARS = {"PORT", "DATA_DIR", "HOME", "PATH", "HOST", "WORKBENCH_NO_UI", "DEBUG"}
ABS_URL = re.compile(r"""(?:fetch\(\s*|src=|href=|action=|url\()\s*["']?/(?![/\s"')])(?!\*)""")


# Calls that current library versions no longer have. requirements.txt installs the newest version, so the app dies at start.
REMOVED = [
    (re.compile(r"before_first_request"), "`before_first_request` was removed from Flask. Run that setup code once at start-up instead (call the function before app.run, or put it under `with app.app_context():`)."),
    (re.compile(r"from\s+flask\s+import[^\n]*\b(Markup|escape)\b"), "Flask no longer exports Markup or escape. Use `from markupsafe import Markup, escape`."),
    (re.compile(r"json_encoder|flask\.json\.JSONEncoder|JSON_AS_ASCII|JSON_SORT_KEYS"), "Flask removed json_encoder and the JSON_* config keys. Use `app.json.default` / `app.json.sort_keys` instead."),
    (re.compile(r"request\.is_xhr"), "`request.is_xhr` was removed. Check the X-Requested-With header yourself."),
    (re.compile(r"werkzeug\.urls\s+import[^\n]*\b(url_quote|url_decode|url_encode|url_unquote)\b"), "Werkzeug removed url_quote and friends. Use urllib.parse instead."),
    (re.compile(r"\bsqlalchemy\.ext\.declarative\s+import\s+declarative_base"), "Import declarative_base from sqlalchemy.orm (the old location is deprecated)."),
]


def _walk(blocks: Any) -> Any:
    for b in blocks if isinstance(blocks, list) else []:
        if isinstance(b, dict):
            yield b
            yield from _walk(b.get("children"))


def check(files: Dict[str, str], platforms: Any = None) -> Dict[str, Any]:
    problems: List[str] = []
    warnings: List[str] = []
    py = {p: t for p, t in files.items() if p.endswith(".py")}
    code = "\n".join(py.values())
    reqs = [ln.strip() for ln in files.get("requirements.txt", "").splitlines() if ln.strip() and not ln.strip().startswith("#")]

    if any(re.split(r"[=<>!~\[; ]", r, maxsplit=1)[0].lower() in ("workbench-client", "workbench_client") for r in reqs):
        warnings.append("requirements.txt lists workbench_client, which is not a package; remove that line (the platform adds workbench_client.py itself).")
    builder = builders.detect(code, reqs, "manage.py" in files)
    if builder and builder.family == "gui":
        problems.append(f"{builder.label} draws its own screens, but the Android and Windows app shows a web address. Rebuild this as a web app (Flask serving HTML) so it can be shown.")
    for path, text in py.items():
        try:
            compile(text, path, "exec")
        except SyntaxError as e:
            problems.append(f"{path} has a syntax error on line {e.lineno}: {e.msg}.")
    for rx, why in REMOVED:
        if rx.search(code):
            problems.append(why)
    if any(re.split(r"[=<>!~\[; ]", r, maxsplit=1)[0].lower() == "flask" and not re.search(r"[=<>~]", r) for r in reqs):
        warnings.append("requirements.txt lists flask without a version, so the newest one is installed. Pin it (for example flask>=3.0,<4) so the app keeps working.")
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
    layout, why = content_schema.from_project({p: t.encode() for p, t in files.items() if p == "manage.json"})
    if why:
        problems.append(why)
    elif layout is None:
        warnings.append("manage.json is missing, so Manage app will show a generic list. Add it to describe what the owner manages (sections and fields).")
    else:
        names = {s["name"] for s in layout["sections"]}
        used = set(re.findall(r"""\.sync\(\s*["']([A-Za-z0-9_-]+)["']""", code))
        for u in sorted(used - names):
            warnings.append(f"The code reads the section {u!r} but manage.json has no section with that name.")
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
    pages = {p: t for p, t in front.items() if p.endswith(".html")}
    no_viewport = sorted(p for p, t in pages.items() if "viewport" not in t.lower() and "<html" in t.lower())
    if no_viewport:
        warnings.append("These pages have no <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">, so they look tiny and unresponsive on a phone: " + ", ".join(no_viewport[:5]))
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

    ui = uilint.lint(files, [str(x) for x in platforms] if isinstance(platforms, list) else None)
    return {"ok": not problems, "framework": builder.label if builder else None, "problems": problems, "warnings": warnings,
            "design": ui, "files_checked": len(files)}
