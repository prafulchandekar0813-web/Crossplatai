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
    timed: bool = True   # delegations run as long as the worker needs; everything else gets a timeout

    PATH_ALIASES = ("file_path", "filepath", "file", "filename", "file_name", "filePath")

    def check_args(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Fix the usual argument slips (path called file_path...) and name what is missing, instead of crashing with a KeyError."""
        props = self.parameters.get("properties", {})
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


def _err(result: Dict[str, Any]) -> Optional[str]:
    return f"Failed: {result.get('error')}" if result.get("status") == "error" else None


# ---- handlers ---------------------------------------------------------------------------------------
async def _plan_task(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    plan = {"title": (_s(a, "task_title") or _s(a, "title")).strip()[:120] or "Plan",
            "steps": [str(x)[:240] for x in a.get("steps", [])][:30],
            "files": [str(x)[:200] for x in a.get("files", [])][:50]}
    ctx.rc.plan = plan
    await ctx.emit("plan", **plan)
    return {"status": "success", "message": "Plan recorded. Now carry it out step by step."}


async def _write_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    res = await asyncio.to_thread(ctx.files.write, a["path"], a["content"])
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="created" if res["created"] else "updated", bytes=res["bytes"])
    return res


async def _patch_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    res = await asyncio.to_thread(ctx.files.patch, a["path"], a["edits"])
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="updated", bytes=res["bytes"])
    return res


async def _edit_lines(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    res = await asyncio.to_thread(ctx.files.edit_lines, a["path"], a["changes"])
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="updated", bytes=res["bytes"])
    return res


async def _insert_text(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    res = await asyncio.to_thread(ctx.files.insert, a["path"], a["text"], _s(a, "where"), a.get("line"), a.get("anchor"), bool(a.get("paragraph")))
    ctx.rc.written_files.add(res["path"])
    await ctx.emit("file_changed", path=res["path"], action="updated", bytes=res["bytes"])
    return res


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
    return buildcheck.check(await asyncio.to_thread(collect))


async def _read_file(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    limit = max(500, int(ctx.cfg.context.max_tool_output_length * 0.8))
    res = await asyncio.to_thread(ctx.files.read, a["path"], int(a.get("offset") or 0), limit, bool(a.get("line_numbers")))
    if res.get("binary"):
        res["note"] = "Binary file; its content is not shown."
    elif res["next_offset"] is not None:
        res["note"] = f"Showing part of the file. Call read_file again with offset={res['next_offset']} for the rest."
    return res


async def _list_files(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(ctx.files.list, _s(a, "path"), 200)


async def _bash(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    from llm_agent.shell import ShellError
    try:
        if a.get("background"):
            run = await ctx.shell.start_app(ctx.user_id, ctx.session_id, ctx.files, _s(a, "cwd"), _s(a, "command"))
            probe = await ctx.shell.probe(run["id"], "/")
            return {"status": "running", "http_status": probe.get("status"), "page_start": probe.get("text", "")[:400],
                    "log": ctx.shell.logs(run["id"])[-1500:],
                    "note": "The app is running and the user can see it under Preview app > Run app. Fix any error shown above and start it again. "
                            "It stops after a while or with stop_app."}
        return await ctx.shell.run(ctx.user_id, ctx.session_id, ctx.files, _s(a, "cwd"), _s(a, "command"), int(a.get("timeout") or 60))
    except ShellError as e:
        return {"error": e.message}


async def _stop_app(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    cur = ctx.shell.current(ctx.user_id, ctx.session_id)
    if cur:
        await ctx.shell.stop_app(cur["id"])
    return {"stopped": bool(cur)}


async def _web_search(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.search.search(a["query"])


async def _read_url(ctx: ToolContext, a: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.fetch.read(_s(a, "url"), a.get("offset") or 0)


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
            "Call it first, then carry the plan out.",
            _obj({"task_title": {"type": "string", "description": "Short title of the task"},
                  "steps": {"type": "array", "items": {"type": "string"}, "description": "Ordered steps"},
                  "files": {"type": "array", "items": {"type": "string"}, "description": "Paths of files you expect to create"}},
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
            "insert_text",
            "Insert new lines or a whole paragraph/block into an existing text file without touching anything else. 'where' picks the "
            "place: start, end, after_line / before_line (give 'line', the number from read_file with line_numbers), or after_text / "
            "before_text (give 'anchor': text that appears on exactly one line; the new text goes on the line after/before it). "
            "Set paragraph=true to keep a blank line around the new block (prose, markdown, docstrings).",
            _obj({"path": {"type": "string"}, "text": {"type": "string", "description": "The lines to insert (may contain several lines)"},
                  "where": {"type": "string", "description": "start | end | after_line | before_line | after_text | before_text"},
                  "line": {"type": "integer", "description": "Line number for after_line / before_line"},
                  "anchor": {"type": "string", "description": "Text on one line, for after_text / before_text"},
                  "paragraph": {"type": "boolean", "description": "Surround the block with blank lines"}}, ["path", "text", "where"]),
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
            "list_files",
            "List files and folders in your workspace (recursively). Leave path empty for the root.",
            _obj({"path": {"type": "string", "description": "Folder to list (default: workspace root)"}}, []),
            _list_files,
            lambda a: f"List files in {_s(a, 'path') or 'workspace'}",
            lambda r: _err(r) or f"{len(r.get('entries', []))} entries"),
        ToolSpec(
            "check_build",
            "Check an app project folder before you tell the user it is ready: finds the framework, a missing start file or requirements.txt, a "
            "missing PORT setting, keys written in the code, root-relative web addresses, a missing .env.example and similar. It returns "
            "{ok, framework, problems, warnings}. Fix every problem and read each warning, then run it again. Run it once when you think you are done.",
            _obj({"path": {"type": "string", "description": "The project folder (the one with app.py); empty for the workspace root"}}, []),
            _check_build,
            lambda a: f"Check build readiness of {_s(a, 'path') or 'workspace'}",
            lambda r: _err(r) or (f"Ready ({r.get('framework') or 'no framework found'}), {len(r.get('warnings', []))} warning(s)" if r.get("ok")
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
            "Search the web by keywords (not by address) for current information. Returns titles, URLs and snippets; open a result with read_url.",
            _obj({"query": {"type": "string"}}, ["query"]),
            _web_search,
            lambda a: f"Search the web: “{_s(a, 'query')[:90]}”",
            lambda r: _err(r) or f"{len(r.get('results', []))} results",
            needs_search=True),
        ToolSpec(
            "read_url",
            "Open a public web page or PDF by its address and read it as text (for example a link the user gives you, or a "
            "result from web_search). A GitHub repository address returns its README. Long pages come back in parts; use 'offset' to continue.",
            _obj({"url": {"type": "string", "description": "Full address starting with http:// or https://"},
                  "offset": {"type": "integer", "description": "Character offset to continue a long page (default 0)"}}, ["url"]),
            _read_url,
            lambda a: f"Read web page: {_s(a, 'url')[:100]}",
            lambda r: _err(r) or f"Read {r.get('total_chars', 0)} characters" + (" (more available)" if r.get("next_offset") is not None else "")),
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
    ]
    return {s.name: s for s in specs}


def definitions(registry: Dict[str, ToolSpec], kind: str, search_enabled: bool, shell_enabled: bool = False) -> List[Dict[str, Any]]:
    return [s.definition() for s in registry.values()
            if kind in s.kinds and (search_enabled or not s.needs_search) and (shell_enabled or not s.needs_shell)]


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
