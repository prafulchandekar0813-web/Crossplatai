"""Tool definitions, argument validation and the handlers agents can call.

Each tool carries:
  parameters  JSON schema sent to the model (also used to validate the model's arguments)
  describe    one-line human description of a call, shown live in the UI
  summarize   one-line human description of the result, shown in the UI
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from llm_agent.config import SystemConfig

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
    timed: bool = True   # delegations run as long as the worker needs; everything else gets a timeout

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
                            "items": {"type": "object"}}}, ["path", "edits"]),
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
                  "changes": {"type": "array", "description": "List of {start_line, end_line, new_text}", "items": {"type": "object"}}},
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


def definitions(registry: Dict[str, ToolSpec], kind: str, search_enabled: bool) -> List[Dict[str, Any]]:
    return [s.definition() for s in registry.values() if kind in s.kinds and (search_enabled or not s.needs_search)]


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
