"""Read a whole project (usually a downloaded repository) and say what it is: framework, how it starts, where its data and content live,
whether it already has an admin and how that is protected. Plain rules, no AI, no network. The result is written to PROJECT.md by the
analyze_project tool, and the assistant may not edit a copied repository until that file exists.
"""
import json
import re
from typing import Any, Dict, List, Tuple

MARK = "<!-- workbench:analyzed -->"
PY_FRAMEWORKS = [("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask"), ("starlette", "Starlette"), ("tornado", "Tornado"), ("aiohttp", "aiohttp"), ("bottle", "Bottle")]
JS_FRAMEWORKS = [("next", "Next.js"), ("nuxt", "Nuxt"), ("@angular/core", "Angular"), ("gatsby", "Gatsby"), ("astro", "Astro"), ("@nestjs/core", "NestJS"),
                 ("@strapi/strapi", "Strapi"), ("express", "Express"), ("fastify", "Fastify"), ("koa", "Koa"), ("svelte", "Svelte"), ("vue", "Vue"), ("react", "React")]
SERVER_JS = {"Express", "Fastify", "Koa", "NestJS", "Strapi", "Next.js", "Nuxt"}
DATABASES = [("postgres", r"psycopg|asyncpg|postgres|\"pg\"|pg-promise|sequelize.*postgres"), ("mysql", r"pymysql|mysqlclient|mysql2|mysql-connector|aiomysql|mariadb"),
             ("mongodb", r"pymongo|motor\b|mongoose|\"mongodb\""), ("redis", r"\bredis\b|ioredis|aioredis"), ("firebase", r"firebase[-_]admin|firebase/|\"firebase\""),
             ("sqlite", r"sqlite3|aiosqlite|better-sqlite3|sqlite:///|\.sqlite3?\b|\.db\b")]
ORMS = [("SQLAlchemy", r"sqlalchemy"), ("Django ORM", r"django"), ("Peewee", r"\bpeewee\b"), ("Prisma", r"\bprisma\b"), ("Sequelize", r"sequelize"), ("TypeORM", r"typeorm"), ("Mongoose", r"mongoose")]
ADMIN_HINTS = [("django.contrib.admin", "Django admin", "/admin/"), ("flask_admin", "Flask-Admin", "/admin/"), ("flask-admin", "Flask-Admin", "/admin/"),
               ("adminjs", "AdminJS", "/admin"), ("@strapi/strapi", "Strapi admin", "/admin"), ("sqladmin", "SQLAdmin", "/admin")]
ROUTE_ADMIN = re.compile(r"""(?:route|get|post|use|mount|Blueprint|APIRouter|path|url)\s*\(\s*r?['"]/?(admin|dashboard|manage|backoffice)\b""", re.I)
DEFAULT_PW = re.compile(r"""(pass(word)?|pwd|secret)\w*\s*[=:]\s*['"](admin|password|123456|1234|admin123|changeme|root|test|demo|letmein)['"]""", re.I)
ENV_NAME = re.compile(r"""os\.environ(?:\.get)?\s*[\[(]\s*['"]([A-Z][A-Z0-9_]{2,40})['"]|os\.getenv\(\s*['"]([A-Z][A-Z0-9_]{2,40})['"]|process\.env\.([A-Z][A-Z0-9_]{2,40})""")
OBJECT_ITEM = re.compile(r"""\{\s*['"]?(?:name|title)['"]?\s*[:=]""")
DATA_DIRS = ("data/", "content/", "db/", "fixtures/", "seed/", "_data/", "public/data/", "static/data/")
SKIP_JSON = {"package.json", "package-lock.json", "tsconfig.json", "manifest.json", "composer.json", "manage.json", "run.json", "jsconfig.json", "vercel.json", "netlify.json"}


def _text(files: Dict[str, bytes], name: str) -> str:
    return files.get(name, b"").decode("utf-8", "replace")


def _deps(files: Dict[str, bytes]) -> Tuple[set, set, Dict[str, Any]]:
    py, js, pkg = set(), set(), {}
    for n in ("requirements.txt", "requirements/base.txt", "requirements/prod.txt"):
        for line in _text(files, n).splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.-]+)", line)
            if m and not line.lstrip().startswith(("#", "-")):
                py.add(m.group(1).lower().replace("_", "-"))
    pp = _text(files, "pyproject.toml").lower()
    py |= {name for name, _ in PY_FRAMEWORKS if name in pp}
    try:
        pkg = json.loads(_text(files, "package.json") or "{}")
    except ValueError:
        pkg = {}
    if isinstance(pkg, dict):
        js = set((pkg.get("dependencies") or {})) | set((pkg.get("devDependencies") or {}))
    return py, js, pkg if isinstance(pkg, dict) else {}


def _framework(files: Dict[str, bytes], py: set, js: set) -> Tuple[str, str]:
    if "manage.py" in files and any(n.endswith("settings.py") for n in files):
        return "Django", "python"
    for key, label in PY_FRAMEWORKS:
        if key in py:
            return label, "python"
    for key, label in JS_FRAMEWORKS:
        if key in js:
            return label, "node"
    if "composer.json" in files or any(n.endswith(".php") for n in files):
        return "PHP", "php"
    if "Gemfile" in files:
        return "Ruby on Rails", "ruby"
    if "go.mod" in files:
        return "Go", "go"
    if "pom.xml" in files or "build.gradle" in files:
        return "Java", "java"
    code = "\n".join(_text(files, n)[:20_000] for n in files if n.endswith(".py"))[:300_000]
    for key, label in PY_FRAMEWORKS:                       # no requirements file: go by what the code imports
        if re.search(rf"^\s*(from|import)\s+{key}\b", code, re.M):
            return label, "python"
    if any(n.endswith(".py") for n in files):
        return "Plain Python", "python"
    if any(n.endswith(".html") for n in files):
        return "Static HTML/CSS/JS", "static"
    return "Unknown", ""


def _start(files: Dict[str, bytes], pkg: Dict[str, Any], language: str) -> Dict[str, Any]:
    from llm_agent import runspec
    scripts = pkg.get("scripts") or {}
    out: Dict[str, Any] = {"command": "", "from": "", "listens_on_port_env": False}
    if "run.json" in files:
        try:
            out.update(command=runspec.parse_start(_text(files, "run.json"), files.keys()), **{"from": "run.json"})
        except ValueError as e:
            out["problem"] = str(e)
    elif "Procfile" in files:
        m = re.search(r"^web:\s*(.+)$", _text(files, "Procfile"), re.M)
        if m:
            out.update(command=m.group(1).strip(), **{"from": "Procfile"})
    elif language == "python":
        try:
            out.update(command=runspec.parse_start(None, files.keys()), **{"from": "default file name"})
        except ValueError as e:
            out["problem"] = str(e)
    elif language == "node" and isinstance(scripts.get("start"), str):
        out.update(command="npm start", **{"from": "package.json scripts.start"})
    code = "\n".join(_text(files, n) for n in files if n.endswith((".py", ".js", ".mjs", ".ts")) and n.count("/") <= 2)[:400_000]
    out["listens_on_port_env"] = bool(re.search(r"environ(\.get)?\s*[\[(]\s*['\"]PORT['\"]|process\.env\.PORT", code))
    return out


def _database(files: Dict[str, bytes], py: set, js: set) -> Dict[str, Any]:
    blob = " ".join(py | js).lower() + "\n" + "\n".join(_text(files, n) for n in files if n.endswith((".py", ".js", ".mjs", ".ts", ".env", ".example", ".yml", ".yaml", ".toml", ".json")) and len(files[n]) < 200_000)[:500_000].lower()
    engines = [name for name, pat in DATABASES if re.search(pat, blob)]
    orm = next((name for name, pat in ORMS if re.search(pat, " ".join(py | js).lower())), "")
    services = []
    for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if n in files:
            services = [s for s in re.findall(r"^\s{2}([a-z0-9_-]+):\s*$", _text(files, n), re.M) if s not in ("services", "volumes", "networks")]
    return {"engines": engines, "orm": orm, "compose_services": services}


def _admin(files: Dict[str, bytes], py: set, js: set) -> Dict[str, Any]:
    blob = " ".join(py | js) + "\n" + "\n".join(_text(files, n) for n in files if n.endswith((".py", ".js", ".mjs", ".ts", ".json")) and len(files[n]) < 200_000)[:600_000]
    kind, path = "", "/admin"
    api = any(re.search(r"/admin-api\b", _text(files, n)) for n in files if n.endswith((".py", ".js", ".mjs", ".ts")) and len(files[n]) < 200_000)
    if api and "admin.json" in files and '"collections"' in _text(files, "admin.json"):
        kind, path = "Admin API for the dashboard", "/admin-api"
    for needle, label, p in ([] if kind else ADMIN_HINTS):
        if needle in blob:
            kind, path = label, p
            break
    if not kind:
        hit = ROUTE_ADMIN.search(blob)
        if hit or any(re.search(r"(^|/)admin[^/]*\.(html|jinja2?|ejs|hbs)$", n, re.I) for n in files):
            kind = "Admin pages in the project"
            path = "/" + (hit.group(1).lower() if hit else "admin")
    defaults = []
    for n, b in files.items():
        if n.endswith((".py", ".js", ".mjs", ".ts", ".json", ".env", ".example", ".yml", ".yaml")):
            m = DEFAULT_PW.search(b[:200_000].decode("utf-8", "replace"))
            if m:
                defaults.append(n)
    trusts = any("x-workbench-admin" in b[:200_000].decode("utf-8", "replace").lower() for n, b in files.items() if n.endswith((".py", ".js", ".mjs", ".ts")))
    return {"exists": bool(kind), "kind": kind, "path": path if kind else "", "default_passwords_in": defaults[:6], "trusts_manage_login": trusts}


def _content(files: Dict[str, bytes]) -> Dict[str, Any]:
    data_files = [n for n, b in files.items() if (n.lower().endswith((".json", ".csv", ".yml", ".yaml")) and n.rsplit("/", 1)[-1] not in SKIP_JSON
                                                 and (n.startswith(DATA_DIRS) or "/" not in n) and len(b) < 400_000)][:12]
    hard = []
    for n, b in files.items():
        if n.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".vue", ".svelte")) and len(b) < 400_000:
            if len(OBJECT_ITEM.findall(b.decode("utf-8", "replace"))) >= 3:
                hard.append(n)
    return {"data_files": data_files, "hard_coded_in": hard[:12], "manage_json": "manage.json" in files}


def audit(files: Dict[str, bytes]) -> Dict[str, Any]:
    py, js, pkg = _deps(files)
    framework, language = _framework(files, py, js)
    start = _start(files, pkg, language)
    scripts = pkg.get("scripts") or {}
    needs_build = bool(language == "node" and isinstance(scripts.get("build"), str) and "index.html" not in files)
    env_vars = sorted({g for n, b in files.items() if n.endswith((".py", ".js", ".mjs", ".ts")) for m in ENV_NAME.finditer(b[:200_000].decode("utf-8", "replace")) for g in m.groups() if g})[:30]
    db, admin, content = _database(files, py, js), _admin(files, py, js), _content(files)
    if language in ("python", "static"):
        support, why = "yes", ""
    elif language == "node" and (needs_build or framework in ("React", "Vue", "Svelte", "Angular", "Astro", "Gatsby")) and framework not in SERVER_JS:
        support, why = "build", "It is a JavaScript front end that is built with npm, then served as static files."
    elif language == "node":
        support, why = "node", f"{framework} needs a Node server. Hosting supports it only through the Node image."
    else:
        support, why = "no", f"{framework} projects cannot be hosted here: hosting runs Python apps, static sites and built JavaScript front ends."
    notes: List[str] = []
    if len(files) > 600:
        notes.append("It is a large project. Change only the files the request needs.")
    if db["compose_services"] and len(db["compose_services"]) > 1:
        notes.append("It expects extra services (" + ", ".join(db["compose_services"]) + "), which the platform provides separately or not at all.")
    if admin["default_passwords_in"]:
        notes.append("It has a default admin password in " + ", ".join(admin["default_passwords_in"]) + ". Remove it.")
    return {"framework": framework, "language": language, "files": len(files), "start": start, "needs_build": needs_build, "hosting": {"support": support, "why": why},
            "database": db, "admin": admin, "content": content, "env_vars": env_vars, "notes": notes}


def render_md(report: Dict[str, Any], folder: str) -> str:
    r, s, d, a, c = report, report["start"], report["database"], report["admin"], report["content"]
    lines = [MARK, f"# Project: {folder or '.'}", "",
             "This file was written by analyze_project. Keep it up to date when the plan changes. The project is changed in place: keep its framework and folder structure.", "",
             f"- **Framework:** {r['framework']} ({r['language'] or 'unknown'}), {r['files']} files",
             f"- **Starts with:** `{s['command'] or 'unknown'}`" + (f" (from {s['from']})" if s["from"] else "") + ("" if s["listens_on_port_env"] else " - does not read the PORT variable yet"),
             f"- **Needs a build step:** {'yes' if r['needs_build'] else 'no'}",
             f"- **Can be hosted here:** {r['hosting']['support']}" + (f" - {r['hosting']['why']}" if r["hosting"]["why"] else ""),
             f"- **Database:** {', '.join(d['engines']) or 'none found'}" + (f" via {d['orm']}" if d["orm"] else ""),
             f"- **Admin:** {a['kind'] + ' at ' + a['path'] if a['exists'] else 'none found'}" + (" - trusts the Manage login" if a["trusts_manage_login"] else (" - NOT yet tied to the Manage login" if a["exists"] else "")),
             f"- **Content files:** {', '.join(c['data_files']) or 'none'}",
             f"- **Content written inside code:** {', '.join(c['hard_coded_in']) or 'none found'}",
             f"- **Settings it reads:** {', '.join(r['env_vars']) or 'none'}", ""]
    if r["notes"]:
        lines += ["## Watch out", *[f"- {n}" for n in r["notes"]], ""]
    lines += ["## Plan", "- (the assistant writes here what it will change, file by file)", ""]
    return "\n".join(lines)
