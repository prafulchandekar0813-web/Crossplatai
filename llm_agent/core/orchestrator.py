"""The application service: owns every component and runs user tasks.

API handlers call into this class; it knows nothing about HTTP.
"""
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from llm_agent.audit import Audit
from llm_agent.ci_build import CIBuilder
from llm_agent.hosting import HostingManager
from llm_agent.mail import Mailer
from llm_agent.payments import Razorpay
from llm_agent.plans import Plans
from llm_agent.profile import Profiles
from llm_agent.vault import AppSecrets
from llm_agent.core.reporter import Reporter
from llm_agent.auth import AuthService
from llm_agent.config import SystemConfig
from llm_agent.context.context_manager import ContextManager
from llm_agent.core.patience import Patience
from llm_agent.curator import Curator
from llm_agent.flags import Flags
from llm_agent.skills import Skills
from llm_agent.core.agent import Agent, RunContext, clean_text
from llm_agent.core.llm_client import LLMClient, LLMError
from llm_agent.core.prompts import build_system_prompt
from llm_agent.core.runs import Run, RunManager
from llm_agent.db import Database
from llm_agent.memory.decay import DecayEngine
from llm_agent.memory.store import MemoryStore
from llm_agent.repo_library import RepoLibrary
from llm_agent.tools.github_repos import GitHubRepos
from llm_agent.notes import NOTES_SYSTEM, Notes, NoteError, parse as parse_notes
from llm_agent import readiness
from llm_agent.repo_approval import RepoApprovals
from llm_agent.run_proof import RunProofs
from llm_agent.security import valid_identifier
from llm_agent.settings import Settings
from llm_agent.tools.file_manager import WorkspaceFiles
from llm_agent.shell import ShellManager
from llm_agent.apps import AppStore
from llm_agent.tools.rag_search import KnowledgeLibrary
from llm_agent.tools.registry import build_registry, definitions
from llm_agent.tools.web_fetch import WebFetch
from llm_agent.tools.web_scrape import Scraper
from llm_agent.tools.web_search import WebSearch

log = logging.getLogger("llm_agent.orchestrator")
MB = 1024 * 1024

SUMMARY_SYSTEM = ("You compress chat transcripts. Write a concise summary (at most 250 words) of the conversation so far: "
                  "the user's goals, decisions made, files created, and open questions. Keep names, paths and numbers exact.")


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _abs(base: Path, p: str) -> Path:
    path = Path(p).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


class Orchestrator:
    def __init__(self, settings: Settings, base_dir: Optional[str] = None,
                 llm_transport: Optional[httpx.AsyncBaseTransport] = None,
                 search_client: Optional[httpx.AsyncClient] = None,
                 fetch_client: Optional[httpx.AsyncClient] = None,
                 scrape_client: Optional[httpx.AsyncClient] = None,
                 github_client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        cfg = settings.effective
        base = Path(base_dir or os.getcwd())
        self.data_dir = _abs(base, cfg.data_dir)
        self.workspace_root = _abs(base, cfg.workspace_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

        self.db = Database(str(self.data_dir / "app.db"))
        self.auth = AuthService(self.db, settings)
        self.memory = MemoryStore(self.db)
        self.notes = Notes(self.db)
        self.repo_library = RepoLibrary(self.data_dir / "repo_library")
        self.github = GitHubRepos(github_client, self.repo_library)
        self.repo_approvals = RepoApprovals()
        self.decay = DecayEngine(self.db)
        self.skills = Skills(self.db)
        self.flags = Flags(self.db)
        self.kb = KnowledgeLibrary(str(self.data_dir / "knowledge_base"), cfg.max_upload_mb * MB)
        self.apps = AppStore(self.db, self.data_dir / "published_apps",
                             lambda: (self.settings.effective.max_apps_per_user, self.settings.effective.max_app_mb * MB))
        self.app_secrets = AppSecrets(self.db)
        self.proofs = RunProofs(self.db)
        self.apps.run_proof = self._run_proof
        self.apps.node_ok = lambda: settings.effective.hosting_runtime == "docker"
        self.hosting = HostingManager(self.db, self.app_secrets, settings, self.data_dir / "hosting")
        self.builds = CIBuilder(self.db, settings, self.data_dir / "shell_builds")
        self.plans = Plans(self.db, lambda: self.settings.effective)
        self.razorpay = Razorpay(self.db, self.plans, lambda: self.settings.effective)
        self.profiles = Profiles(self.db, Mailer(lambda: self.settings.effective.brand_name), lambda: self.settings.effective.brand_name)
        self.audit = Audit(self.db, lambda: self.settings.effective.reporter_enabled)
        self.shell = ShellManager(settings, self.data_dir)
        self.shell.on_blocked = lambda uid, cmd: self.audit.log(uid, "blocked_command", cmd)
        self.llm = LLMClient(lambda: self.settings.effective.openai, transport=llm_transport,
                             backoff=0.0 if llm_transport is not None else 1.0)
        self.fetch = WebFetch(client=fetch_client)
        self.scrape = Scraper(self.db, lambda: self.settings.effective, client=scrape_client)
        self.search = WebSearch(lambda: self.settings.effective.search, client=search_client)
        self.registry = build_registry()
        self.runs = RunManager()
        self.reporter = Reporter(self.db, self.audit, self.llm, settings)
        self.curator = Curator(self.db, self.llm, self.skills, self.flags)
        self.slots = asyncio.Semaphore(cfg.max_concurrent_runs)
        self._requests = 0
        self._background: set = set()

    # ---- lifecycle -------------------------------------------------------------------------------
    async def startup(self) -> None:
        await self.decay.run()
        await self.shell.cleanup_orphans()

    async def shutdown(self) -> None:
        for run in self.runs.running():
            if run.task:
                run.task.cancel()
        tasks = [r.task for r in self.runs.running() if r.task]
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        await self.shell.stop_all()
        await self.hosting.stop_all()
        await self.llm.aclose()
        await self.builds.aclose()
        await self.search.aclose()
        await self.github.aclose()
        await self.fetch.aclose()
        await self.scrape.aclose()
        await self.razorpay.aclose()
        self.db.close()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---- per-user resources ----------------------------------------------------------------------
    def workspace_for(self, user_id: str) -> WorkspaceFiles:
        if not valid_identifier(user_id):
            raise ServiceError("Invalid user", 400)
        cfg = self.settings.effective
        return WorkspaceFiles(str(self.workspace_root / user_id), cfg.max_file_kb * 1024, cfg.workspace_quota_mb * MB)

    async def delete_user(self, user_id: str) -> bool:
        for run in self.runs.running():
            if run.user_id == user_id and run.task:
                run.task.cancel()
        existed = await self.auth.delete_account(user_id)
        await self.memory.delete_user_data(user_id)
        if valid_identifier(user_id):
            shutil.rmtree(self.workspace_root / user_id, ignore_errors=True)
            self.kb.delete_user(user_id)
            await self.shell.stop_user(user_id)
            for r in await self.db.fetchall("SELECT app_id FROM published_apps WHERE owner_id=?", (user_id,)):
                await self.hosting.remove(r["app_id"])
            await self.apps.delete_owner(user_id)
            await self.plans.forget_user(user_id)
            await self.audit.forget_user(user_id)
        return existed

    async def delete_app(self, app_id: str) -> None:
        """Remove an app completely: its running container and data volume, its saved keys, then its records."""
        await self.hosting.remove(app_id)
        await self.apps.delete(app_id)

    async def stop_hosted_for(self, user_id: str) -> None:
        for r in await self.db.fetchall("SELECT app_id FROM published_apps WHERE owner_id=?", (user_id,)):
            await self.hosting.stop(r["app_id"])

    # ---- running tasks ---------------------------------------------------------------------------
    async def start_run(self, user_id: str, session_id: str, prompt: str, is_admin: bool = False) -> Run:
        cfg = self.settings.effective
        prompt = (prompt or "").strip()
        if not prompt:
            raise ServiceError("The message is empty", 400)
        if len(prompt) > cfg.max_prompt_chars:
            raise ServiceError(f"The message is too long (limit {cfg.max_prompt_chars} characters)", 413)
        if not valid_identifier(session_id):
            raise ServiceError("Invalid chat id", 400)
        if not (cfg.openai.base_url and cfg.openai.model):
            raise ServiceError("No model is configured yet. An administrator needs to set the model server URL and "
                               "model name under Admin → Model.", 503)
        if self.runs.active_for(user_id):
            raise ServiceError("You already have a task running. Wait for it to finish or stop it first.", 409)
        await self.plans.check_chat(user_id, is_admin)

        self.repo_approvals.heard(user_id, prompt)       # the user's own words are the only way a warned repository gets approved
        run = self.runs.create(user_id, session_id, prompt)
        run.info["is_admin"] = is_admin
        try:
            run.user_message_id = await self.memory.add_message(user_id, session_id, "user", prompt)
        except Exception:
            self.runs.release(run)
            await run.finish("error")
            raise
        self._requests += 1
        await self.audit.log(user_id, "prompt", prompt)
        if self._requests % cfg.memory.auto_prune_interval_turns == 0:
            self._spawn(self.decay.run())
        run.task = asyncio.create_task(self._execute(run, cfg))
        return run

    async def cancel_run(self, user_id: str, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if not run or run.user_id != user_id:
            raise ServiceError("Run not found", 404)
        if run.finished or not run.task:
            return False
        run.task.cancel()
        return True

    async def get_run(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.runs.get(run_id)
        if run:
            return run.snapshot() if run.user_id == user_id else None
        trace = await self.memory.get_run(user_id, run_id)
        return trace

    # ---- the task itself -------------------------------------------------------------------------
    async def _execute(self, run: Run, cfg: SystemConfig) -> None:
        started = time.monotonic()
        status, text = "error", ""
        try:
            await run.emit("run_started", run_id=run.id, session_id=run.session_id, prompt=run.prompt[:500])
            try:
                if self.slots.locked():
                    await run.emit("queued", message="Waiting for a free slot - other tasks are running.")
                async with self.slots:
                    async with asyncio.timeout(cfg.circuit_breaker.max_run_seconds):
                        status, text = await self._run_agents(run, cfg)
            except TimeoutError:
                status = "timeout"
                text = (f"This task hit the time limit ({cfg.circuit_breaker.max_run_seconds} seconds) and was stopped. "
                        "Anything already written to your workspace is still there.")
            except asyncio.CancelledError:
                status, text = "cancelled", "Stopped. Anything already written to your workspace is still there."
            except LLMError as e:
                status, text = "error", f"⚠️ {e}"
            except Exception:
                log.exception("Task %s failed", run.id)
                status, text = "error", "⚠️ Something went wrong inside the server while running this task. The details are in the server log."
            await self._finalize(run, cfg, status, text, started)
        finally:
            self.runs.release(run)
            if not run.finished:
                await run.finish(status)

    async def _finalize(self, run: Run, cfg: SystemConfig, status: str, text: str, started: float) -> None:
        msg_status = "ok" if status in ("completed", "partial") else ("cancelled" if status == "cancelled" else "error")
        try:
            await self.memory.add_message(run.user_id, run.session_id, "assistant", text, status=msg_status, run_id=run.id)
        except Exception:
            log.exception("Could not save the assistant message for run %s", run.id)
        rc: Optional[RunContext] = run.info.get("rc")
        agents = []
        if rc:
            for a in rc.agents.values():
                if a.get("status") == "running":
                    a["status"] = "cancelled" if status == "cancelled" else "stopped"
                agents.append(dict(a))
        summary = {
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "usage": dict(rc.usage) if rc else {},
            "agents": agents,
            "files": sorted(rc.written_files) if rc else [],
            "plan": rc.plan if rc else None,
        }
        usage = summary["usage"]
        tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        if not tokens and usage.get("llm_calls"):  # the provider reported no usage: estimate from the text (about 4 characters a token)
            tokens = (len(run.prompt) + len(text)) // 4
        try:
            await self.plans.record_tokens(run.user_id, tokens, bool(run.info.get("is_admin")))
        except Exception:
            log.exception("Could not record token usage for run %s", run.id)
        await run.emit("final", content=text, status=status)
        await run.emit("run_finished", **summary)
        await run.finish(status)
        try:
            await self.memory.save_run(run.user_id, run.session_id, run.id, {**run.snapshot(), "summary": summary},
                                       cfg.memory.ttl_days_transient)
        except Exception:
            log.exception("Could not store the trace for run %s", run.id)

    async def _summarize(self, text: str) -> str:
        resp = await self.llm.chat([{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": text}],
                                   max_tokens=700, temperature=0.2, allow_tools_fallback=False)
        return clean_text(resp.message.get("content"))

    async def _run_proof(self, owner: str, files: Dict[str, bytes]) -> Optional[bool]:
        """Did this exact project start in the sandbox? None when the sandbox is not available here, so the gate does not block on it."""
        try:
            await self.shell.require()
        except Exception:
            return None
        return await self.proofs.seen(owner, readiness.digest(files))

    async def review_ask(self, messages: List[Dict[str, str]]) -> str:
        """The model as a tool-less reviewer of downloaded code (repo_review.py)."""
        resp = await self.llm.chat(messages, max_tokens=500, temperature=0.0, allow_tools_fallback=False)
        return str(resp.message.get("content") or "")

    async def notes_from_chat(self, user_id: str, session_id: str) -> List[Dict[str, Any]]:
        """Write the whole conversation up as a few useful notes (an LLM call over the stored messages)."""
        _, summary = await self.memory.get_summary(user_id, session_id)
        rows = await self.memory.model_history(user_id, session_id, limit=200)
        if not rows:
            raise NoteError("That chat has no messages to write up yet.")
        text = "".join(f"{r['role'].upper()}: {r['content'][:1500]}\n" for r in rows)[-24000:]
        resp = await self.llm.chat([{"role": "system", "content": NOTES_SYSTEM},
                                    {"role": "user", "content": (f"Earlier part of the chat, summarised:\n{summary}\n\n" if summary else "") + "Conversation:\n" + text}],
                                   max_tokens=1800, temperature=0.2, allow_tools_fallback=False)
        notes = parse_notes(str(resp.message.get("content") or ""))
        if not notes:
            raise NoteError("The model did not return usable notes. Try again.")
        return [await self.notes.add(user_id, n["title"], n["body"], session_id, "chat") for n in notes]

    async def _run_agents(self, run: Run, cfg: SystemConfig):
        user_id, session_id = run.user_id, run.session_id
        files = self.workspace_for(user_id)
        ctxm = ContextManager(cfg.context)

        upto, summary = await self.memory.get_summary(user_id, session_id)
        rows = [r for r in await self.memory.model_history(user_id, session_id, after_id=upto)
                if r["id"] < run.user_message_id]
        facts = await self.memory.relevant_facts(user_id, run.prompt, cfg.memory.max_facts_in_prompt)
        note_titles = [(n["id"], n["title"]) for n in (await self.notes.list(user_id))[:12]]
        workspace_lines = await asyncio.to_thread(files.summary_lines)
        search_on = self.search.enabled

        catalog = await self.skills.catalog(user_id)
        draft = build_system_prompt(cfg, "orchestrator", "Orchestrator", user_id, search_on, facts, workspace_lines, skills=catalog, notes=note_titles)
        tool_defs = definitions(self.registry, "orchestrator", search_on, self.shell.enabled, self.scrape.enabled)
        reserved = (ctxm.estimate_tokens(draft) + ctxm.estimate_tokens(json.dumps(tool_defs))
                    + ctxm.estimate_tokens(run.prompt) + cfg.openai.max_tokens)
        history, summary, new_summary = await ctxm.prepare_history(rows, summary, self._summarize, reserved)
        if new_summary:
            await self.memory.set_summary(user_id, session_id, new_summary[0], new_summary[1])
            await run.emit("context_summarised", message="Older messages in this chat were summarised to save context.")

        system = build_system_prompt(cfg, "orchestrator", "Orchestrator", user_id, search_on, facts, workspace_lines, summary, skills=catalog, notes=note_titles)
        # merge so a failed earlier turn (user message without a reply) never leaves two user messages in a row
        convo = ContextManager.merge_adjacent([*history, {"role": "user", "content": run.prompt}])
        messages = [{"role": "system", "content": system}, *convo]

        rc = RunContext(user_id=user_id, cfg=cfg, llm=self.llm, registry=self.registry, files=files, kb=self.kb,
                        search=self.search, fetch=self.fetch, memory=self.memory, context=ctxm, run=run,
                        sem=asyncio.Semaphore(cfg.max_subagents_per_user), facts=facts, shell=self.shell, scrape=self.scrape,
                        patience=Patience(cfg.circuit_breaker.max_wait_seconds), skills=self.skills, notes=self.notes, github=self.github, review_ask=self.review_ask, approvals=self.repo_approvals, proofs=self.proofs, apps=self.apps, library=self.repo_library, flags=self.flags,
                        share=await self.flags.opted_in(user_id))
        run.info["rc"] = rc
        agent = Agent(rc, "orchestrator", "orchestrator", "Orchestrator")
        result = await agent.run(messages)
        await self._count_task(rc, result.status, agent.breaker.checkpoints)
        return result.status, result.text

    @staticmethod
    async def _count_task(rc: Any, status: str, checkpoints: int) -> None:
        """Plain counters about how tasks end (never who ran them or what they said), unless the user switched recording off."""
        if not rc.share:
            return
        await rc.flags.bump(f"task.{status}")
        if checkpoints:
            await rc.flags.bump("task.checkpoints", checkpoints)
        if rc.design_last is not None:
            await rc.flags.bump("design.last_notes", rc.design_last)
            await rc.flags.bump("design.last_checks")

    # ---- admin -----------------------------------------------------------------------------------
    async def stats(self) -> Dict[str, Any]:
        out = await self.memory.stats()
        out.update(running_tasks=len(self.runs.running()), knowledge=self.kb.stats(), published_apps=self.apps.count_files(),
                   database_bytes=os.path.getsize(self.db.path) if os.path.exists(self.db.path) else 0)
        return out
