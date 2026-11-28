"""The agent loop.

One Agent instance = one agent (the orchestrator, or a worker it delegated to) working on one task:

    ask the model -> it answers or calls tools -> run the tools -> feed results back -> repeat

The loop ends when the model answers without calling tools, or when the circuit breaker trips (turn / tool-call
limits, repeated identical calls, consecutive failures). A tripped agent is asked once, without tools, to summarise
what it achieved, so the user always gets an answer instead of a bare error.
Every step is published as an event on the Run; the UI renders those events live.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from llm_agent.circuit_breaker.call_cache import CallCache
from llm_agent.circuit_breaker.circuit_breaker import AgentCircuitBreaker
from llm_agent.core.patience import Patience
from llm_agent.config import SystemConfig
from llm_agent.context.context_manager import ContextManager
from llm_agent.core.llm_client import LLMClient, LLMError
from llm_agent.core.prompts import build_system_prompt
from llm_agent.core.runs import Run
from llm_agent.tools.file_manager import WorkspaceError
from llm_agent.tools.rag_search import KnowledgeError
from llm_agent.tools.registry import ToolContext, ToolSpec, definitions, preview, validate_args

log = logging.getLogger("llm_agent.agent")
CODE_EXT = (".py", ".js", ".ts", ".html", ".css", ".dart", ".java", ".kt", ".go", ".rs")
RESEARCH_NUDGE = ("Before writing the app's code, look up the trusted sources first: "
                  "use research_web on the official documentation for the libraries you will use (current version and API) and a design reference "
                  "for this kind of app, then write the code from what you read and name the sources. "
                  "If nothing in this task can have changed since you learned it, say so in one line.")
READ_NUDGE = ("research_web only returns short extracts. Before you write the code, open the single most important page in full with read_url (the official documentation of the main library, "
              "or the design reference) and write from what it says.")
LINK = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
MAX_AUTO_LINKS = 3
SKILL_NUDGE = ("Before you write the screens, load the guide: use_skill(\"github-ui\") (start from a real open-source UI) and use_skill(\"app-design\"), plus the playbook skill that matches this kind of app if there is one. Then write the file again.")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def clean_text(content: Any) -> str:
    """Assistant text as plain string: join multi-part content, drop <think> reasoning blocks."""
    if content is None:
        return ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    text = _THINK.sub("", str(content))
    if re.search(r"</think>", text, re.IGNORECASE):  # template that opens <think> inside the prompt
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


@dataclass
class RunContext:
    """State shared by every agent working on one task."""
    user_id: str
    cfg: SystemConfig
    llm: LLMClient
    registry: Dict[str, ToolSpec]
    files: Any
    kb: Any
    search: Any
    fetch: Any
    memory: Any
    context: ContextManager
    run: Run
    sem: asyncio.Semaphore
    facts: List[Dict[str, str]] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
    delegations: int = 0
    agents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    written_files: Set[str] = field(default_factory=set)
    plan: Optional[Dict[str, Any]] = None
    shell: Any = None
    scrape: Any = None
    scrapes: int = 0
    skills: Any = None               # llm_agent.skills.Skills
    notes: Any = None                # llm_agent.notes.Notes
    github: Any = None               # llm_agent.tools.github_repos.GitHubRepos
    review_ask: Any = None           # async (messages) -> str: the model as a tool-less code reviewer
    approvals: Any = None            # llm_agent.repo_approval.RepoApprovals
    proofs: Any = None               # llm_agent.run_proof.RunProofs
    apps: Any = None                 # llm_agent.apps.AppStore (name checks)
    library: Any = None              # llm_agent.repo_library.RepoLibrary
    skills_used: Set[str] = field(default_factory=set)
    flags: Any = None                # llm_agent.flags.Flags
    share: bool = False              # the user allows generic flags and counters
    flags_made: int = 0
    design_first: Optional[int] = None
    design_last: Optional[int] = None
    patience: Any = None             # one waiting allowance shared by every agent of the task


@dataclass
class AgentResult:
    status: str                       # completed | partial | error
    text: str
    turns: int = 0
    tool_calls: int = 0
    reason: Optional[str] = None
    files: List[str] = field(default_factory=list)


@dataclass
class _Call:
    id: str
    name: str
    args: Dict[str, Any]
    raw_args: str
    spec: Optional[ToolSpec] = None
    error: Optional[str] = None
    skipped: bool = False             # never executed (blocked / after a trip): does not count as a tool failure
    result: Optional[Dict[str, Any]] = None
    reused: bool = False              # answered from an earlier identical call: free, not a failure
    same_as: Optional["_Call"] = None  # identical to another call in the same reply


def _parse_args(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    if isinstance(raw, dict):
        return raw, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}, "The arguments were not valid JSON"
    if not isinstance(value, dict):
        return {}, "The arguments must be a JSON object"
    return value, None


class Agent:
    def __init__(self, rc: RunContext, agent_id: str, kind: str, role: str, parent: Optional[str] = None):
        self.rc = rc
        self.id = agent_id
        self.kind = kind              # "orchestrator" | "worker"
        self.role = role
        self.parent = parent
        self.breaker = AgentCircuitBreaker(rc.cfg.circuit_breaker)
        self.cache = CallCache()
        self.patience = getattr(rc, "patience", None) or Patience(rc.cfg.circuit_breaker.max_wait_seconds)
        self.researched = False       # a web lookup has succeeded in this task
        self.read_a_page = False      # read_url or scrape_page has opened a page in full
        self.nudged = False           # the one-time research reminder has been given
        self.files_written: Set[str] = set()

    async def emit(self, type: str, **data: Any) -> None:
        await self.rc.run.emit(type, agent=self.id, **data)

    # ---- entry point -----------------------------------------------------------------------------
    async def run(self, messages: List[Dict[str, Any]]) -> AgentResult:
        rc = self.rc
        started = time.monotonic()
        info = rc.agents.setdefault(self.id, {"id": self.id, "role": self.role, "parent": self.parent})
        info["status"] = "running"
        await self.emit("agent_started", role=self.role, parent=self.parent)
        try:
            text, status, reason = await self._loop(messages)
            result = AgentResult(status, text, reason=reason)
        except LLMError as e:
            result = AgentResult("error", f"The model request failed: {e}", reason=str(e))
        result.turns, result.tool_calls = self.breaker.all_turns, self.breaker.all_tool_calls
        result.files = sorted(self.files_written)
        info.update(status=result.status, turns=result.turns, tool_calls=result.tool_calls, reason=result.reason,
                    duration_ms=int((time.monotonic() - started) * 1000))
        await self.emit("agent_finished", status=result.status, turns=result.turns, tool_calls=result.tool_calls,
                        reason=result.reason, summary=result.text[:600], files=result.files,
                        duration_ms=info["duration_ms"])
        return result

    # ---- the loop --------------------------------------------------------------------------------
    async def _loop(self, messages: List[Dict[str, Any]]) -> Tuple[str, str, Optional[str]]:
        rc, breaker = self.rc, self.breaker
        defs = definitions(rc.registry, self.kind, rc.search.enabled, bool(rc.shell and rc.shell.enabled), bool(rc.scrape and rc.scrape.enabled))
        tools_on = bool(defs)
        if tools_on and self.kind == "orchestrator" and self.id == "orchestrator":
            await self._open_links(messages)
        empty = 0
        while True:
            verdict = breaker.check_turn()
            if not verdict.allowed:
                if await self._checkpoint(messages):
                    continue
                return await self._wrap_up(messages, verdict.reason)
            if (over := self._over_budget()):
                return await self._wrap_up(messages, over)
            compacted, overflow = rc.context.compact(messages)
            if compacted:
                await self.emit("context_compacted", messages=compacted)
            if overflow:
                return await self._wrap_up(messages, "The context window is full.")

            await self.emit("llm_start", turn=breaker.turns)
            t0 = time.monotonic()
            resp = await self._ask(messages, defs if tools_on else None)
            rc.usage["llm_calls"] += 1
            rc.usage["prompt_tokens"] += int(resp.usage.get("prompt_tokens") or 0)
            rc.usage["completion_tokens"] += int(resp.usage.get("completion_tokens") or 0)
            if resp.tools_dropped:
                tools_on = False
                await self.emit("warning", message="The model server rejected tool calling, so the assistant can only "
                                                   "chat in this task. Use a model with tool support for files, search and delegation.")
            text = clean_text(resp.message.get("content"))
            raw_calls = resp.message.get("tool_calls") or []
            await self.emit("llm_end", turn=breaker.turns, duration_ms=int((time.monotonic() - t0) * 1000),
                            usage=resp.usage, finish_reason=resp.finish_reason, tool_calls=len(raw_calls))

            if not raw_calls:
                if not text:
                    # Models sometimes go quiet (after tool results, or a reasoning model spends its whole allowance thinking).
                    # Ask again, without showing the user anything; only the admin-facing warning records it.
                    empty += 1
                    await self.emit("diagnostic", message=f"The model returned an empty reply (finish reason: {resp.finish_reason}); asking again ({empty}).")
                    if empty <= 2:
                        messages.append({"role": "user", "content": "[System notice] Your last reply was empty. If the task is finished, write the final answer for the user now "
                                                                    "in a few clear sentences. If more work is needed, call the next tool. Do not think at length."})
                        continue
                    return self._quiet_model_message(), "completed", None
                empty = 0
                if resp.finish_reason == "length":
                    text += "\n\n_(The reply was cut off because it hit the max-tokens limit.)_"
                return text, "completed", None

            if text:
                await self.emit("assistant_note", text=text[:1500])
            calls = self._normalise(raw_calls)
            messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name,
                 "arguments": c.raw_args if not c.error or "JSON" not in c.error else "{}"}} for c in calls]})
            await self._run_tool_calls(calls, resp.finish_reason, messages)
            if breaker.tripped:
                if await self._checkpoint(messages):
                    continue
                return await self._wrap_up(messages, breaker.trip_reason or "Stopped by the circuit breaker.")

    async def _ask(self, messages: List[Dict[str, Any]], defs: Optional[List[Dict[str, Any]]]) -> Any:
        """One model request: wait out a busy model, and make room once if the request was too long."""
        rc = self.rc

        async def on_wait(delay: float, why: str, attempt: int) -> None:
            await self.emit("waiting", seconds=int(delay), reason=why, attempt=attempt,
                            message=f"Waiting {int(delay)} seconds because {why}, then trying again (attempt {attempt}).")
        try:
            return await self.patience.call(lambda: rc.llm.chat(messages, defs), on_wait)
        except LLMError as e:
            if not e.context_full:
                raise
        shrunk, _ = rc.context.compact(messages, keep_recent_tools=1, force=True)
        await self.emit("context_compacted", messages=shrunk)
        return await self.patience.call(lambda: rc.llm.chat(messages, defs), on_wait)

    def _over_budget(self) -> Optional[str]:
        cap = self.rc.cfg.circuit_breaker.max_task_tokens
        used = self.rc.usage["prompt_tokens"] + self.rc.usage["completion_tokens"]
        if cap and used >= cap:
            return f"This task used its token budget ({cap:,} tokens)."
        return None

    async def _checkpoint(self, messages: List[Dict[str, Any]]) -> bool:
        """The round / tool-call allowance ran out while work was going well: save the progress, make room and carry on.

        Loops and repeated failures never get here; the time and token budgets still apply."""
        breaker = self.breaker
        if not breaker.can_renew() or self._over_budget():
            return False
        reason = breaker.trip_reason
        breaker.renew()
        shrunk, _ = self.rc.context.compact(messages, keep_recent_tools=3, force=True)
        await self.emit("checkpoint", number=breaker.checkpoints, of=breaker.config.max_checkpoints, reason=reason, compacted=shrunk,
                        message=f"Checkpoint {breaker.checkpoints}: {reason} Carrying on with a fresh allowance.")
        messages.append({"role": "user", "content": (
            f"[System notice] Checkpoint {breaker.checkpoints}. {reason} Your allowance has been renewed, so keep going. "
            "Files you wrote are still in the workspace; older tool output was shortened. Do not redo finished work. "
            "Any tool calls that were blocked just now were not run: send them again if you still need them.")})
        return True

    def _quiet_model_message(self) -> str:
        """What the user reads when the model stays silent after being asked twice: what happened, in plain words, with no admin advice."""
        done = f" Files saved in your workspace: {', '.join(sorted(self.files_written)[:8])}." if self.files_written else ""
        return "I could not write a closing message for this one." + done + " Please send your message again, or tell me what to do next and I'll carry on from here."

    async def _wrap_up(self, messages: List[Dict[str, Any]], reason: str) -> Tuple[str, str, Optional[str]]:
        """The breaker tripped: get a final summary from the model without offering any tools."""
        stats = {k: v for k, v in self.breaker.snapshot().items() if k != "reason"}
        await self.emit("breaker", reason=reason, **stats)
        messages.append({"role": "user", "content":
                         f"[System notice] {reason} You cannot use any more tools. Write your final answer now: say what "
                         "you completed, what is unfinished, and anything the user should know. Do not call tools."})
        try:
            resp = await self._ask(messages, None)
            text = clean_text(resp.message.get("content"))
        except LLMError:
            text = ""
        if not text:
            text = f"I stopped early: {reason}"
        return text, "partial", reason

    # ---- tool calls ------------------------------------------------------------------------------
    @staticmethod
    def _normalise(raw_calls: List[Dict[str, Any]]) -> List[_Call]:
        out, seen = [], set()
        for c in raw_calls:
            fn = c.get("function") or {}
            cid = c.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            while cid in seen:
                cid += "x"
            seen.add(cid)
            raw = fn.get("arguments")
            raw_str = raw if isinstance(raw, str) else json.dumps(raw or {})
            args, err = _parse_args(raw)
            out.append(_Call(cid, str(fn.get("name") or ""), args, raw_str or "{}", error=err))
        return out

    def _tool_context(self) -> ToolContext:
        rc = self.rc
        return ToolContext(user_id=rc.user_id, cfg=rc.cfg, files=rc.files, kb=rc.kb, search=rc.search, fetch=rc.fetch, memory=rc.memory,
                           rc=rc, agent_id=self.id, emit=self.emit,
                           delegate=self._delegate if self.kind == "orchestrator" else None, shell=rc.shell, session_id=rc.run.session_id, scrape=rc.scrape)

    async def _open_links(self, messages: List[Dict[str, Any]]) -> None:
        """Links in the user's message are opened for them before the model answers (read_url, with the page scraper as its own fallback), so a link is never ignored.
        It is recorded as the model's own read_url calls, so the page text arrives as ordinary tool results (untrusted data)."""
        last = messages[-1] if messages else {}
        text = last.get("content") if last.get("role") == "user" else ""
        urls = list(dict.fromkeys(u.rstrip(".,;:") for u in LINK.findall(text if isinstance(text, str) else "")))[:MAX_AUTO_LINKS]
        spec = self.rc.registry.get("read_url")
        if not urls or spec is None:
            return
        calls = [_Call(f"auto_{uuid.uuid4().hex[:8]}", "read_url", {"url": u}, json.dumps({"url": u}), spec=spec) for u in urls]
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": c.id, "type": "function", "function": {"name": "read_url", "arguments": c.raw_args}} for c in calls]})
        for c in calls:
            await self.emit("tool_start", call_id=c.id, tool=c.name, args=preview(c.args), summary=spec.describe(c.args))
            await self._execute(c)
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": self.rc.context.truncate_tool_output(json.dumps(c.result, ensure_ascii=False, default=str))})

    async def _run_tool_calls(self, calls: List[_Call], finish_reason: Optional[str], messages: List[Dict[str, Any]]) -> None:
        rc, breaker = self.rc, self.breaker
        allowed = definitions(rc.registry, self.kind, rc.search.enabled, bool(rc.shell and rc.shell.enabled), bool(rc.scrape and rc.scrape.enabled))
        allowed_names = {d["function"]["name"] for d in allowed}
        blocked_reason: Optional[str] = None

        for c in calls:
            c.spec = rc.registry.get(c.name) if c.name in allowed_names else None
            if blocked_reason:
                c.error, c.skipped = f"Skipped: {blocked_reason}", True
            elif c.spec is None:
                c.error = f"Unknown tool '{c.name}'. Available tools: {', '.join(sorted(allowed_names))}."
            elif c.error:  # unparsable arguments
                if finish_reason == "length":
                    c.error += ". The reply hit the max-tokens limit and was cut off; send smaller files or split the work."
            else:
                c.args, err = c.spec.check_args(c.args)
                err = err or validate_args(c.spec, c.args)
                if err:
                    c.error = err
                elif (nudge := self._needs_prep(c)):
                    self.nudged = True
                    c.error, c.skipped = "Not written yet (this does not count as a failure). " + nudge, True
                else:
                    twin = next((o for o in calls[:calls.index(c)] if o.same_as is None and not o.error and o.spec is not None
                                 and o.name == c.name and o.args == c.args and self.cache.cacheable(c.name)), None)
                    cached = None if twin else self.cache.lookup(c.name, c.args)
                    if twin or cached is not None:
                        verdict = breaker.note_repeat(c.name)
                        if verdict.allowed:
                            c.reused, c.same_as, c.result = True, twin, cached
                            continue
                    else:
                        verdict = breaker.check_tool_call(c.name, c.args)
                    if not verdict.allowed:
                        blocked_reason = verdict.reason
                        c.error, c.skipped = f"Blocked by the circuit breaker: {verdict.reason}", True

        for c in calls:
            await self.emit("tool_start", call_id=c.id, tool=c.name, args=preview(c.args),
                            summary=c.spec.describe(c.args) if c.spec else f"Call {c.name or '(unnamed tool)'}")

        runnable = [c for c in calls if not c.error and not c.reused]
        if len(runnable) > 1 and all(c.name == "delegate_task" for c in runnable):
            await asyncio.gather(*(self._execute(c) for c in runnable))  # parallel workers
        else:
            for c in runnable:
                await self._execute(c)

        for c in calls:
            if c.reused:
                if c.same_as is not None:
                    c.result = dict(c.same_as.result or {}, repeated=True, note="Same call as another one in this reply; it ran once.")
                await self.emit("tool_end", call_id=c.id, tool=c.name, ok=(c.result or {}).get("status") != "error",
                                summary="Same call as before: reused the earlier result", duration_ms=0, preview=preview(c.result or {}, 240))
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": rc.context.truncate_tool_output(json.dumps(c.result, ensure_ascii=False, default=str))})
                continue
            if c.error:
                c.result = {"status": "error", "error": c.error}
                await self.emit("tool_end", call_id=c.id, tool=c.name, ok=False, summary=f"Failed: {c.error}"[:300],
                                duration_ms=0, preview={"error": c.error})
            if not c.skipped:
                breaker.record_result((c.result or {}).get("status") == "error")
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": rc.context.truncate_tool_output(json.dumps(c.result, ensure_ascii=False, default=str))})

    async def _execute(self, c: _Call) -> None:
        assert c.spec is not None
        t0 = time.monotonic()
        ctx = self._tool_context()
        c.args, problem = c.spec.check_args(c.args)
        if problem:
            c.result = {"status": "error", "error": problem}
            await self.emit("tool_end", call_id=c.id, tool=c.name, ok=False, summary=f"Failed: {problem}"[:300], duration_ms=0, preview={"error": problem})
            return
        try:
            coro = c.spec.handler(ctx, c.args)
            timeout = self.rc.cfg.circuit_breaker.tool_timeout_seconds
            result = await (asyncio.wait_for(coro, timeout) if c.spec.timed else coro)
        except asyncio.TimeoutError:
            result = {"status": "error", "error": f"The tool timed out after {self.rc.cfg.circuit_breaker.tool_timeout_seconds}s"}
        except (WorkspaceError, KnowledgeError) as e:
            result = {"status": "error", "error": str(e)}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Tool %s crashed", c.name)
            result = {"status": "error", "error": f"The tool failed unexpectedly ({type(e).__name__})"}
        if not isinstance(result, dict):
            result = {"status": "success", "result": result}
        c.result = result
        self.cache.store(c.name, c.args, result)
        await self._count(c.name, result)
        if c.name in ("web_search", "research_web", "read_url", "scrape_page") and result.get("status") != "error":
            self.researched = True
        if c.name in ("read_url", "scrape_page") and result.get("status") != "error":
            self.read_a_page = True
        if c.name == "use_skill" and result.get("status") != "error":
            self.rc.skills_used.add(str(c.args.get("name", "")))
        if c.name == "write_file" and result.get("status") == "success":
            self.files_written.add(result["path"])
        await self.emit("tool_end", call_id=c.id, tool=c.name, ok=result.get("status") != "error",
                        summary=c.spec.summarize(result)[:300], duration_ms=int((time.monotonic() - t0) * 1000),
                        preview=preview(result, 240))

    async def _count(self, name: str, result: Dict[str, Any]) -> None:
        rc = self.rc
        if not rc.share or rc.flags is None or result.get("repeated"):
            return
        await rc.flags.bump(f"tool.{name}.{'err' if result.get('status') == 'error' else 'ok'}")
        if name == "check_build" and result.get("status") != "error" and isinstance(result.get("design"), list):
            n = len(result["design"])
            if rc.design_first is None:
                rc.design_first = n
                await rc.flags.bump("design.first_notes", n)
                await rc.flags.bump("design.first_checks")
            rc.design_last = n

    def _needs_prep(self, c: _Call) -> str:
        """Once per task: before the first sizeable code is written, ask for the research and the skill that go with it.

        Returns the reminder to give, or '' when nothing is missing."""
        rc = self.rc
        if self.nudged or self.kind != "orchestrator" or c.name not in ("write_file", "write_files"):
            return ""
        files = c.args.get("files") if c.name == "write_files" else [c.args]
        files = [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []
        if sum(len(str(f.get("content", ""))) for f in files if str(f.get("path", "")).lower().endswith(CODE_EXT)) < 1500:
            return ""
        missing = []
        if rc.search.enabled and not self.researched:
            missing.append(RESEARCH_NUDGE)
        elif rc.search.enabled and not self.read_a_page:
            missing.append(READ_NUDGE)
        ui = any(str(f.get("path", "")).lower().endswith((".html", ".css")) for f in files)
        if ui and rc.skills is not None and "app-design" not in rc.skills_used:
            missing.append(SKILL_NUDGE)
        return " ".join(missing)

    # ---- delegation ------------------------------------------------------------------------------
    async def _delegate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rc = self.rc
        limit = rc.cfg.circuit_breaker.max_delegations
        if rc.delegations >= limit:
            return {"status": "error", "error": f"Delegation limit reached ({limit} workers per task). Finish the rest yourself."}
        rc.delegations += 1
        worker_id = f"worker_{rc.delegations}"
        role = (args.get("role") or "Worker").strip()[:60] or "Worker"
        task = str(args["task"]).strip()
        extra = str(args.get("context") or "").strip()
        await rc.run.emit("agent_queued", agent=worker_id, parent=self.id, role=role, task=task[:400])
        async with rc.sem:  # at most max_subagents_per_user workers at once
            worker = Agent(rc, worker_id, "worker", role, parent=self.id)
            system = build_system_prompt(rc.cfg, "worker", role, rc.user_id, rc.search.enabled, [],
                                         await asyncio.to_thread(rc.files.summary_lines),
                                         skills=await rc.skills.catalog() if rc.skills else None)
            user = task + (f"\n\nContext from the orchestrator:\n{extra}" if extra else "")
            result = await worker.run([{"role": "system", "content": system}, {"role": "user", "content": user}])
        out: Dict[str, Any] = {"agent": worker_id, "role": role, "summary": result.text[:4000],
                               "files_written": result.files, "tool_calls": result.tool_calls}
        if result.status == "error":
            return {**out, "status": "error", "error": result.text[:500]}
        if result.status == "partial":
            return {**out, "status": "partial", "stopped_reason": result.reason}
        return {**out, "status": "success"}
