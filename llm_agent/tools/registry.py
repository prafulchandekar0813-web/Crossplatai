"""Tool definitions, argument validation and the handlers agents can call.

Each tool carries:
  parameters  JSON schema sent to the model (also used to validate the model's arguments)
  describe    one-line human description of a call, shown live in the UI
  summarize   one-line human description of the result, shown in the UI
"""
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from llm_agent.config import SystemConfig
from llm_agent import buildcheck
from llm_agent import appkit, project_audit, project_gate, readiness
from llm_agent.tools.file_manager import WorkspaceError

Handler = Callable[["ToolContext", Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass
class ToolContext:
    user_id: str
    cfg: SystemConfig
    files: Any          # WorkspaceFiles
    kb: Any             # KnowledgeBase
    search: Any         # WebSearch
    fetch: Any          # WebFetch
    memory: Any         # MemoryStore
    rc: Any             # shared per-task state (core.agent.RunContext)
    agent_id: str
    emit: Callable[..., Awaitable[None]]
    delegate: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None
    shell: Any = None    # llm_agent.shell.ShellManager
    session_id: str = ""
    scrape: Any = None   # llm_agent.tools.web_scrape.Scraper


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Handler
    describe: Callable[[Dict[str, Any]], str]
    summarize: Callable[[Dict[str, Any]], str]
    kinds: tuple = ("orchestrator", "worker")
    needs_search: bool = False
    needs_shell: bool = False
    needs_scrape: bool = False
    timed: bool = True   # delegations run as long as the worker needs; everything else gets a timeout

    PATH_ALIASES = ("file_path", "filepath", "file", "filename", "file_name", "filePath")

    def check_args(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Fix the usual argument slips (path called file_path...) and name what is missing, instead of crashing with a KeyError."""
        props = self.parameters.get("properties", {})
        if not isinstance(args, dict):
            return {}, "The tool arguments must be a JSON object like {\"path\": \"app.py\"}"
        args = dict(args)
        if "path" in props and "path" not in args:
            for alias in self.PATH_ALIASES:
                if alias in args:
                    args["path"] = args.pop(alias)
                    break
        self._single_item_shorthand(args)
        for key, value in list(args.items()):         # text where a list or number was meant: JSON "[...]", "12", 12.0
            kind = props.get(key, {}).get("type")
            if kind == "array" and isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
                args[key] = [value] if isinstance(value, dict) else value
            elif kind == "array" and isinstance(value, dict):
                args[key] = [value]
            elif kind == "integer" and isinstance(value, (str, float)) and not isinstance(value, bool):
                if isinstance(value, float) and value.is_integer():
                    args[key] = int(value)
                elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    args[key] = int(value)
        missing = [k for k in self.parameters.get("required", []) if k not in args]
        if missing:
            return args, (f"Missing required argument{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
                          f"{self.name} takes: {', '.join(props)} (required: {', '.join(self.parameters.get('required', []))}).")
        return args, ""

    def _single_item_shorthand(self, args: Dict[str, Any]) -> None:
        """One edit sent flat ({path, old, new}) instead of inside the list the tool asks for."""
        from llm_agent.tools.file_manager import ALIASES
        if self.name == "patch_file" and "edits" not in args and any(k in args for k in ALIASES["old"]):
            item = {"old": next(args.pop(k) for k in ALIASES["old"] if k in args)}
            item["new"] = next((args.pop(k) for k in ALIASES["new"] if k in args), "")
            if "replace_all" in args:
                item["replace_all"] = args.pop("replace_all")
            args["edits"] = [item]
        elif self.name == "edit_lines" and "changes" not in args and any(k in args for k in ("start_line", "start", "startLine")):
            item = {"start_line": next(args.pop(k) for k in ("start_line", "start", "startLine") if k in args)}
            end = next((args.pop(k) for k in ("end_line", "end", "endLine") if k in args), None)
            if end is not None:
                item["end_line"] = end
            item["new_text"] = next((args.pop(k) for k in ("new_text", "text", "newText", "content") if k in args), "")
            args["changes"] = [item]

    def definition(self) -> Dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                   "parameters": self.parameters}}


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _s(args: Dict[str, Any], key: str, default: str = "") -> str:
    v = args.get(key, default)
    return v if isinstance(v, str) else default


def _num(v: Any, default: int = 0) -> int:
    """A whole number from whatever the model sent ("12", 12.0, null, "abc" -> default)."""
    try:
        return max(0, int(float(v))) if v not in (None, "", True, False) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _as_list(v: Any) -> List[Any]:
    """Models send null, one string or a list for list-like arguments; accept all three."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v if x is not None]
    if isinstance(v, str):
        return [p.strip() for p in v.splitlines() if p.strip()] or ([v.strip()] if v.strip() else [])
    return [v]


def _n(v: Any) -> int:
    return len(v) if isinstance(v, (list, tuple, dict)) else 0


def _err(result: Dict[str, Any]) -> Optional[str]:
    return f"Failed: {result.get('error')}" if result.get("status") == "error" else None


# ---- handlers ---------------------------------------------------------------------------------------
async def _plan_task(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    plan = {"title": (_s(a, "task_title") or _s(a, "title")).strip()[:120] or "Plan",
            "steps": [str(x)[:240] for x in _as_list(a.get("steps"))][:30],
            "files": [str(x)[:200] for x in _as_list(a.get("files"))][:50],
            "look_up": [str(x)[:200] for x in _as_list(a.get("look_up"))][:10]}
    ctx.rc.plan = plan
    await ctx.emit("plan", **plan)
    return {"status": "success", "message": "Plan recorded. Now carry it out step by step."}


async def _write_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, [a.get("path")])
    res = await asyncio.to_thread(ctx.files.write, a["path"], a["content"])
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="created" if res["created"] else "updated", bytes=res["bytes"])
    return res


async def _patch_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, [a.get("path")])
    res = await asyncio.to_thread(ctx.files.patch, a["path"], a["edits"])
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="updated", bytes=res["bytes"])
    return res


async def _batch_result(ctx: ToolContext, res: Dict[str, Any]) -> Dict[str, Any]:
    for f in res["files"]:
        ctx.rc.written_files.add(f["path"])
        await ctx.emit("file_changed", path=f["path"], action="created" if f.get("created") else "updated", bytes=f["bytes"])
    return res


async def _patch_files(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, project_gate.paths_of(a))
    return await _batch_result(ctx, await asyncio.to_thread(ctx.files.patch_many, a["files"]))


async def _write_files(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, project_gate.paths_of(a))
    return await _batch_result(ctx, await asyncio.to_thread(ctx.files.write_many, a["files"]))


async def _move_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, [a.get("from"), a.get("to")])
    res = await asyncio.to_thread(ctx.files.move, _s(a, "from"), _s(a, "to"), bool(a.get("overwrite")))
    for old in res["old_paths"]:
        ctx.rc.written_files.discard(old)
        await ctx.emit("file_changed", path=old, action="deleted", bytes=0)
    for new in res["new_paths"]:
        ctx.rc.written_files.add(new)
        await ctx.emit("file_changed", path=new, action="created", bytes=0)
    return {k: v for k, v in res.items() if k not in ("old_paths", "new_paths")}


async def _edit_lines(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, [a.get("path")])
    res = await asyncio.to_thread(ctx.files.edit_lines, a["path"], a["changes"])
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="updated", bytes=res["bytes"])
    return res


async def _insert_text(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, [a.get("path")])
    if a.get("inserts") is not None:
        res = await asyncio.to_thread(ctx.files.insert_many, a["path"], a["inserts"])
    elif a.get("text") is None or not a.get("where"):
        raise WorkspaceError("Give 'text' and 'where', or a list of 'inserts' (each with text and where)")
    else:
        res = await asyncio.to_thread(ctx.files.insert, a["path"], a["text"], _s(a, "where"), a.get("line"), a.get("anchor"), bool(a.get("paragraph")))
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="updated", bytes=res["bytes"])
    return res


async def _check_app_name(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    try:
        name = appkit.check_name(_s(a, "name"))
    except appkit.KitError as e:
        return {"available": False, "error": str(e)}
    if await ctx.rc.apps.name_taken(name):
        return {"available": False, "name": name, "message": f"'{name}' is already used by another app. App names are unique across all users. Choose another name (add a place or a word of the owner's) and check it again."}
    return {"available": True, "name": name, "message": "This name is free. It is only reserved when the app is created, so build soon."}


async def _check_build(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    def collect() -> Dict[str, str]:
        root = ctx.files.resolve(_s(a, "path"), allow_root=True)
        if not root.is_dir():
            raise WorkspaceError("path must be the project folder (use the folder that holds app.py)")
        skip = {"venv", ".venv", "node_modules", "__pycache__", ".git", "data", "build", "dist"}
        out: Dict[str, str] = {}
        for f in sorted(root.rglob("*")):
            rel = f.relative_to(root)
            if f.is_file() and not (set(rel.parts[:-1]) & skip) and f.stat().st_size <= 300_000 and len(out) < 400:
                try:
                    out[rel.as_posix()] = f.read_text("utf-8")
                except (UnicodeDecodeError, OSError):
                    pass
        return out
    def collect_bytes() -> Dict[str, bytes]:
        root, out = ctx.files.resolve(_s(a, "path"), allow_root=True), {}
        for f in sorted(root.rglob("*")):
            rel = f.relative_to(root)
            if f.is_file() and not (set(rel.parts[:-1]) & {"venv", ".venv", "node_modules", "__pycache__", ".git", "data", "build", "dist", ".next"}) and f.stat().st_size <= 400_000 and len(out) < 1500:
                out[rel.as_posix()] = f.read_bytes()
        return out
    result = buildcheck.check(await asyncio.to_thread(collect), a.get("platforms"))
    ready = await asyncio.to_thread(lambda: readiness.check(collect_bytes()))
    if ready["applies"]:                    # a copied repository: the build and Manage app stay locked until every required check passes
        result = {**result, "ready_check": ready["checks"], "ok": result["ok"] and ready["ready"], "problems": list(result["problems"]) + ready["missing"]}
    return result


def _project_summary(rep: Dict[str, Any]) -> str:
    if not rep:
        return "Project read"
    return f"{rep.get('framework', '?')}, database: {', '.join(rep.get('database', {}).get('engines', [])) or 'none'}, admin: {'yes' if rep.get('admin', {}).get('exists') else 'no'}"


async def _analyze_project(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    def collect() -> Tuple[str, Dict[str, bytes]]:
        root = ctx.files.resolve(_s(a, "path"), allow_root=True)
        if not root.is_dir():
            raise WorkspaceError("path must be the project folder (the folder that was copied, for example 'menu-ui')")
        skip = {"venv", ".venv", "node_modules", "__pycache__", ".git", "build", "dist", ".next"}
        out: Dict[str, bytes] = {}
        for f in sorted(root.rglob("*")):
            rel = f.relative_to(root)
            if f.is_file() and not (set(rel.parts[:-1]) & skip) and f.stat().st_size <= 400_000 and len(out) < 1500 and rel.as_posix() != "PROJECT.md":
                out[rel.as_posix()] = f.read_bytes()
        return files_rel(root), out
    def files_rel(root) -> str:
        return "" if root == ctx.files.root else ctx.files.rel(root)
    folder, files = await asyncio.to_thread(collect)
    if not files:
        raise WorkspaceError("That folder has no files to analyse.")
    report = await asyncio.to_thread(project_audit.audit, files)
    doc = (folder + "/" if folder else "") + "PROJECT.md"
    await asyncio.to_thread(ctx.files.write, doc, project_audit.render_md(report, folder))
    ctx.rc.written_files.add(doc)
    await ctx.emit("file_changed", path=doc, action="created", bytes=0)
    return {"status": "success", "project_md": doc, "report": report,
            "next": ("PROJECT.md is written and the project may now be edited. Tell the user in 3-4 plain lines what it is (framework, where its content lives, its admin and database). "
                     "Then change the project IN PLACE: keep its framework and folder structure, edit its own files, add nothing in a parallel folder. "
                     + ("It has no admin of its own: make its content editable in Manage app with manage.json. " if not report["admin"]["exists"] else
                        "It has its own admin: tie it to the Manage login and remove any default password (use_skill admin-access). ")
                     + ("It cannot be hosted here (" + report["hosting"]["why"] + "). Tell the user and offer another project." if report["hosting"]["support"] == "no" else ""))}


async def _read_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    limit = max(500, int(ctx.cfg.context.max_tool_output_length * 0.8))
    res = await asyncio.to_thread(ctx.files.read, a["path"], _num(a.get("offset")), limit, bool(a.get("line_numbers")))
    if res.get("binary"):
        res["note"] = "Binary file; its content is not shown."
    elif res["next_offset"] is not None:
        res["note"] = f"Showing part of the file. Call read_file again with offset={res['next_offset']} for the rest."
    return res


async def _list_files(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(ctx.files.list, _s(a, "path"), 200)


async def _search_files(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(ctx.files.search, _s(a, "pattern"), _s(a, "path"))


async def _delete_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    project_gate.check(ctx.files, [a.get("path")])
    res = await asyncio.to_thread(ctx.files.delete, _s(a, "path"))
    ctx.rc.written_files.discard(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="deleted", bytes=0)
    return res


async def _library_search(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    lib = getattr(ctx.rc, "library", None)
    if lib is None:
        return {"status": "success", "projects": [], "note": "The shared library is not set up."}
    found = await asyncio.to_thread(lib.search, _s(a, "query"))
    return {"status": "success", "projects": found, "next": ("Copy one with github_clone using its repo (instant, no download); it was already checked." if found else
                                                              "Nothing saved fits. Use github_search to find an open source project.")}


async def _bash(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    from llm_agent.shell import ShellError
    try:
        if a.get("background"):
            run = await ctx.shell.start_app(ctx.user_id, ctx.session_id, ctx.files, _s(a, "cwd"), _s(a, "command"))
            probe = await ctx.shell.probe(run["id"], "/")
            await _record_run(ctx, _s(a, "cwd"), probe.get("status"))
            return {"status": "running", "http_status": probe.get("status"), "page_start": probe.get("text", "")[:400],
                    "log": ctx.shell.logs(run["id"])[-1500:],
                    "note": "The app is running and the user can see it under Preview app > Run app. Fix any error shown above and start it again. "
                            "It stops after a while or with stop_app."}
        return await ctx.shell.run(ctx.user_id, ctx.session_id, ctx.files, _s(a, "cwd"), _s(a, "command"), _num(a.get("timeout"), 60))
    except ShellError as e:
        return {"error": e.message}


async def _record_run(ctx: ToolContext, folder: str, status: Any) -> None:
    """A copied project whose page answered in the sandbox earns the build gate's proof (the server records it, not the assistant)."""
    if getattr(ctx.rc, "proofs", None) is None or not isinstance(status, int) or status >= 500:
        return

    def fingerprint() -> str:
        root = ctx.files.resolve(folder, allow_root=True)
        if not (root / "SOURCE.md").is_file():
            return ""
        files = {f.relative_to(root).as_posix(): f.read_bytes() for f in sorted(root.rglob("*")) if f.is_file() and f.stat().st_size <= 5_000_000 and not appkit.is_skipped(f.relative_to(root).as_posix())}
        return readiness.digest(files)
    try:
        digest = await asyncio.to_thread(fingerprint)
    except (OSError, WorkspaceError):
        return
    if digest:
        await ctx.rc.proofs.record(ctx.user_id, digest)


async def _stop_app(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    cur = ctx.shell.current(ctx.user_id, ctx.session_id)
    if cur:
        await ctx.shell.stop_app(cur["id"])
    return {"stopped": bool(cur)}


async def _web_search(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.search.search(a["query"], _s(a, "recency"))


async def _research_web(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    from llm_agent.tools.web_research import WebResearch
    return await WebResearch(ctx.search, ctx.fetch).run(_s(a, "query"), a.get("pages") or 3, _s(a, "recency"))


async def _read_url(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    res = await ctx.fetch.read(_s(a, "url"), a.get("offset") or 0, bool(a.get("links")))
    unreadable = res.get("status") == "error" or not (res.get("text") or res.get("content") or "").strip()
    scraper = getattr(ctx, "scrape", None)
    if unreadable and scraper is not None and scraper.enabled and not a.get("offset") and getattr(ctx.rc, "scrapes", 0) < ctx.cfg.scrape_task_limit:
        got = await _scrape_page(ctx, {"url": _s(a, "url")})        # the plain reader could not get the page (JavaScript, blocked): the page scraper opens it
        if got.get("status") == "success":
            return {**got, "note": ("The plain reader could not read this page, so the page scraper opened it. " + got.get("note", "")).strip()}
    return res


async def _scrape_page(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    used = getattr(ctx.rc, "scrapes", 0)
    offset = a.get("offset") or 0
    if not offset and used >= ctx.cfg.scrape_task_limit:       # continuing an already scraped page is free; a new scrape is not
        return {"status": "error", "error": f"Only {ctx.cfg.scrape_task_limit} scrapes are allowed per task (the monthly allowance is small). Use read_url or web_search."}
    res = await ctx.scrape.scrape(ctx.user_id, _s(a, "url"), offset, a.get("wait_ms") or 0)
    if res.get("status") == "success" and not res.get("note", "").startswith("Served from"):
        ctx.rc.scrapes = used + 1
    return res


async def _github_search(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.rc.github.search(_s(a, "query"), _s(a, "language"))


async def _github_clone(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.rc.github.clone(ctx.files, _s(a, "repo"), _s(a, "dest"), _s(a, "subdir"), ctx.user_id, ctx.rc.review_ask, ctx.rc.approvals, _s(a, "category"))


async def _use_skill(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    skills = getattr(ctx.rc, "skills", None)
    if skills is None:
        return {"status": "error", "error": "No skills are available."}
    name = str(a.get("name", "")).strip().lower()
    got = await skills.get(name, ctx.user_id)
    if not got:
        names = ", ".join(n for n, _ in await skills.catalog(ctx.user_id))
        return {"status": "error", "error": f"There is no skill called '{name}'. Available: {names}."}
    return {"status": "success", "name": got["name"], "guide": got["body"],
            "note": "Follow this guide for this part of the work. It is a starting point to adapt, not text to paste."}


async def _flag_knowledge(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    from llm_agent.flags import FlagError, MAX_PER_TASK
    rc = ctx.rc
    if not rc.share or rc.flags is None:
        return {"status": "success", "note": "This user has switched recording off, so nothing was saved. Carry on."}
    if rc.flags_made >= MAX_PER_TASK:
        return {"status": "success", "note": f"Enough flags for this task (at most {MAX_PER_TASK}). Carry on."}
    try:
        await rc.flags.record(str(a.get("kind", "")), str(a.get("topic", "")), str(a.get("note", "")), str(a.get("suggestion", "")), str(a.get("tool", "")))
    except FlagError as e:
        return {"status": "error", "error": e.message}
    rc.flags_made += 1
    return {"status": "success", "note": "Flag recorded for the people who maintain the guides. Carry on with the task."}


async def _kb_search(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    if not _s(a, "query").strip():
        return {"status": "error", "error": "query is empty"}
    return await asyncio.to_thread(ctx.kb.search, ctx.user_id, a["query"])


async def _remember(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    try:
        saved = await ctx.memory.add_fact(ctx.user_id, a["fact"], _s(a, "category", "general"), "agent",
                                          ctx.cfg.memory.max_long_term_facts)
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    if saved["created"]:
        await ctx.emit("memory_saved", fact=saved["fact"], category=saved["category"])
    return {"status": "success", "saved": saved["created"], "fact": saved["fact"],
            "note": "Stored." if saved["created"] else "Already remembered."}


async def _save_note(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    try:
        saved = await ctx.rc.notes.add(ctx.user_id, _s(a, "title"), _s(a, "body"), ctx.session_id or "", "agent")
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    await ctx.emit("memory_saved", fact=f"Note: {saved['title']}", category="note")
    return {"status": "success", "id": saved["id"], "note": "Updated the existing note." if saved["updated"] else "Saved."}


async def _read_notes(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    found = await ctx.rc.notes.search(ctx.user_id, _s(a, "query"), 3)
    return {"status": "success", "notes": [{"id": n["id"], "title": n["title"], "body": n["body"]} for n in found],
            **({} if found else {"note": "No saved note matches."})}


async def _delegate(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    if ctx.delegate is None:
        return {"status": "error", "error": "Delegation is not available to this agent."}
    if not _s(a, "task").strip():
        return {"status": "error", "error": "task must not be empty"}
    return await ctx.delegate(a)


# ---- registry ---------------------------------------------------------------------------------------
def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def build_registry() -> Dict[str, ToolSpec]:
    specs = [
        ToolSpec(
            "plan_task",
            "Record a step-by-step plan before doing multi-step work (building an app, several files, research). "
            "Call it first, then carry the plan out. In 'look_up' list ONLY the specific parts of the task that depend on current information "
            "(for example 'current Flask 3 file-upload API', 'latest Flutter stable version'); research just those before writing anything. "
            "Leave it empty when nothing in the task can have changed.",
            _obj({"task_title": {"type": "string", "description": "Short title of the task"},
                  "steps": {"type": "array", "items": {"type": "string"}, "description": "Ordered steps"},
                  "files": {"type": "array", "items": {"type": "string"}, "description": "Paths of files you expect to create"},
                  "look_up": {"type": "array", "items": {"type": "string"}, "description": "Specific facts, APIs or versions that need a current web lookup"}},
                 ["steps"]),
            _plan_task,
            lambda a: f"Plan: {_s(a, 'task_title') or _s(a, 'title') or 'steps'}",
            lambda r: _err(r) or "Plan recorded",
            kinds=("orchestrator",)),
        ToolSpec(
            "delegate_task",
            "Hand an independent sub-task to a worker agent that has its own tools (files, search). Use it for separable "
            "pieces of a bigger job. Several delegate_task calls in the same turn run in parallel. The worker replies "
            "with a short report of what it did and which files it wrote.",
            _obj({"task": {"type": "string", "description": "Complete, self-contained instructions for the worker"},
                  "role": {"type": "string", "description": "Short label, e.g. 'Backend developer'"},
                  "context": {"type": "string", "description": "Anything the worker needs to know (file names, decisions, API shapes)"}},
                 ["task"]),
            _delegate,
            lambda a: f"Delegate to {_s(a, 'role') or 'worker'}: {_s(a, 'task')[:90]}",
            lambda r: _err(r) or f"{r.get('agent', 'worker')} {r.get('status', 'finished')}: {str(r.get('summary', ''))[:100]}",
            kinds=("orchestrator",), timed=False),
        ToolSpec(
            "write_file",
            "Create or overwrite a text file in your workspace. Missing folders are created. Paths are relative to the "
            "workspace root (for example 'app/main.py').",
            _obj({"path": {"type": "string", "description": "File path relative to the workspace root"},
                  "content": {"type": "string", "description": "Complete file content"}}, ["path", "content"]),
            _write_file,
            lambda a: f"Write {_s(a, 'path')}",
            lambda r: _err(r) or f"{'Created' if r.get('created') else 'Updated'} {r.get('path')} ({fmt_bytes(r.get('bytes', 0))})"),
        ToolSpec(
            "patch_file",
            "Change part of an existing text file without rewriting it. Each edit replaces one exact piece of text ('old') with "
            "new text ('new'); 'old' must match the file exactly and be unique (add surrounding lines if needed, or set "
            "replace_all). All edits apply together or not at all. Prefer this over write_file for small changes (use edit_lines instead when you know the line numbers); read_file first "
            "so you copy the text exactly.",
            _obj({"path": {"type": "string"},
                  "edits": {"type": "array", "description": "List of {old, new, replace_all?} objects, applied in order",
                            "items": {"type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
                                      "required": ["old", "new"]}}}, ["path", "edits"]),
            _patch_file,
            lambda a: f"Patch {_s(a, 'path')}",
            lambda r: _err(r) or f"Patched {r.get('path')} ({r.get('replacements', 0)} change{'s' if r.get('replacements') != 1 else ''})"),
        ToolSpec(
            "patch_files",
            "Patch several files in one call (up to 20). 'files' is a list of {path, edits}; each edit is {old, new, replace_all?} exactly as in "
            "patch_file. Every file is checked first: if any edit fails, nothing is changed. Small whitespace/indentation differences in 'old' are tolerated.",
            _obj({"files": {"type": "array", "description": "List of {path, edits}", "items": {"type": "object", "properties": {
                "path": {"type": "string"},
                "edits": {"type": "array", "items": {"type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["old", "new"]}}},
                "required": ["path", "edits"]}}}, ["files"]),
            _patch_files,
            lambda a: f"Patch {_n(a.get('files'))} files",
            lambda r: _err(r) or f"Patched {len(r.get('files', []))} files ({r.get('replacements', 0)} changes)"),
        ToolSpec(
            "write_files",
            "Create or overwrite several text files in one call (up to 20): 'files' is a list of {path, content}. Sizes and quota are checked for all "
            "files before any is written. Use this to scaffold a project in one step.",
            _obj({"files": {"type": "array", "description": "List of {path, content}", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}}, ["files"]),
            _write_files,
            lambda a: f"Write {_n(a.get('files'))} files",
            lambda r: _err(r) or f"Wrote {len(r.get('files', []))} files ({fmt_bytes(r.get('bytes', 0))})"),
        ToolSpec(
            "insert_text",
            "Insert new lines or a whole paragraph/block into an existing text file without touching anything else. 'where' picks the "
            "place: start, end, after_line / before_line (give 'line', the number from read_file with line_numbers), or after_text / "
            "before_text (give 'anchor': text that appears on exactly one line; the new text goes on the line after/before it). "
            "Set paragraph=true to keep a blank line around the new block (prose, markdown, docstrings). For several insertions in one file, "
            "send 'inserts': a list of {text, where, line?, anchor?, paragraph?}; all positions refer to the file as it is now.",
            _obj({"path": {"type": "string"},
                  "inserts": {"type": "array", "description": "Several insertions in one call", "items": {"type": "object", "properties": {
                      "text": {"type": "string"}, "where": {"type": "string"}, "line": {"type": "integer"}, "anchor": {"type": "string"}, "paragraph": {"type": "boolean"}}, "required": ["text", "where"]}},
                  "text": {"type": "string", "description": "The lines to insert (may contain several lines)"},
                  "where": {"type": "string", "description": "start | end | after_line | before_line | after_text | before_text"},
                  "line": {"type": "integer", "description": "Line number for after_line / before_line"},
                  "anchor": {"type": "string", "description": "Text on one line, for after_text / before_text"},
                  "paragraph": {"type": "boolean", "description": "Surround the block with blank lines"}}, ["path"]),
            _insert_text,
            lambda a: f"Insert into {_s(a, 'path')} ({_s(a, 'where').replace('_', ' ')})",
            lambda r: _err(r) or f"Inserted into {r.get('path')} at line {r.get('inserted_at_line', '?')} (now {r.get('total_lines', '?')} lines)"),
        ToolSpec(
            "edit_lines",
            "Change specific lines of an existing text file by line number, without rewriting the rest. First call read_file with "
            "line_numbers=true. Each change is {start_line, end_line, new_text}: lines start_line..end_line (inclusive) are replaced "
            "by new_text; empty new_text deletes them; end_line = start_line - 1 inserts new_text before start_line (use the last line "
            "number + 1 to append). All numbers refer to the file as you last read it, and all changes apply together. Do not include "
            "the line-number prefixes in new_text.",
            _obj({"path": {"type": "string"},
                  "changes": {"type": "array", "description": "List of {start_line, end_line, new_text}", "items": {"type": "object", "properties": {"start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "new_text": {"type": "string"}},
                                        "required": ["start_line", "new_text"]}}},
                 ["path", "changes"]),
            _edit_lines,
            lambda a: f"Edit lines in {_s(a, 'path')}",
            lambda r: _err(r) or f"Edited {r.get('path')} ({r.get('changes', 0)} change{'s' if r.get('changes') != 1 else ''}, now {r.get('total_lines', '?')} lines)"),
        ToolSpec(
            "move_file",
            "Move or rename a file or a whole folder in your workspace, for example into or out of a folder. 'to' is the new path; if it is an existing "
            "folder (or ends with /) the item goes inside it, so move_file(from='app.py', to='backend/') puts it in backend/ and "
            "move_file(from='backend/app.py', to='.') takes it out to the top. It never replaces an existing file unless overwrite is true. "
            "Afterwards fix any imports or links that used the old path.",
            _obj({"from": {"type": "string", "description": "Existing file or folder"}, "to": {"type": "string", "description": "New path, or a folder to move it into ('.' = workspace root)"},
                  "overwrite": {"type": "boolean", "description": "Replace an existing file at the new path (default false)"}}, ["from", "to"]),
            _move_file,
            lambda a: f"Move {_s(a, 'from')[:60]} to {_s(a, 'to')[:60]}",
            lambda r: _err(r) or f"Moved {r.get('from')} to {r.get('to')}" + (f" ({r.get('moved')} files)" if r.get("folder") else "")),
        ToolSpec(
            "read_file",
            "Read a text file from your workspace. Long files are returned in parts; use 'offset' to continue.",
            _obj({"path": {"type": "string"}, "offset": {"type": "integer", "description": "Character offset to start from (default 0)"},
                  "line_numbers": {"type": "boolean", "description": "Prefix every line with its line number (use before edit_lines)"}},
                 ["path"]),
            _read_file,
            lambda a: f"Read {_s(a, 'path')}",
            lambda r: _err(r) or (f"Read {r.get('path')} ({r.get('total_chars', 0)} characters"
                                  + (", more available)" if r.get("next_offset") is not None else ")"))),
        ToolSpec(
            "search_files",
            "Find text in your files (a case-insensitive pattern): returns path, line number and the line. Use it to find where something is (a heading, a price, a route, a function) before you read or edit; "
            "much cheaper than reading whole files.",
            _obj({"pattern": {"type": "string", "description": "Text or regular expression to look for"}, "path": {"type": "string", "description": "Only search under this folder (default: everything)"}}, ["pattern"]),
            _search_files,
            lambda a: f"Search your files for {_s(a, 'pattern')[:50]!r}",
            lambda r: _err(r) or f"{len(r.get('matches', []))} match(es)" + (" (more exist)" if r.get("truncated") else "")),
        ToolSpec(
            "delete_file",
            "Delete one file, or a folder with everything in it, from your workspace. Use it to remove files you no longer need (an old version, sample files, a leftover folder). It cannot be undone.",
            _obj({"path": {"type": "string"}}, ["path"]),
            _delete_file,
            lambda a: f"Delete {_s(a, 'path')[:70]}",
            lambda r: _err(r) or f"Deleted {r.get('path')}"),
        ToolSpec(
            "library_search",
            "Look in the shared library of open source projects that were already copied and safety-checked, before you search GitHub. Describe the app in a few words (restaurant menu, booking, online shop).",
            _obj({"query": {"type": "string"}}, ["query"]),
            _library_search,
            lambda a: "Finding open source project to build this app",
            lambda r: _err(r) or (f"{len(r.get('projects', []))} saved project(s) fit" if r.get("projects") else "None saved yet")),
        ToolSpec(
            "list_files",
            "List files and folders in your workspace (recursively). Leave path empty for the root.",
            _obj({"path": {"type": "string", "description": "Folder to list (default: workspace root)"}}, []),
            _list_files,
            lambda a: f"List files in {_s(a, 'path') or 'workspace'}",
            lambda r: _err(r) or f"{len(r.get('entries', []))} entries"),
        ToolSpec(
            "check_app_name",
            "Check that an app name is free. App names must be unique across ALL users (case, spaces and punctuation ignored). Call this when you pick or are given a name, "
            "before you build the app, and choose another name when it is taken. The server also refuses a taken name when the app is created.",
            _obj({"name": {"type": "string", "description": "The app name (2 to 30 characters)"}}, ["name"]),
            _check_app_name,
            lambda a: f"Check the app name {_s(a, 'name')!r}",
            lambda r: _err(r) or ("Name is free" if r.get("available") else "Name is taken or not allowed")),
        ToolSpec(
            "check_build",
            "Check an app project folder before you tell the user it is ready: finds the framework, a missing start file or requirements.txt, a "
            "missing PORT setting, keys written in the code, root-relative web addresses, a missing .env.example and similar. It returns "
            "{ok, framework, problems, warnings, design}. Fix every problem and read each warning. 'design' is a review of how the screens look (too plain, no icons, no "
            "navigation, not adapted to the platform): treat each line as work to do, then run it again. Pass platforms when the user asked for Android or Windows.",
            _obj({"path": {"type": "string", "description": "The project folder (the one with app.py); empty for the workspace root"},
                  "platforms": {"type": "array", "items": {"type": "string", "enum": ["android", "windows"]},
                                "description": "The platforms the user asked for; adds checks for each platform's layout"}}, []),
            _check_build,
            lambda a: f"Check build readiness of {_s(a, 'path') or 'workspace'}",
            lambda r: _err(r) or (f"Ready ({r.get('framework') or 'no framework found'}), {len(r.get('warnings', []))} warning(s), {len(r.get('design', []))} design note(s)" if r.get("ok")
                                  else f"{len(r.get('problems', []))} problem(s) to fix")),
        ToolSpec(
            "bash",
            "Run a shell command in this chat's own temporary environment, starting in your project folder (the files you wrote). Use it to install packages "
            "(first: python3 -m venv ~/venv && pip install flask), run scripts and tests, and look at errors. ~ is this chat's private home folder. "
            "Dangerous commands are refused. Commands that wait forever (a web server) must use background=true: the app is then started on a port given in the PORT "
            "environment variable, checked, and shown to the user in Preview app > Run app. Only one background app per chat; stop it with stop_app.",
            _obj({"command": {"type": "string", "description": "The command, e.g. python3 -m pytest or python app.py"},
                  "cwd": {"type": "string", "description": "Folder inside your workspace to run in (default: the workspace root)"},
                  "timeout": {"type": "integer", "description": "Seconds before the command is stopped (default 60)"},
                  "background": {"type": "boolean", "description": "true to start a server and keep it running (listen on $PORT)"}},
                 ["command"]),
            _bash,
            lambda a: ("Start in background: " if a.get("background") else "Run: ") + _s(a, "command")[:100],
            lambda r: _err(r) or (f"Running (HTTP {r.get('http_status')})" if r.get("status") == "running"
                                   else f"Exit {r.get('exit_code')}" + (" (timed out)" if r.get("timed_out") else "")),
            needs_shell=True),
        ToolSpec(
            "stop_app",
            "Stop the app you started in the background in this chat.",
            _obj({}, []),
            _stop_app,
            lambda a: "Stop the running app",
            lambda r: "Stopped" if r.get("stopped") else "Nothing was running",
            needs_shell=True),
        ToolSpec(
            "web_search",
            "Search the web by keywords (not by address) for current information. Returns titles, URLs, snippets and the site; open a result with read_url. "
            "Use plain words (3 to 8), no quotes. For news or anything that changes, set recency. For a normal question prefer research_web, which also reads the best pages.",
            _obj({"query": {"type": "string"},
                  "recency": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Optional: only results from this period"}}, ["query"]),
            _web_search,
            lambda a: "Searching the web for best results",
            lambda r: _err(r) or f"{len(r.get('results', []))} results",
            needs_search=True),
        ToolSpec(
            "research_web",
            "Answer a normal question from the web in ONE call: searches, opens the best 3 pages in parallel and returns the passages that match the question, with their addresses. "
            "Use it for facts, how-tos, comparisons, prices, news, documentation. Give the question as a few plain words. Cite the addresses you used in your answer. "
            "If the extracts are not enough, use read_url on one source.",
            _obj({"query": {"type": "string", "description": "The question as 3 to 8 plain words"},
                  "pages": {"type": "integer", "description": "How many pages to read, 1 to 4 (default 3)"},
                  "recency": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Optional: only results from this period"}}, ["query"]),
            _research_web,
            lambda a: "Searching the web for best results",
            lambda r: _err(r) or f"Read {len(r.get('sources', []))} pages",
            needs_search=True),
        ToolSpec(
            "read_url",
            "Open a public web page or PDF by its address and read it as text (for example a link the user gives you, or a "
            "result from web_search). Reads HTML, PDF, JSON, RSS and plain text; a GitHub repository gives its README and a GitHub file its raw text. "
            "If the site blocks readers it falls back to an archived copy. Long pages come back in parts; use 'offset' to continue. "
            "Set links=true to also get the page's links, so you can follow them (docs, sitemaps, listings).",
            _obj({"url": {"type": "string", "description": "Full address starting with http:// or https://"},
                  "offset": {"type": "integer", "description": "Character offset to continue a long page (default 0)"},
                  "links": {"type": "boolean", "description": "Also return the links found on the page (default false)"}}, ["url"]),
            _read_url,
            lambda a: f"Read web page: {_s(a, 'url')[:100]}",
            lambda r: _err(r) or f"Read {r.get('total_chars', 0)} characters" + (" (more available)" if r.get("next_offset") is not None else "")),
        ToolSpec(
            "scrape_page",
            "LAST-RESORT page reader (metered: a small monthly allowance shared by everyone, 2 per task). Use it only when read_url "
            "failed or returned nothing because the page needs JavaScript or blocks plain downloads, AND the page matters for the answer. "
            "Never for casual browsing, never in a loop, never for pages read_url can open. Returns the page as markdown; long pages "
            "continue with 'offset' at no extra cost.",
            _obj({"url": {"type": "string", "description": "Full address starting with https://"},
                  "offset": {"type": "integer", "description": "Character offset to continue a long page (free)"},
                  "wait_ms": {"type": "integer", "description": "Milliseconds to let a JavaScript page finish loading (0-10000, default 0)"}}, ["url"]),
            _scrape_page,
            lambda a: f"Scrape page: {_s(a, 'url')[:100]}",
            lambda r: _err(r) or f"Scraped {r.get('total_chars', 0)} characters ({r.get('scrapes_left_this_month', '?')} scrapes left this month)",
            needs_scrape=True),
        ToolSpec(
            "github_search",
            "Search GitHub for open-source projects you can reuse, for example 'restaurant menu ordering ui' or 'booking calendar app'. Returns only repositories with a "
            "permissive licence (MIT, Apache-2.0, BSD...) that can be copied, with stars, language, size and topics. Search two or three phrasings, then pick the best one "
            "and copy it with github_clone.",
            _obj({"query": {"type": "string", "description": "A few words describing the kind of app or UI"},
                  "language": {"type": "string", "description": "Optional: JavaScript, TypeScript, Python, HTML, Dart..."}}, ["query"]),
            _github_search,
            lambda a: "Finding open source project to build this app",
            lambda r: _err(r) or f"{len(r.get('results', []))} reusable projects"),
        ToolSpec(
            "github_clone",
            "Copy a public GitHub repository (permissive licence only) into its own project folder (default: its name). The copy is the app: it is safety-scanned first "
            "(a project that fails is refused and nothing is copied; one with warnings waits until the user replies 'run anyway'), then you keep its framework and structure and "
            "change it in place. A checked copy is also kept in the shared library (by category) so the next user gets it instantly; your copy is separate and yours to change. Use subdir to copy only one folder of a large repository. Next call analyze_project; do not edit before that.",
            _obj({"repo": {"type": "string", "description": "owner/name or the github.com address"},
                  "dest": {"type": "string", "description": "Project folder to copy into (default: the repository name)"},
                  "subdir": {"type": "string", "description": "Optional: copy only this folder of the repository"},
                  "category": {"type": "string", "description": "Shared library folder for the original, one short lowercase word for the kind of app: restaurant, shop, booking, dashboard, blog, social, education, portfolio, other"}}, ["repo"]),
            _github_clone,
            lambda a: "Finding open source project to build this app",
            lambda r: _err(r) or (f"Refused for safety: {r.get('repo', '')}" if r.get("status") == "blocked" else f"Waiting for the user: {r.get('repo', '')} has warnings" if r.get("status") == "needs_approval" else f"Copied {r.get('files', 0)} files from {r.get('repo', '')} ({r.get('license', '')})")),
        ToolSpec(
            "analyze_project",
            "Read a whole project folder (a copied GitHub repository or your own) and say what it is: framework, how it starts, database, admin and where its content lives. "
            "It writes PROJECT.md. A copied repository cannot be edited until this has been called on it. Call it right after github_clone.",
            _obj({"path": {"type": "string", "description": "The project folder, for example the dest that github_clone returned"}}, ["path"]),
            _analyze_project,
            lambda a: f"Read the project: {_s(a, 'path')[:80]}",
            lambda r: _err(r) or _project_summary(r.get("report") or {})),
        ToolSpec(
            "use_skill",
            "Load a how-to guide (a skill) from the list in your instructions, for example app-design or a playbook for the kind of app you are building. "
            "Load a skill once, before the part of the work it covers.",
            _obj({"name": {"type": "string", "description": "The skill's name exactly as listed"}}, ["name"]),
            _use_skill,
            lambda a: f"Load skill: {_s(a, 'name')[:60]}",
            lambda r: _err(r) or f"Loaded skill {r.get('name', '')} ({len(r.get('guide', ''))} characters)"),
        ToolSpec(
            "flag_knowledge",
            "Tell the people who maintain the platform's guides that something was missing, wrong or out of date, or that you learned a rule that would help next time. "
            "Use it sparingly (a guide gave a wrong instruction, a skill is missing for this kind of app, a tool kept failing in the same way). Write ONE generic sentence "
            "about the guide or tool: never the user's name, files, data, keys or what the app is called.",
            _obj({"kind": {"type": "string", "enum": ["missing", "wrong", "outdated", "lesson"]},
                  "topic": {"type": "string", "description": "A few words, such as 'patch_file' or 'playbook for fitness apps'"},
                  "note": {"type": "string", "description": "One generic sentence about what was missing or wrong"},
                  "suggestion": {"type": "string", "description": "Optional: what the guide should say instead"},
                  "tool": {"type": "string", "description": "Optional: the tool involved"}}, ["kind", "topic", "note"]),
            _flag_knowledge,
            lambda a: f"Flag: {_s(a, 'topic')[:80]}",
            lambda r: _err(r) or r.get("note", "Recorded"),
            kinds=("orchestrator", "worker")),
        ToolSpec(
            "knowledgebase_search",
            "Search the knowledge base: the user's own uploaded documents plus any shared reference documents. Returns the best matching passages.",
            _obj({"query": {"type": "string"}}, ["query"]),
            _kb_search,
            lambda a: f"Search knowledge base: “{_s(a, 'query')[:90]}”",
            lambda r: _err(r) or (f"{len(r.get('matches', []))} matching passages" if r.get("matches") else (r.get("note") or "No matching passages"))),
        ToolSpec(
            "remember",
            "Save a durable fact about the user for future conversations (name, preferences, ongoing projects). Use only "
            "when the user states something worth keeping or asks you to remember it. category: general, preference or profile.",
            _obj({"fact": {"type": "string", "description": "One short, self-contained sentence"},
                  "category": {"type": "string"}}, ["fact"]),
            _remember,
            lambda a: f"Remember: {_s(a, 'fact')[:100]}",
            lambda r: _err(r) or r.get("note", "Saved"),
            kinds=("orchestrator",)),
        ToolSpec(
            "save_note",
            "Save a longer note the user can come back to: a write-up of this conversation or of one topic in it (decisions, requirements, "
            "steps that worked, what is left to do). Use it when the user asks to save, summarise or keep the conversation, or after a big piece of work. "
            "Saving the same title again updates that note. Never put passwords, keys or private data in a note.",
            _obj({"title": {"type": "string", "description": "Short topic title"},
                  "body": {"type": "string", "description": "Markdown, at most about 250 words"}}, ["title", "body"]),
            _save_note,
            lambda a: f"Save note: {_s(a, 'title')[:80]}",
            lambda r: _err(r) or r.get("note", "Saved"),
            kinds=("orchestrator",)),
        ToolSpec(
            "read_notes",
            "Open the user's saved notes that match some words (or the newest ones with no words). Use it when the user refers to earlier work "
            "or a topic listed under Saved notes.",
            _obj({"query": {"type": "string", "description": "Words to look for; empty returns the newest notes"}}, []),
            _read_notes,
            lambda a: f"Read notes: {_s(a, 'query')[:80] or 'newest'}",
            lambda r: _err(r) or f"{len(r.get('notes', []))} note(s)",
            kinds=("orchestrator",)),
    ]
    return {s.name: s for s in specs}


def definitions(registry: Dict[str, ToolSpec], kind: str, search_enabled: bool, shell_enabled: bool = False,
                scrape_enabled: bool = False) -> List[Dict[str, Any]]:
    return [s.definition() for s in registry.values()
            if kind in s.kinds and (search_enabled or not s.needs_search) and (shell_enabled or not s.needs_shell)
            and (scrape_enabled or not s.needs_scrape)]


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate_args(spec: ToolSpec, args: Dict[str, Any]) -> Optional[str]:
    """Return an error message the model can act on, or None if the arguments are acceptable."""
    schema = spec.parameters
    missing = [k for k in schema.get("required", []) if args.get(k) is None]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}"
    for key, value in args.items():
        prop = schema.get("properties", {}).get(key)
        if not prop:
            continue
        t = prop.get("type")
        if t and (not isinstance(value, _TYPES[t]) or (t in ("integer", "number") and isinstance(value, bool))):
            return f"Argument '{key}' must be of type {t}"
        if t == "array" and prop.get("items", {}).get("type") == "string" and not all(isinstance(x, str) for x in value):
            return f"Argument '{key}' must be a list of strings"
    return None


def preview(value: Any, limit: int = 160, depth: int = 0) -> Any:
    """Shrink arguments/results for the activity log (file contents are never copied into it)."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}… (+{len(value) - limit} chars)"
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: preview(v, limit, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        items = [preview(v, limit, depth + 1) for v in value[:12]]
        if len(value) > 12:
            items.append(f"… (+{len(value) - 12} more)")
        return items
    return value
