"""HTTP API (FastAPI). Thin layer: authentication, validation and translation to/from the Orchestrator."""
import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from llm_agent.core.llm_client import LLMError
from llm_agent.notes import NoteError
from llm_agent.repo_library import LibraryError
from llm_agent import admin_kit, admin_session, appkit, connections, content_schema as cs, datastore
from llm_agent.ci_build import BuildError
from llm_agent.hosting import HostingError
from llm_agent.vault import VaultError
from llm_agent.auth import SESSION_TTL_SECONDS, AuthError, Principal
from llm_agent.config import load_dotenv
from llm_agent.skills import MAX_MINE, SkillError
from llm_agent.flags import FlagError
from llm_agent.curator import CuratorError
from llm_agent.plans import PlanError
from llm_agent.profile import ProfileError
from llm_agent.core.orchestrator import MB, Orchestrator, ServiceError
from llm_agent.core.runs import Run
from llm_agent import preview as previews
from llm_agent.apps import AppError, AppStore
from llm_agent.runspec import parse_start
from llm_agent.shell import ShellError
from llm_agent.security import valid_identifier
from llm_agent.settings import ConfigError, Settings
from llm_agent.tools.file_manager import WorkspaceError
from llm_agent.tools.rag_search import KnowledgeError

log = logging.getLogger("llm_agent.api")
COOKIE = "llm_agent_session"
WEB_DIR = Path(__file__).parent / "web_ui"
PRUNE_EVERY_SECONDS = 1800

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


APP_CSP = "sandbox allow-scripts allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads; frame-ancestors 'self'"


class SecurityHeaders:
    """Pure ASGI middleware (does not buffer streaming responses)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_api = scope["path"].startswith("/api/")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Referrer-Policy", "no-referrer")
                if is_api:
                    h.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, wrapped)


class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=300)


class PasswordChange(BaseModel):
    current: str = Field(max_length=300)
    new: str = Field(max_length=300)


class ChatBody(BaseModel):
    prompt: str = Field(max_length=200_000)


class ProfileBody(BaseModel):
    name: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=254)


class CodeBody(BaseModel):
    code: str = Field(max_length=20)


class SubscribeBody(BaseModel):
    plan: str = Field(max_length=10)
    period: str = Field(max_length=10)


class ConfirmBody(BaseModel):
    order_id: str = Field(max_length=80)


class NoteBody(BaseModel):
    title: str = Field(max_length=200)
    body: str = Field(max_length=10000)


class NoteFromChatBody(BaseModel):
    session_id: str = Field(max_length=100)


class FactBody(BaseModel):
    fact: str = Field(max_length=2000)
    category: str = Field(default="general", max_length=20)


def sse_line(ev: Dict[str, Any]) -> str:
    return f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n"


def create_app(config_path: Optional[str] = None, base_dir: Optional[str] = None,
               llm_transport: Optional[httpx.AsyncBaseTransport] = None,
               search_client: Optional[httpx.AsyncClient] = None) -> FastAPI:
    base = Path(base_dir or os.getcwd()).resolve()
    load_dotenv((str(base),))
    settings = Settings(str(Path(config_path) if config_path else base / os.environ.get("CONFIG_PATH", "config.json")))
    orch = Orchestrator(settings, base_dir=str(base), llm_transport=llm_transport, search_client=search_client)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await orch.startup()

        async def periodic():
            while True:
                await asyncio.sleep(PRUNE_EVERY_SECONDS)
                try:
                    await orch.decay.run()
                except Exception:
                    log.exception("Scheduled prune failed")

        async def reaper():
            while True:
                await asyncio.sleep(30)
                try:
                    await orch.shell.reap()
                    await orch.hosting.reap()
                except Exception:
                    log.exception("Stopping expired app runs failed")

        async def reporting():
            while True:
                await asyncio.sleep(max(60, orch.settings.effective.reporter_interval_minutes * 60))
                try:
                    await orch.reporter.run_all()
                except Exception:
                    log.exception("Scheduled reports failed")

        async def learning():  # once a day: forget old notes and let the curator draft proposals (a person still approves them)
            await asyncio.sleep(3600)
            while True:
                try:
                    await orch.flags.purge()
                    await orch.curator.scheduled()
                except Exception:
                    log.exception("Daily learning run failed")
                await asyncio.sleep(86400)

        async def backing_up():  # once a day: back up every hosted app's data volumes (kept 30 days)
            await asyncio.sleep(7200)
            while True:
                try:
                    await orch.hosting.backup_all()
                except Exception:
                    log.exception("Daily backups failed")
                await asyncio.sleep(86400)

        reconcile_task = asyncio.create_task(orch.hosting.reconcile())
        task, reap_task, report_task = asyncio.create_task(periodic()), asyncio.create_task(reaper()), asyncio.create_task(reporting())
        learn_task = asyncio.create_task(learning())
        backup_task = asyncio.create_task(backing_up())
        try:
            yield
        finally:
            task.cancel()
            reap_task.cancel()
            report_task.cancel()
            learn_task.cancel()
            backup_task.cancel()
            reconcile_task.cancel()
            await run_client.aclose()
            await orch.shutdown()

    app = FastAPI(title="LLM Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.orch, app.state.settings = orch, settings
    app.add_middleware(SecurityHeaders)

    # ---- errors ----------------------------------------------------------------------------------
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, e: ServiceError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(ShellError)
    async def _shell_error(_: Request, e: ShellError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, e: AuthError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(PlanError)
    async def _plan_error(_: Request, e: PlanError):
        return JSONResponse({"error": e.message, "detail": e.message}, status_code=e.status)

    @app.exception_handler(ProfileError)
    async def _profile_error(_: Request, e: ProfileError):
        return JSONResponse({"error": e.message, "detail": e.message}, status_code=e.status)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, e: HTTPException):
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        return JSONResponse({"error": "The request was not valid"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        log.exception("Unhandled error", exc_info=e)
        return JSONResponse({"error": "Internal server error"}, status_code=500)

    # ---- dependencies ----------------------------------------------------------------------------
    async def csrf_guard(request: Request) -> None:
        """State-changing requests must come from this site (the cookie is also SameSite=Strict)."""
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                raise HTTPException(403, "Cross-site request blocked")

    def token_of(request: Request) -> Optional[str]:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.cookies.get(COOKIE)

    async def principal(request: Request) -> Principal:
        p = await orch.auth.authenticate(token_of(request))
        if not p:
            raise HTTPException(401, "Please sign in")
        return p

    async def admin(p: Principal = Depends(principal)) -> Principal:
        if not p.is_admin:
            raise HTTPException(403, "Administrator access required")
        return p

    async def reviewer(p: Principal = Depends(principal)) -> Principal:
        if not p.can_review:
            raise HTTPException(403, "Only the administrator and managers can do this")
        return p

    app.router.dependencies.append(Depends(csrf_guard))

    def set_cookie(request: Request, response: Response, token: str) -> None:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="strict",
                            secure=secure, path="/")

    def user_json(p: Principal) -> Dict[str, Any]:
        return {"user_id": p.user_id, "role": p.role, "manager": p.manager}

    def own_run(p: Principal, run_id: str) -> Run:
        run = orch.runs.get(run_id)
        if not run or run.user_id != p.user_id:
            raise HTTPException(404, "Run not found")
        return run

    async def sse(run: Run, after: int = -1) -> AsyncIterator[str]:
        async for ev in run.stream(after):
            yield ": ping\n\n" if ev is None else sse_line(ev)

    def sse_response(run: Run, after: int = -1) -> StreamingResponse:
        return StreamingResponse(sse(run, after), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    # ---- public ----------------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"ok": True, "llm_ready": settings.public()["llm_ready"]}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        p = await orch.auth.authenticate(token_of(request))
        return {"user": user_json(p) if p else None, "config": settings.public()}

    # ---- auth ------------------------------------------------------------------------------------
    @app.post("/api/auth/register")
    async def register(body: Credentials, request: Request, response: Response):
        token, p = await orch.auth.register(body.username, body.password)
        set_cookie(request, response, token)
        return {"user": user_json(p)}

    @app.post("/api/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        token, p = await orch.auth.login(body.username, body.password, request.client.host if request.client else "")
        set_cookie(request, response, token)
        return {"user": user_json(p)}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        await orch.auth.logout(token_of(request))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/password")
    async def change_password(body: PasswordChange, request: Request, p: Principal = Depends(principal)):
        await orch.auth.change_password(p, body.current, body.new, token_of(request))
        return {"ok": True}

    # ---- account: profile, e-mail verification, plan and payment ------------------------------------
    @app.get("/api/account")
    async def account(p: Principal = Depends(principal)):
        return {"profile": None if p.is_admin else await orch.profiles.get(p.user_id), "user_id": p.user_id,
                "plan": await orch.plans.status(p.user_id, p.is_admin),
                "payments_ready": orch.razorpay.enabled, "brand": settings.effective.brand_name}

    @app.post("/api/account/profile")
    async def account_profile(body: ProfileBody, p: Principal = Depends(principal)):
        return {"profile": await orch.profiles.update(p.user_id, body.name, body.phone, body.email)}

    @app.get("/api/account/improve")
    async def account_improve(p: Principal = Depends(principal)):
        return {"share": await orch.flags.opted_in(p.user_id), "can_change": not p.is_admin}

    @app.post("/api/account/improve")
    async def account_improve_set(request: Request, p: Principal = Depends(principal)):
        if p.is_admin:
            raise HTTPException(400, "The administrator account always records, to keep the guides up to date")
        share = bool((await json_body(request)).get("share", True))
        await orch.flags.set_opt(p.user_id, share)
        return {"share": share}

    @app.post("/api/account/email/send")
    async def account_email_send(p: Principal = Depends(principal)):
        return await orch.profiles.send_code(p.user_id)

    @app.post("/api/account/email/verify")
    async def account_email_verify(body: CodeBody, p: Principal = Depends(principal)):
        return {"profile": await orch.profiles.verify_code(p.user_id, body.code)}

    @app.post("/api/account/subscribe")
    async def account_subscribe(body: SubscribeBody, request: Request, p: Principal = Depends(principal)):
        if p.is_admin:
            raise HTTPException(400, "The administrator account already has every feature.")
        return await orch.razorpay.start(p.user_id, body.plan, body.period, public_base(request) + "/")

    @app.post("/api/account/subscribe/confirm")
    async def account_confirm(body: ConfirmBody, p: Principal = Depends(principal)):
        result = await orch.razorpay.confirm(p.user_id, body.order_id)
        return {**result, "plan": await orch.plans.status(p.user_id)}

    @app.get("/api/account/subscribe/pending")
    async def account_pending(p: Principal = Depends(principal)):
        return {"orders": await orch.razorpay.pending(p.user_id)}

    # ---- chat sessions & runs --------------------------------------------------------------------
    def check_sid(sid: str) -> str:
        if not valid_identifier(sid):
            raise HTTPException(400, "Invalid chat id")
        return sid

    @app.get("/api/sessions")
    async def sessions(p: Principal = Depends(principal)):
        active = orch.runs.active_for(p.user_id)
        return {"sessions": await orch.memory.list_sessions(p.user_id),
                "active_run": {"id": active.id, "session_id": active.session_id} if active else None}

    @app.get("/api/sessions/{sid}/messages")
    async def messages(sid: str, p: Principal = Depends(principal)):
        check_sid(sid)
        active = orch.runs.active_for(p.user_id)
        return {"messages": await orch.memory.get_messages(p.user_id, sid),
                "active_run": active.id if active and active.session_id == sid else None}

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str, p: Principal = Depends(principal)):
        check_sid(sid)
        active = orch.runs.active_for(p.user_id)
        if active and active.session_id == sid:
            raise HTTPException(409, "A task is still running in this chat. Stop it first.")
        await orch.memory.delete_session(p.user_id, sid)
        await orch.shell.stop_chat(p.user_id, sid)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/chat")
    async def chat(sid: str, body: ChatBody, p: Principal = Depends(principal)):
        run = await orch.start_run(p.user_id, check_sid(sid), body.prompt, p.is_admin)
        return sse_response(run)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, p: Principal = Depends(principal)):
        data = await orch.get_run(p.user_id, run_id)
        if not data:
            raise HTTPException(404, "The activity details for this answer have expired")
        return {"run": data}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, after: int = Query(-1), p: Principal = Depends(principal)):
        return sse_response(own_run(p, run_id), after)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, p: Principal = Depends(principal)):
        return {"cancelled": await orch.cancel_run(p.user_id, run_id)}

    # ---- memory ----------------------------------------------------------------------------------
    @app.get("/api/memory")
    async def memory_list(p: Principal = Depends(principal)):
        return {"facts": await orch.memory.list_facts(p.user_id), "limit": settings.effective.memory.max_long_term_facts}

    @app.post("/api/memory")
    async def memory_add(body: FactBody, p: Principal = Depends(principal)):
        try:
            saved = await orch.memory.add_fact(p.user_id, body.fact, body.category, "user",
                                               settings.effective.memory.max_long_term_facts)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"fact": saved}

    @app.get("/api/notes")
    async def notes_list(p: Principal = Depends(principal)):
        return {"notes": await orch.notes.list(p.user_id, full=True)}

    @app.post("/api/notes")
    async def notes_add(body: NoteBody, p: Principal = Depends(principal)):
        try:
            return {"note": await orch.notes.add(p.user_id, body.title, body.body, "", "user")}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/notes/from-chat")
    async def notes_from_chat(body: NoteFromChatBody, p: Principal = Depends(principal)):
        if not await orch.memory.session_exists(p.user_id, body.session_id):
            raise HTTPException(404, "Chat not found")
        try:
            return {"notes": await orch.notes_from_chat(p.user_id, body.session_id)}
        except NoteError as e:
            raise HTTPException(400, str(e))
        except LLMError as e:
            raise HTTPException(502, f"The model could not be reached: {e}")

    @app.delete("/api/notes/{note_id}")
    async def notes_delete(note_id: int, p: Principal = Depends(principal)):
        if not await orch.notes.delete(p.user_id, note_id):
            raise HTTPException(404, "Note not found")
        return {"deleted": True}

    @app.delete("/api/memory/{fact_id}")
    async def memory_delete(fact_id: int, p: Principal = Depends(principal)):
        if not await orch.memory.delete_fact(p.user_id, fact_id):
            raise HTTPException(404, "Memory not found")
        return {"ok": True}

    # ---- workspace -------------------------------------------------------------------------------
    @app.get("/api/workspace")
    async def workspace(everything: bool = False, p: Principal = Depends(principal)):
        """The user's own files. everything=true also lists the unchanged files of a copied project (preview and build need them)."""
        files = orch.workspace_for(p.user_id)
        listing = await asyncio.to_thread(files.list, "", 2000, not everything)
        return {"entries": listing["entries"], "truncated": listing["truncated"],
                "usage_bytes": await asyncio.to_thread(files.usage_bytes), "quota_bytes": files.quota_bytes}

    @app.get("/api/workspace/file")
    async def workspace_file(path: str = Query(max_length=400), p: Principal = Depends(principal)):
        files = orch.workspace_for(p.user_id)
        try:
            res = await asyncio.to_thread(files.read, path, 0, 300_000)
        except WorkspaceError as e:
            raise HTTPException(404, str(e))
        res["truncated"] = res["next_offset"] is not None
        return res

    @app.get("/api/workspace/download")
    async def workspace_download(path: str = Query(max_length=400), p: Principal = Depends(principal)):
        files = orch.workspace_for(p.user_id)
        try:
            target = files.resolve(path)
        except WorkspaceError as e:
            raise HTTPException(400, str(e))
        if not target.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.get("/api/workspace/zip")
    async def workspace_zip(path: str = Query("", max_length=400), p: Principal = Depends(principal)):
        """Download a folder (or the whole workspace) as one .zip file."""
        import tempfile
        import zipfile
        from starlette.background import BackgroundTask

        files = orch.workspace_for(p.user_id)
        try:
            folder = files.resolve(path, allow_root=True)
        except WorkspaceError as e:
            raise HTTPException(400, str(e))
        if not folder.is_dir():
            raise HTTPException(404, "Folder not found")

        def build() -> str:
            fd, tmp = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            total = 0
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
                    dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                    for name in filenames:
                        full = Path(dirpath) / name
                        if full.is_symlink() or not full.is_file():
                            continue  # never follow links out of the workspace
                        total += full.stat().st_size
                        if total > 200 * MB:
                            raise HTTPException(413, "This folder is too large to download as one file")
                        z.write(full, full.relative_to(folder).as_posix())
            return tmp

        try:
            tmp = await asyncio.to_thread(build)
        except HTTPException:
            raise
        return FileResponse(tmp, media_type="application/zip", filename=f"{folder.name if folder != files.root else 'workspace'}.zip",
                            background=BackgroundTask(os.unlink, tmp))

    @app.delete("/api/workspace/file")
    async def workspace_delete(path: str = Query(max_length=400), p: Principal = Depends(principal)):
        files = orch.workspace_for(p.user_id)
        try:
            return await asyncio.to_thread(files.delete, path)
        except WorkspaceError as e:
            raise HTTPException(400, str(e))

    # ---- build app / manage app ------------------------------------------------------------------
    async def read_limited(request: Request, limit: int) -> bytes:
        if int(request.headers.get("content-length") or 0) > limit:
            raise HTTPException(413, f"The file is larger than the {limit // MB} MB limit")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"The file is larger than the {limit // MB} MB limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def app_limit() -> int:
        return settings.effective.max_app_mb * MB

    async def app_owner(request: Request, p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Manage-app requests carry the app id and password in headers; being signed in is also required.
        The user's plan decides whether they may manage apps at all, and how many distinct apps a month."""
        row = await orch.apps.authenticate(request.headers.get("x-app-id", ""), request.headers.get("x-app-password", ""),
                                           request.client.host if request.client else "")
        await orch.plans.check_app(p.user_id, row["app_id"], p.is_admin)
        return row

    @app.post("/api/apps/build")
    async def build_app(request: Request, name: str = Query(max_length=80), p: Principal = Depends(principal)):
        await orch.plans.check_app(p.user_id, None, p.is_admin)
        built = await orch.apps.build(p.user_id, name, await read_limited(request, app_limit()))
        await orch.plans.check_app(p.user_id, built["app_id"], p.is_admin)
        return {**built, "url": f"/a/{built['app_id']}/"}

    @app.get("/api/apps")
    async def my_apps(p: Principal = Depends(principal)):
        return {"apps": await orch.apps.list_for(p.user_id), "limit": settings.effective.max_apps_per_user,
                "max_mb": settings.effective.max_app_mb}

    @app.post("/api/apps/{app_id}/reset-password")
    async def reset_app_password(app_id: str, p: Principal = Depends(principal)):
        row = await orch.apps.get(app_id)
        if row["owner_id"] != p.user_id:
            raise HTTPException(404, "Unknown app id.")
        return {"app_id": app_id, "password": await orch.apps.reset_password(app_id)}

    @app.post("/api/apps/manage/open")
    async def manage_open(row: Dict[str, Any] = Depends(app_owner)):
        return {"app": AppStore.public_info(row)}

    @app.post("/api/apps/manage/update")
    async def manage_update(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        res = await orch.apps.update(row["app_id"], await read_limited(request, app_limit()))
        return {**res, "app": AppStore.public_info(await orch.apps.get(row["app_id"]))}

    @app.post("/api/apps/manage/rollback")
    async def manage_rollback(row: Dict[str, Any] = Depends(app_owner)):
        await orch.apps.rollback(row["app_id"])
        return {"app": AppStore.public_info(await orch.apps.get(row["app_id"]))}

    @app.post("/api/apps/manage/delete")
    async def manage_delete(row: Dict[str, Any] = Depends(app_owner)):
        await orch.delete_app(row["app_id"])
        return {"ok": True}

    # ---- managed apps (Android / Windows shell) and the catalogue the apps read ----------------------------
    def public_base(request: Request) -> str:
        return (settings.effective.public_url or str(request.base_url)).rstrip("/")

    def b64(value: Any, what: str) -> Optional[bytes]:
        if not value:
            return None
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise HTTPException(400, f"The {what} could not be read. Try choosing the file again.")

    async def json_body(request: Request) -> Dict[str, Any]:
        raw = await read_limited(request, int(app_limit() * 1.4) + 4 * MB)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "The request was not valid JSON.")
        if not isinstance(body, dict):
            raise HTTPException(400, "The request was not valid JSON.")
        return body

    @app.post("/api/apps/create")
    async def create_managed_app(request: Request, p: Principal = Depends(principal)):
        body = await json_body(request)
        base = public_base(request)
        await orch.plans.check_app(p.user_id, None, p.is_admin)
        built = await orch.apps.create_app(p.user_id, str(body.get("name") or ""), str(body.get("version") or "1.0.0"), base,
                                           b64(body.get("project"), "project zip") or b"", b64(body.get("logo"), "logo"),
                                           logo_whole=body.get("logo_fit") == "whole", session_id=str(body.get("session") or ""))
        await orch.plans.check_app(p.user_id, built["app_id"], p.is_admin)
        await orch.audit.log(p.user_id, "build", f"created managed app {str(body.get('name') or '')[:60]!r}")
        return {**built, "local_url": any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0"))}

    @app.post("/api/apps/manage/details")
    async def manage_details(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        body = await json_body(request)
        done = await orch.apps.update_app(row["app_id"], str(body.get("version") or ""), b64(body.get("project"), "project zip"),
                                          b64(body.get("logo"), "logo"), body.get("name") or None, logo_whole=body.get("logo_fit") == "whole")
        return done

    @app.post("/api/apps/manage/items/list")
    async def items_list(request: Request, collection: str = Query("products", max_length=40), row: Dict[str, Any] = Depends(app_owner)):
        schema = await orch.apps.schema(row["app_id"])
        return {"collections": await orch.apps.collections(row["app_id"]), "collection": collection, "schema": schema,
                "section": cs.section_for(schema, collection), "items": await orch.apps.items(row["app_id"], collection)}

    @app.post("/api/apps/manage/schema")
    async def schema_save(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        return await orch.apps.save_schema(row["app_id"], await json_body(request))

    @app.post("/api/apps/manage/items/save")
    async def items_save(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        body = await json_body(request)
        item_id = await orch.apps.save_item(row["app_id"], str(body.get("collection") or "products"), body)
        return {"id": item_id}

    @app.post("/api/apps/manage/items/delete")
    async def items_delete(request: Request, item_id: int = Query(), row: Dict[str, Any] = Depends(app_owner)):
        await orch.apps.delete_item(row["app_id"], item_id)
        return {"ok": True}

    @app.post("/api/apps/manage/image")
    async def items_image(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        data = await read_limited(request, orch.apps.IMAGE_MAX + 1024)
        return {"image": orch.apps.save_image(row["app_id"], data)}

    CATALOG_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "x-device-id, x-app-version, x-platform",
                    "Cross-Origin-Resource-Policy": "cross-origin"}

    @app.get("/api/catalog/{app_id}/images/{name}")
    async def catalog_image(app_id: str, name: str):
        path = orch.apps.image_path(app_id, name)
        if path is None:
            raise HTTPException(404, "Not found")
        return FileResponse(path, headers={**CATALOG_CORS, "Cache-Control": "public, max-age=86400",
                                           "Content-Security-Policy": "default-src 'none'", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/catalog/{app_id}/submit/{collection}")
    async def catalog_submit(app_id: str, collection: str, request: Request):
        row = await public_app(app_id)
        if row["kind"] != "kit":
            raise HTTPException(404, "This app does not exist (or was removed).")
        body = await read_limited(request, 16 * 1024)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400, "The message was not valid JSON.")
        sub_id = await orch.apps.add_submission(app_id, collection, request.headers.get("x-device-id", ""), data)
        return JSONResponse({"ok": True, "id": sub_id}, headers=CATALOG_CORS)

    @app.options("/api/catalog/{app_id}/submit/{collection}")
    async def catalog_submit_preflight(app_id: str, collection: str):
        return Response(status_code=204, headers={**CATALOG_CORS, "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "content-type, x-device-id"})

    @app.post("/api/apps/manage/inbox/list")
    async def inbox_list(status: str = Query("", max_length=8), row: Dict[str, Any] = Depends(app_owner)):
        return {"items": await orch.apps.submissions(row["app_id"], status)}

    @app.post("/api/apps/manage/inbox/status")
    async def inbox_status(item_id: int = Query(), status: str = Query(max_length=8), row: Dict[str, Any] = Depends(app_owner)):
        await orch.apps.set_submission_status(row["app_id"], item_id, status)
        return {"ok": True}

    @app.post("/api/apps/manage/inbox/delete")
    async def inbox_delete(item_id: int = Query(), row: Dict[str, Any] = Depends(app_owner)):
        await orch.apps.delete_submission(row["app_id"], item_id)
        return {"ok": True}

    @app.get("/api/catalog/{app_id}/logo")
    async def catalog_logo(app_id: str):
        path = orch.apps.logo_path(app_id)
        if path is None:
            raise HTTPException(404, "Not found")
        return FileResponse(path, media_type="image/png", headers={**CATALOG_CORS, "Cache-Control": "public, max-age=3600",
                                                                  "Content-Security-Policy": "default-src 'none'", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/catalog/{app_id}/{collection}")
    async def catalog_items(app_id: str, collection: str, request: Request):
        row = await public_app(app_id)
        if row["kind"] != "kit":
            raise HTTPException(404, "This app does not exist (or was removed).")
        h = request.headers
        await orch.apps.touch_device(app_id, h.get("x-device-id", ""), h.get("x-app-version", ""), h.get("x-platform", ""))
        data = orch.apps.catalog(app_id, collection, public_base(request))
        data["brand"] = settings.effective.brand_name        # the "Powered by" line of the installed app follows the system's name
        hosted = await orch.hosting.row(app_id)
        if hosted and hosted["image"] and hosted["desired"] != "stopped":      # the app we host: its address is ours, never a draft
            data["settings"] = {**data["settings"], "backend_url": live_address(request, app_id)}
        return JSONResponse(data, headers={**CATALOG_CORS, "Cache-Control": "no-store"})

    # ---- hosting: the owner's backend runs in its own container on this server ----
    @app.exception_handler(HostingError)
    async def hosting_error(_: Request, e: HostingError):
        return JSONResponse({"error": str(e), "detail": str(e)}, status_code=e.status)

    @app.exception_handler(VaultError)
    async def vault_error(_: Request, e: VaultError):
        return JSONResponse({"error": str(e), "detail": str(e)}, status_code=e.status)

    def live_address(request: Request, app_id: str) -> str:
        domain = settings.effective.hosting_domain.strip().strip(".")
        return f"https://{app_id}.{domain}/" if domain else f"{public_base(request)}/live/{app_id}/"

    async def hosting_view(request: Request, row: Dict[str, Any]) -> Dict[str, Any]:
        info = await orch.hosting.info(row["app_id"])
        return {**info, "address": live_address(request, row["app_id"]) if info["state"] != "not deployed" else "",
                "secrets": await orch.app_secrets.names(row["app_id"]), "has_project": bool(json.loads(row["meta"] or "{}").get("has_project")),
                "path_mode": not settings.effective.hosting_domain}

    def managed(row: Dict[str, Any]) -> Dict[str, Any]:
        if row["kind"] != "kit":
            raise HTTPException(400, "Only Android / Windows apps can be hosted here.")
        return row

    @app.post("/api/apps/manage/hosting")
    async def hosting_status(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        return await hosting_view(request, managed(row))

    @app.post("/api/apps/manage/hosting/action")
    async def hosting_action(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        managed(row)
        action = str((await json_body(request)).get("action") or "")
        if action == "deploy":
            files = await asyncio.to_thread(orch.apps.project_contents, row["app_id"])
            if not files:
                raise HTTPException(409, "This app has no backend files. Add them under “Change the app itself” first.")
            files = {**files, **appkit.runtime_files(row["name"], row["app_id"], public_base(request))}
            await orch.hosting.deploy(row["app_id"], files)
        elif action in ("start", "restart"):
            await orch.hosting.start(row["app_id"])
        elif action == "stop":
            await orch.hosting.stop(row["app_id"])
        else:
            raise HTTPException(400, "Unknown action.")
        await orch.audit.log(row["owner_id"], "hosting", f"{action} app {row['app_id']}")
        return await hosting_view(request, row)

    @app.post("/api/apps/manage/hosting/logs")
    async def hosting_logs(row: Dict[str, Any] = Depends(app_owner)):
        return {"logs": await orch.hosting.logs(managed(row)["app_id"])}

    @app.post("/api/apps/manage/hosting/data")
    async def hosting_data(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        body = await json_body(request)
        app_id, extra = managed(row)["app_id"], {}
        if body.get("engine") is not None:                                    # the full choice: where, which database, and the address if it is online
            extra = await orch.hosting.configure_data(app_id, str(body.get("mode") or ""), str(body.get("engine") or ""), str(body.get("provider") or ""), str(body.get("value") or ""))
            await orch.audit.log(row["owner_id"], "hosting", f"data of app {app_id}: {body.get('mode')}/{body.get('engine')}")      # never the address
        else:
            await orch.hosting.set_data_mode(app_id, str(body.get("mode") or ""))
        return {**await hosting_view(request, row), **extra}

    @app.post("/api/apps/manage/hosting/data/test")
    async def hosting_data_test(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        body = await json_body(request)
        managed(row)
        return await datastore.test(str(body.get("engine") or ""), str(body.get("value") or ""))

    @app.post("/api/apps/manage/hosting/backups")
    async def hosting_backups(row: Dict[str, Any] = Depends(app_owner)):
        return {"backups": orch.hosting.backups.list(managed(row)["app_id"])}

    @app.post("/api/apps/manage/hosting/backups/create")
    async def hosting_backup_create(row: Dict[str, Any] = Depends(app_owner)):
        made = await orch.hosting.backup_create(managed(row)["app_id"])
        await orch.audit.log(row["owner_id"], "hosting", f"backup of app {row['app_id']}")
        return {**made, "backups": orch.hosting.backups.list(row["app_id"])}

    @app.post("/api/apps/manage/hosting/backups/restore")
    async def hosting_backup_restore(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        name = str((await json_body(request)).get("name") or "")
        done = await orch.hosting.backup_restore(managed(row)["app_id"], name)
        await orch.audit.log(row["owner_id"], "hosting", f"restored backup {name} of app {row['app_id']}")
        return {"restored": done, "backups": orch.hosting.backups.list(row["app_id"])}

    @app.post("/api/apps/manage/hosting/secret")
    async def hosting_secret_set(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        body = await json_body(request)
        name = str(body.get("name") or "").strip()
        await orch.app_secrets.set(managed(row)["app_id"], name, str(body.get("value") or ""))
        await orch.audit.log(row["owner_id"], "hosting", f"saved key {name} for app {row['app_id']}")      # the name only, never the value
        return {**await hosting_view(request, row), "restart_needed": True}

    @app.post("/api/apps/manage/hosting/secret/delete")
    async def hosting_secret_delete(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        name = str((await json_body(request)).get("name") or "")
        if not await orch.app_secrets.delete(managed(row)["app_id"], name):
            raise HTTPException(404, "That key is not saved.")
        await orch.audit.log(row["owner_id"], "hosting", f"removed key {name} from app {row['app_id']}")
        return {**await hosting_view(request, row), "restart_needed": True}

    @app.get("/api/admin/hosting")
    async def admin_hosting(_: Principal = Depends(admin)):
        rows = await orch.db.fetchall("SELECT h.app_id, h.desired, h.last_started, h.last_used, a.name, a.owner_id FROM hosted_apps h JOIN published_apps a ON a.app_id=h.app_id ORDER BY h.last_used DESC")
        ok, reason = await orch.hosting.available()
        return {"available": ok, "reason": reason, "runtime": settings.effective.hosting_runtime, "vault": orch.app_secrets.vault.available(), "apps": [dict(r) for r in rows]}

    @app.post("/api/admin/hosting/{app_id}/stop")
    async def admin_hosting_stop(app_id: str, _: Principal = Depends(admin)):
        await orch.apps.get(app_id)
        await orch.hosting.stop(app_id)
        return {"ok": True}

    # public traffic to hosted apps: /live/<app id>/... (sandboxed, for testing) or https://<app id>.<hosting domain>/ (production)
    live_client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False)
    LIVE_DROP_REQ = {"host", "content-length", "connection", "accept-encoding", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", admin_session.HEADER}
    LIVE_DROP_RES = {"content-length", "content-encoding", "transfer-encoding", "connection", "server", "strict-transport-security"}
    buckets: Dict[Tuple[str, str], List[float]] = {}

    def too_many(ip: str, app_id: str) -> bool:
        now, key = time.monotonic(), (ip, app_id)
        hits = [t for t in buckets.get(key, []) if now - t < 10]
        hits.append(now)
        buckets[key] = hits
        if len(buckets) > 5000:
            buckets.clear()
        return len(hits) > 150

    async def live_proxy(app_id: str, path: str, request: Request, prefix: Optional[str], admin_ok: bool = False) -> Response:
        """prefix is set in path mode: the page is sandboxed (no cookies or storage of this site) and root-relative addresses are rewritten.
        admin_ok: the request carries the owner's Manage session; only then are the app's admin paths served, and the app is told so."""
        if not re.fullmatch(r"[a-z0-9]{3,32}", app_id):
            raise HTTPException(404, "This app does not exist.")
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "content-type, x-requested-with, accept, authorization"} if prefix else {}
        if request.method == "OPTIONS" and prefix:
            return Response(status_code=204, headers=cors)
        if too_many(request.client.host if request.client else "?", app_id):
            raise HTTPException(429, "Too many requests. Wait a moment.")
        if not admin_ok and admin_session.protected(path, await orch.hosting.admin_paths(app_id)):
            raise HTTPException(403, "The admin pages are opened from Manage app with the app id and password, not from the app's own address.")
        port = await orch.hosting.endpoint(app_id)
        if port is None:
            raise HTTPException(503 if await orch.hosting.row(app_id) else 404, "This app is not available right now.")
        body = await read_limited(request, 5 * MB)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in LIVE_DROP_REQ and not (prefix and k.lower() in ("cookie", "origin", "referer", "authorization"))}
        if "cookie" in headers:
            headers["cookie"] = admin_session.without_cookie(headers["cookie"])     # the app never sees our admin cookie
        if admin_ok:
            headers[admin_session.HEADER] = "1"
        headers["x-forwarded-for"] = request.client.host if request.client else ""
        headers["x-forwarded-proto"] = "https" if prefix is None or request.url.scheme == "https" else request.url.scheme
        try:
            res = await live_client.request(request.method, f"http://127.0.0.1:{port}/{path}", params=request.query_params.multi_items(), content=body or None, headers=headers)
        except httpx.HTTPError:
            raise HTTPException(502, "The app did not answer. The owner can look at its log in Manage app.")
        if len(res.content) > 10 * MB:
            raise HTTPException(502, "The app's answer was too large.")
        data = res.content
        out = {k: v for k, v in res.headers.items() if k.lower() not in LIVE_DROP_RES and not (prefix and k.lower() in ("set-cookie", "content-security-policy", "access-control-allow-origin"))}
        if "content-security-policy" not in {k.lower() for k in out}:
            out["Content-Security-Policy"] = previews.SANDBOX_CSP if prefix else "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; frame-ancestors 'self'"
        out.setdefault("X-Frame-Options", "SAMEORIGIN")
        if prefix and "location" in out and out["location"].startswith("/") and not out["location"].startswith("//"):
            out["location"] = prefix + out["location"][1:]
        if prefix and res.headers.get("content-type", "").startswith("text/html"):
            data = _prefix_html(data.decode("utf-8", "replace"), prefix).encode()
        out.update(cors)
        return Response(data, status_code=res.status_code, headers=out)

    @app.api_route("/live/{app_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def live_path(app_id: str, path: str, request: Request):
        return await live_proxy(app_id, path, request, f"/live/{app_id}/")

    @app.get("/live/{app_id}")
    async def live_root(app_id: str):
        return RedirectResponse(f"/live/{app_id}/")

    # ---- the admin dashboard the platform builds for every app (admin_kit.py) ----------------------------------
    def dash_app(app_id: str, token: str) -> str:
        if not re.fullmatch(r"[a-z0-9]{3,32}", app_id) or admin_session.read_token(token) != app_id:
            raise HTTPException(403, "This admin session has ended. Open the admin again from Manage app.")
        return app_id

    NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data: https: http:; media-src 'self' https: http:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'self'"}

    @app.get("/admin-app/{app_id}/{token}/")
    async def dash_page(app_id: str, token: str):
        dash_app(app_id, token)
        return FileResponse(WEB_DIR / "admin_app.html", headers=NO_STORE)

    @app.get("/admin-app/{app_id}/{token}/schema")
    async def dash_schema(app_id: str, token: str):
        dash_app(app_id, token)
        try:
            row = await orch.apps.get(app_id)
        except Exception:
            row = {}
        schema = await orch.hosting.admin_schema(app_id)
        if not schema:
            raise HTTPException(404, "This app has no admin dashboard description (admin.json).")
        return JSONResponse({**schema, "app": (row or {}).get("name", app_id), "expires_in": admin_session.TTL}, headers=NO_STORE)

    @app.api_route("/admin-app/{app_id}/{token}/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def dash_api(app_id: str, token: str, rest: str, request: Request):
        dash_app(app_id, token)
        parts = rest.strip("/").split("/")
        schema = await orch.hosting.admin_schema(app_id)
        if len(parts) not in (1, 2) or parts[0] not in {c["name"] for c in schema.get("collections", [])} or (len(parts) == 2 and not admin_kit.ITEM_ID.fullmatch(parts[1])):
            raise HTTPException(404, "Unknown list.")
        if (len(parts) == 1 and request.method not in ("GET", "POST")) or (len(parts) == 2 and request.method not in ("PUT", "DELETE")):
            raise HTTPException(405, "That action is not available here.")
        port = await orch.hosting.endpoint(app_id)
        if port is None:
            raise HTTPException(409, "The app is not running. Deploy it first, then open the admin again.")
        body = await request.body()
        if len(body) > 1_000_000:
            raise HTTPException(413, "That is too much text for one item.")
        try:
            r = await live_client.request(request.method, f"http://127.0.0.1:{port}/admin-api/" + "/".join(parts), content=body or None,
                                          headers={admin_session.HEADER: "1", "content-type": "application/json"})
        except httpx.HTTPError:
            raise HTTPException(502, "The app did not answer. Look at its log with Show log.")
        try:
            data = r.json()
        except ValueError:
            data = {"error": f"The app answered something that is not JSON (HTTP {r.status_code}). Its /admin-api must return JSON." if r.status_code < 400 else
                    f"The app answered HTTP {r.status_code}. Look at its log with Show log."}
        if r.status_code >= 400 and isinstance(data, dict) and "error" not in data:
            data = {"error": str(data.get("detail") or data.get("message") or f"The app refused it (HTTP {r.status_code}).")}
        return JSONResponse(data, status_code=r.status_code, headers=NO_STORE)

    @app.post("/admin-app/{app_id}/{token}/upload")
    async def dash_upload(app_id: str, token: str, request: Request):
        dash_app(app_id, token)
        name = unquote(request.headers.get("x-filename", ""))
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in admin_kit.MEDIA:
            raise HTTPException(415, "That file type is not allowed. Use a picture (jpg, png, webp, gif), a video (mp4, webm, mov), audio (mp3, m4a, wav) or a pdf.")
        cap = admin_kit.MAX_VIDEO if admin_kit.MEDIA[ext].startswith(("video/", "audio/")) else admin_kit.MAX_IMAGE
        folder = orch.hosting.media / app_id
        used = sum(f.stat().st_size for f in folder.glob("*") if f.is_file()) if folder.is_dir() else 0
        out = secrets.token_hex(8) + "." + ext
        folder.mkdir(parents=True, exist_ok=True)
        size, target = 0, folder / (out + ".part")
        try:
            with open(target, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap or used + size > admin_kit.MAX_APP_MEDIA:
                        raise HTTPException(413, f"That file is too big (the limit is {cap // (1024 * 1024)} MB, and {admin_kit.MAX_APP_MEDIA // (1024 * 1024)} MB in total for this app).")
                    f.write(chunk)
            if size == 0:
                raise HTTPException(400, "The file is empty.")
            target.rename(folder / out)
        finally:
            if target.exists():
                target.unlink()
        return JSONResponse({"url": f"{public_base(request)}/media/{app_id}/{out}", "bytes": size}, headers=NO_STORE)

    @app.get("/media/{app_id}/{name}")
    async def media_file(app_id: str, name: str):
        ext = name.rsplit(".", 1)[-1].lower()
        path = orch.hosting.media / app_id / name
        if not re.fullmatch(r"[a-z0-9]{3,32}", app_id) or not re.fullmatch(r"[0-9a-f]{16}\.[a-z0-9]{2,4}", name) or ext not in admin_kit.MEDIA or not path.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(path, media_type=admin_kit.MEDIA[ext], headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=86400",
                                                                            "Content-Security-Policy": "sandbox", "Cross-Origin-Resource-Policy": "cross-origin"})

    @app.api_route("/admin-live/{app_id}/{token}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def admin_live(app_id: str, token: str, path: str, request: Request):
        """The owner's way into the app's admin pages when the server has no hosting domain (testing). Needs the session Manage app issued."""
        if admin_session.read_token(token) != app_id:
            raise HTTPException(403, "This admin session has ended. Open the admin again from Manage app.")
        return await live_proxy(app_id, path, request, f"/admin-live/{app_id}/{token}/", admin_ok=True)

    @app.post("/api/apps/manage/admin-session")
    async def admin_open(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        app_id = managed(row)["app_id"]
        if (await orch.hosting.info(app_id))["state"] == "not deployed":
            raise HTTPException(409, "Deploy the app first, then open its admin.")
        paths = await orch.hosting.admin_paths(app_id)
        port = await orch.hosting.endpoint(app_id)
        if port is None:
            raise HTTPException(409, "The app is not running. Deploy it first (or wait a moment if it was just started), then open its admin.")
        schema = await orch.hosting.admin_schema(app_id)
        if schema:                                                 # the dashboard the platform builds from admin.json: the app only needs its /admin-api
            first = schema["collections"][0]["name"]
            try:
                r = await live_client.get(f"http://127.0.0.1:{port}/admin-api/{first}", headers={admin_session.HEADER: "1"}, follow_redirects=False)
            except httpx.HTTPError:
                raise HTTPException(502, "The app did not answer. Look at its log with Show log, then try again.")
            if r.status_code >= 500:
                raise HTTPException(502, f"The app's admin API crashed (HTTP {r.status_code}). Open Show log to see why, and ask the assistant to fix it.")
            if r.status_code in (404, 405, 410):
                raise HTTPException(409, f"This app has no admin API yet: it answered 'not found' at /admin-api/{first}. Every app needs one. Ask the assistant to add it, then update the app and open the admin again.")
            token = admin_session.make_token(app_id)
            await orch.audit.log(row["owner_id"], "hosting", f"opened the admin dashboard of app {app_id}")
            return {"url": f"{public_base(request)}/admin-app/{app_id}/{token}/", "path": "/admin-app", "expires_in": admin_session.TTL, "cookies": False, "dashboard": True}
        found = None
        for pth in paths:                                          # an app with its own admin pages: ask the app itself: an admin that does not exist must not end in a bare 'Not Found'
            best = None
            for variant in (pth + "/", pth):                       # a Flask route '/admin' does not answer '/admin/' (and the other way round): use the one that works
                try:
                    r = await live_client.get(f"http://127.0.0.1:{port}{variant}", headers={admin_session.HEADER: "1"}, follow_redirects=False)
                except httpx.HTTPError:
                    raise HTTPException(502, "The app did not answer. Look at its log with Show log, then try again.")
                if r.status_code >= 500:
                    raise HTTPException(502, f"The app's admin page crashed (HTTP {r.status_code}). Open Show log to see why, and ask the assistant to fix it.")
                if r.status_code in (404, 410):
                    continue
                if r.status_code < 300:
                    best = variant
                    break
                best = best or variant                              # a redirect or a refusal: the page exists, prefer a variant that answers directly
            if best:
                found = best
                break
        if found is None:
            raise HTTPException(409, f"This app has no admin page yet: it answered 'not found' at {', '.join(paths)}. Every app needs one. "
                                     "Ask the assistant in chat: 'Add the admin to my app', then update the app and open the admin again.")
        token, domain = admin_session.make_token(app_id), settings.effective.hosting_domain.strip().strip(".")
        url = f"https://{app_id}.{domain}/__wb_admin?t={token}&p={found}" if domain else f"{public_base(request)}/admin-live/{app_id}/{token}{found}"
        await orch.audit.log(row["owner_id"], "hosting", f"opened the admin of app {app_id}")
        return {"url": url, "path": found.rstrip("/"), "expires_in": admin_session.TTL, "cookies": bool(domain)}

    @app.middleware("http")
    async def host_router(request: Request, call_next):
        domain = settings.effective.hosting_domain.strip().strip(".").lower()
        host = request.headers.get("host", "").split(":")[0].lower()
        if domain and host.endswith("." + domain):
            app_id = host[: -len(domain) - 1]
            if request.method == "GET" and request.url.path == "/__wb_admin":        # Manage app sends the owner here with a short-lived session
                t = request.query_params.get("t", "")
                if admin_session.read_token(t) != app_id:
                    return JSONResponse({"error": "This admin session has ended. Open the admin again from Manage app."}, status_code=403)
                ap = await orch.hosting.admin_paths(app_id)
                want = request.query_params.get("p", "")
                res = RedirectResponse(want if want.rstrip("/") in ap else ap[0] + "/", status_code=303, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
                res.set_cookie(admin_session.COOKIE, t, max_age=admin_session.TTL, httponly=True, secure=True, samesite="lax", path="/")
                return res
            try:
                return await live_proxy(app_id, request.url.path.lstrip("/"), request, None,
                                        admin_ok=admin_session.cookie_ok(request.headers.get("cookie", ""), app_id))
            except HTTPException as e:                                              # raised in middleware nothing else would turn into a response
                return JSONResponse({"error": e.detail, "detail": e.detail}, status_code=e.status_code)
        return await call_next(request)

    # ---- branded native builds (GitHub Actions) ----
    @app.exception_handler(BuildError)
    async def build_error(_: Request, e: BuildError):
        return JSONResponse({"error": str(e), "detail": str(e)}, status_code=e.status)

    @app.post("/api/apps/manage/shell-builds")
    async def shell_builds(row: Dict[str, Any] = Depends(app_owner)):
        if row["kind"] != "kit":
            raise HTTPException(400, "Only apps built with Build app > Phone / computer app can be packed.")
        return {**orch.builds.status(), "builds": await orch.builds.list_for(row["app_id"])}

    @app.post("/api/apps/manage/shell-build")
    async def shell_build(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        if row["kind"] != "kit":
            raise HTTPException(400, "Only apps built with Build app > Phone / computer app can be packed.")
        body = await json_body(request)
        platforms = [str(x) for x in (body.get("platforms") or [])][:2]
        build = await orch.builds.request(row, public_base(request), platforms, str(body.get("package_id") or ""))
        await orch.audit.log(row["owner_id"], "build", f"requested native build of app {row['app_id']} for {','.join(platforms)}")
        return build

    @app.post("/api/apps/manage/shell-build/file")
    async def shell_build_file(build_id: int = Query(), kind: str = Query(max_length=3), row: Dict[str, Any] = Depends(app_owner)):
        path = orch.builds.file_path(row["app_id"], build_id, kind)
        if path is None:
            raise HTTPException(404, "That file is not available (builds are kept for a limited time).")
        return FileResponse(path, filename=f"{orch.builds.clean_name(row['name']).replace(' ', '-')}{ {'.apk': '.apk', '.exe': '-Setup.exe', '.zip': '-windows.zip'}[path.suffix] }",
                            media_type="application/octet-stream")

    @app.get("/api/admin/build-repo.zip")
    async def build_repo_zip(_: Principal = Depends(admin)):
        import io, zipfile
        base = Path(__file__).parent / "shell_template"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(base.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    z.write(f, f.relative_to(base).as_posix())
        return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="build-repo.zip"'})

    @app.post("/api/apps/manage/overview")
    async def manage_overview(row: Dict[str, Any] = Depends(app_owner)):
        return await orch.apps.overview(row["app_id"])

    @app.post("/api/apps/manage/publish")
    async def manage_publish(row: Dict[str, Any] = Depends(app_owner)):
        return await orch.apps.publish(row["app_id"])

    @app.post("/api/apps/manage/settings")
    async def manage_settings(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        return {"settings": await orch.apps.save_settings(row["app_id"], await json_body(request))}

    # ---- shared repo library (administrator only) ---------------------------------------------------
    @app.get("/api/admin/repos")
    async def repos_tree(_: Principal = Depends(admin)):
        return {"root": str(orch.repo_library.root), "categories": await asyncio.to_thread(orch.repo_library.tree)}

    @app.get("/api/admin/repos/{category}/{key}")
    async def repos_files(category: str, key: str, _: Principal = Depends(admin)):
        try:
            return {"files": await asyncio.to_thread(orch.repo_library.listing, category, key)}
        except LibraryError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/admin/repos")
    async def repos_add(request: Request, _: Principal = Depends(admin)):
        body = await json_body(request)
        if not str(body.get("repo", "")).strip():
            raise HTTPException(400, "Give the GitHub address or owner/name.")
        res = await orch.github.add_to_library(str(body["repo"]), str(body.get("category", "")), str(body.get("subdir", "")), orch.review_ask)
        if res["status"] == "error":
            raise HTTPException(400, res["error"])
        if res["status"] == "refused":
            raise HTTPException(422, "Not saved, the safety check found: " + " ".join(res["reasons"]))
        return res

    @app.post("/api/admin/repos/category")
    async def repos_category(request: Request, _: Principal = Depends(admin)):
        return {"category": orch.repo_library.make_category(str((await json_body(request)).get("name", "")))}

    @app.delete("/api/admin/repos/{category}/{key}")
    async def repos_delete(category: str, key: str, _: Principal = Depends(admin)):
        try:
            await asyncio.to_thread(orch.repo_library.delete, category, key)
        except LibraryError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.delete("/api/admin/repos/{category}")
    async def repos_delete_category(category: str, _: Principal = Depends(admin)):
        try:
            await asyncio.to_thread(orch.repo_library.delete_category, category)
        except LibraryError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.get("/api/admin/apps")
    async def admin_apps(_: Principal = Depends(admin)):
        return {"apps": await orch.apps.list_all()}

    @app.delete("/api/admin/apps/{app_id}")
    async def admin_delete_app(app_id: str, _: Principal = Depends(admin)):
        await orch.apps.get(app_id)
        await orch.delete_app(app_id)
        return {"ok": True}

    # ---- public: published apps (no login) --------------------------------------------------------
    async def public_app(app_id: str) -> Dict[str, Any]:
        try:
            return await orch.apps.get(app_id)
        except AppError:
            raise HTTPException(404, "This app does not exist (or was removed).")

    @app.get("/a/{app_id}/")
    async def app_shell(app_id: str):
        import html as _html
        row = await public_app(app_id)
        if row["kind"] != "web":
            raise HTTPException(404, "This app does not exist (or was removed).")
        name = _html.escape(row["name"])
        page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{name}</title>'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                f'<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon.svg"><link rel="apple-touch-icon" href="icon.svg">'
                '<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes">'
                f'<meta name="apple-mobile-web-app-title" content="{name}"><meta name="theme-color" content="#ffffff">'
                '<style>html,body{height:100%;margin:0;background:#fff}iframe{display:block;border:0;width:100%;height:100%}</style></head>'
                f'<body><iframe src="app/?v={row["version"]}" title="{name}" sandbox="allow-scripts allow-forms allow-modals allow-popups '
                'allow-popups-to-escape-sandbox allow-downloads"></iframe></body></html>')
        return Response(page, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/a/{app_id}/manifest.webmanifest")
    async def app_manifest(app_id: str):
        row = await public_app(app_id)
        return JSONResponse({"name": row["name"], "short_name": row["name"][:12], "start_url": f"/a/{app_id}/", "scope": f"/a/{app_id}/",
                             "display": "standalone", "background_color": "#ffffff", "theme_color": "#ffffff",
                             "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]},
                            media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    @app.get("/a/{app_id}/icon.svg")
    async def app_icon(app_id: str):
        import html as _html
        row = await public_app(app_id)
        hue = sum(map(ord, app_id)) * 37 % 360
        letter = _html.escape((row["name"].strip() or "A")[0].upper())
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="hsl({hue},62%,42%)"/>'
               f'<text x="256" y="350" font-family="Arial,sans-serif" font-size="300" font-weight="700" text-anchor="middle" fill="#fff">{letter}</text></svg>')
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/a/{app_id}/app/{path:path}")
    async def app_content(app_id: str, path: str):
        await public_app(app_id)
        target = orch.apps.find_file(app_id, path)
        if target is None:
            raise HTTPException(404, "Not found")
        data, ctype = await asyncio.to_thread(AppStore.render, target)
        return Response(data, media_type=ctype, headers={
            "Content-Security-Policy": APP_CSP,
            "X-Frame-Options": "SAMEORIGIN", "Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache", "Referrer-Policy": "no-referrer"})

    # ---- run an app in this chat's environment, then look at it -----------------------------------
    class RunBody(BaseModel):
        path: str = Field(default="", max_length=400)
        session_id: str = Field(max_length=64)

    @app.get("/api/run")
    async def run_status(session_id: str = Query("", max_length=64), p: Principal = Depends(principal)):
        return {**await orch.shell.status(), "run": orch.shell.current(p.user_id, session_id)}

    @app.post("/api/run")
    async def run_start(body: RunBody, p: Principal = Depends(principal)):
        if not valid_identifier(body.session_id):
            raise HTTPException(400, "Invalid chat.")
        files = orch.workspace_for(p.user_id)
        try:
            folder = files.resolve(body.path, allow_root=True)
        except WorkspaceError as e:
            raise HTTPException(400, str(e))
        if not folder.is_dir():
            raise HTTPException(400, "Choose a folder that contains the app.")
        names = {f.name for f in folder.iterdir() if f.is_file()}
        try:
            start = parse_start((folder / "run.json").read_text(errors="replace") if "run.json" in names else None, names)
        except ValueError as e:
            raise HTTPException(400, str(e))
        run = await orch.shell.start_app(p.user_id, body.session_id, files, files.rel(folder), start)
        return {**run, "url": f"/run/{previews.make_token(run['id'])}/"}

    class ConnBody(BaseModel):
        session_id: str = Field(max_length=64)
        items: Dict[str, str] = Field(default_factory=dict)

    @app.get("/api/run/connections")
    async def conn_get(session_id: str = Query("", max_length=64), p: Principal = Depends(principal)):
        if not valid_identifier(session_id):
            raise HTTPException(400, "Invalid chat.")
        return {"items": connections.public(orch.shell.connections(p.user_id, session_id))}

    @app.put("/api/run/connections")
    async def conn_put(body: ConnBody, p: Principal = Depends(principal)):
        if not valid_identifier(body.session_id):
            raise HTTPException(400, "Invalid chat.")
        try:
            saved = orch.shell.set_connections(p.user_id, body.session_id, body.items)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"items": connections.public(saved), "restart": bool(orch.shell.current(p.user_id, body.session_id))}

    def own_app(run_id: str, p: Principal):
        run = orch.shell.get(run_id)
        if not run or run.user_id != p.user_id:
            raise HTTPException(404, "This app is no longer running. Run it again.")
        return run

    @app.post("/api/run/{run_id}/link")
    async def run_link(run_id: str, p: Principal = Depends(principal)):
        run = own_app(run_id, p)
        return {**orch.shell.public(run), "url": f"/run/{previews.make_token(run_id)}/"}

    @app.get("/api/run/{run_id}/logs")
    async def run_logs(run_id: str, p: Principal = Depends(principal)):
        own_app(run_id, p)
        return {"logs": orch.shell.logs(run_id)}

    @app.delete("/api/run/{run_id}")
    async def run_stop(run_id: str, p: Principal = Depends(principal)):
        run = orch.shell.apps.get(run_id)
        if run and run.user_id == p.user_id:
            await orch.shell.stop_app(run_id)
        return {"ok": True}

    PROXY_DROP_REQ = {"host", "cookie", "authorization", "content-length", "connection", "accept-encoding", "origin", "referer", "x-forwarded-for"}
    PROXY_DROP_RES = {"set-cookie", "content-length", "content-encoding", "transfer-encoding", "connection", "content-security-policy", "server",
                      "access-control-allow-origin", "strict-transport-security"}
    run_client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False)

    def _prefix_html(text: str, prefix: str) -> str:
        """Pages written for the site root ("/api/x", "/static/a.js") keep working under /run/<link>/."""
        text = re.sub(r'(\s(?:src|href|action)=["\'])/(?!/)', lambda m: m.group(1) + prefix, text)
        shim = ("<script>(function(P){var f=window.fetch;window.fetch=function(u,o){if(typeof u==='string'&&u[0]==='/'&&u[1]!=='/')u=P+u.slice(1);return f.call(this,u,o)};"
                "var x=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){if(typeof u==='string'&&u[0]==='/'&&u[1]!=='/')arguments[1]=P+u.slice(1);return x.apply(this,arguments)}})("
                + json.dumps(prefix) + ");</script>")
        i = text.lower().find("<head")
        i = text.find(">", i) + 1 if i != -1 else 0
        return text[:i] + previews.STORAGE_SHIM + shim + text[i:]

    @app.api_route("/run/{token}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def run_proxy(token: str, path: str, request: Request):
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "content-type, x-requested-with, accept"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        run_id = previews.read_token(token) or ""
        run = orch.shell.get(run_id)
        if not run:
            raise HTTPException(404, "This app is not running (it may have timed out). Run it again.")
        body = await read_limited(request, 1 * MB)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in PROXY_DROP_REQ}
        try:
            res = await run_client.request(request.method, f"http://127.0.0.1:{run.port}/{path}", params=request.query_params.multi_items(),
                                           content=body or None, headers=headers)
        except httpx.HTTPError:
            raise HTTPException(502, "The app did not answer. Look at its log; it may have crashed.")
        data = res.content[:5 * MB]
        prefix = f"/run/{token}/"
        out = {k: v for k, v in res.headers.items() if k.lower() not in PROXY_DROP_RES}
        if "location" in out and out["location"].startswith("/") and not out["location"].startswith("//"):
            out["location"] = prefix + out["location"][1:]
        ctype = res.headers.get("content-type", "application/octet-stream")
        if ctype.startswith("text/html"):
            data = _prefix_html(data.decode("utf-8", "replace"), prefix).encode()
        out.update(cors)
        out.update({"Content-Security-Policy": previews.SANDBOX_CSP, "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return Response(data, status_code=res.status_code, headers=out)

    # ---- app preview -----------------------------------------------------------------------------
    class PreviewBody(BaseModel):
        path: str = Field(max_length=400)

    @app.post("/api/preview")
    async def preview_link(body: PreviewBody, p: Principal = Depends(principal)):
        files = orch.workspace_for(p.user_id)
        try:
            target = previews.pick_file(files.resolve(body.path, allow_root=True))
        except WorkspaceError as e:
            raise HTTPException(400, str(e))
        if target is None:
            raise HTTPException(404, "That page does not exist. Ask the assistant to create an index.html.")
        rel = files.rel(target)
        from urllib.parse import quote
        return {"url": f"/preview/{previews.make_token(p.user_id)}/{quote(rel)}", "path": rel,
                "expires_in": previews.TOKEN_TTL_SECONDS}

    @app.get("/preview/{token}/{path:path}")
    async def preview_file(token: str, path: str):
        user_id = previews.read_token(token)
        if not user_id or not valid_identifier(user_id):
            raise HTTPException(403, "This preview link has expired. Reopen the preview.")
        try:
            target = previews.pick_file(orch.workspace_for(user_id).resolve(path, allow_root=True))
        except WorkspaceError:
            raise HTTPException(404, "Not found")
        if target is None:
            raise HTTPException(404, "Not found")
        data, ctype = await asyncio.to_thread(previews.render, target)
        return Response(data, media_type=ctype, headers={
            "Content-Security-Policy": previews.SANDBOX_CSP, "X-Frame-Options": "SAMEORIGIN",
            "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    # ---- knowledge base --------------------------------------------------------------------------
    # Admins manage the shared library; everyone else manages a private one with a storage quota.
    def kb_owner(p: Principal) -> Optional[str]:
        return None if p.role == "admin" else p.user_id

    @app.get("/api/knowledge")
    async def knowledge(p: Principal = Depends(principal)):
        owner = kb_owner(p)
        docs = await asyncio.to_thread(orch.kb.store(owner).list_documents)
        used = await asyncio.to_thread(orch.kb.usage, owner)
        return {"documents": docs, "docling": orch.kb.store(owner).docling_available(), "shared": owner is None,
                "usage_bytes": used, "quota_bytes": None if owner is None else settings.effective.kb_quota_mb * MB}

    @app.get("/api/knowledge/search")
    async def knowledge_search(q: str = Query(min_length=1, max_length=300), p: Principal = Depends(principal)):
        return await asyncio.to_thread(orch.kb.search, p.user_id if p.role != "admin" else None, q, 5, p.role == "admin")

    @app.put("/api/knowledge/{filename}")
    async def knowledge_upload(filename: str, request: Request, p: Principal = Depends(principal)):
        cfg = settings.effective
        limit = cfg.max_upload_mb * MB
        orch.kb.max_upload_bytes = limit
        too_big = HTTPException(413, f"File exceeds the {cfg.max_upload_mb} MB upload limit")
        if int(request.headers.get("content-length") or 0) > limit:
            raise too_big
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise too_big
            chunks.append(chunk)
        owner = kb_owner(p)
        try:
            return await orch.kb.add(owner, filename, b"".join(chunks), None if owner is None else cfg.kb_quota_mb * MB)
        except KnowledgeError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/knowledge/{filename}")
    async def knowledge_delete(filename: str, p: Principal = Depends(principal)):
        if not await asyncio.to_thread(orch.kb.store(kb_owner(p)).delete_document, filename):
            raise HTTPException(404, "Document not found")
        return {"ok": True}

    # ---- admin -----------------------------------------------------------------------------------
    @app.get("/api/admin/config")
    async def admin_config(_: Principal = Depends(admin)):
        return {"config": settings.admin_view()}

    @app.put("/api/admin/config")
    async def admin_config_update(patch: Dict[str, Any] = Body(...), _: Principal = Depends(admin)):
        try:
            settings.update(patch)
        except ConfigError as e:
            return JSONResponse({"error": "; ".join(e.problems), "problems": e.problems}, status_code=422)
        return {"config": settings.admin_view()}

    @app.post("/api/admin/config/reset")
    async def admin_config_reset(_: Principal = Depends(admin)):
        settings.reset()
        return {"config": settings.admin_view()}

    @app.post("/api/admin/test-llm")
    async def admin_test_llm(_: Principal = Depends(admin)):
        return await orch.llm.probe()

    @app.post("/api/admin/prune")
    async def admin_prune(_: Principal = Depends(admin)):
        return await orch.decay.run()

    @app.get("/api/admin/stats")
    async def admin_stats(_: Principal = Depends(admin)):
        return await orch.stats()

    @app.get("/api/admin/users")
    async def admin_users(_: Principal = Depends(admin)):
        users = await orch.auth.list_users()
        for u in users:
            u["plan"] = (await orch.plans.current(u["user_id"]))["plan"]
        return {"users": users}

    @app.post("/api/admin/users/{user_id}/plan")
    async def admin_set_plan(user_id: str, request: Request, _: Principal = Depends(admin)):
        body = await json_body(request)
        if not valid_identifier(user_id) or not any(u["user_id"] == user_id for u in await orch.auth.list_users()):
            raise HTTPException(404, "User not found")
        plan, period = str(body.get("plan") or ""), str(body.get("period") or "")
        await orch.plans.grant(user_id, plan, period, source="admin")
        return {"plan": (await orch.plans.current(user_id))["plan"]}

    @app.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(user_id: str, _: Principal = Depends(admin)):
        if not valid_identifier(user_id) or not await orch.delete_user(user_id):
            raise HTTPException(404, "User not found")
        return {"ok": True}

    # ---- skills and the review queue: administrator and managers ------------------------------------
    def skill_error(e: SkillError) -> HTTPException:
        return HTTPException(e.status, e.message)

    # ---- a user's own skills (private to them) ---------------------------------------------------
    @app.get("/api/skills")
    async def my_skills(p: Principal = Depends(principal)):
        return {"mine": await orch.skills.mine(p.user_id), "shared": await orch.skills.list(), "limit": MAX_MINE}

    @app.put("/api/skills/{name}")
    async def my_skill_save(name: str, request: Request, p: Principal = Depends(principal)):
        body = await json_body(request)
        try:
            return await orch.skills.save_mine(p.user_id, name, str(body.get("description") or ""), str(body.get("body") or ""))
        except SkillError as e:
            raise skill_error(e)

    @app.post("/api/skills/import")
    async def my_skill_import(request: Request, p: Principal = Depends(principal)):
        body = await json_body(request)
        try:
            return await orch.skills.import_mine(p.user_id, str(body.get("text") or "")[:20000])
        except SkillError as e:
            raise skill_error(e)

    @app.get("/api/skills/{name}")
    async def my_skill(name: str, p: Principal = Depends(principal)):
        got = await orch.skills.get(name, p.user_id)
        if not got:
            raise HTTPException(404, "Skill not found")
        return got

    @app.delete("/api/skills/{name}")
    async def my_skill_delete(name: str, p: Principal = Depends(principal)):
        if not await orch.skills.delete_mine(p.user_id, name):
            raise HTTPException(404, "You have no skill with that name")
        return {"ok": True}

    @app.get("/api/review/skills")
    async def review_skills(_: Principal = Depends(reviewer)):
        return {"skills": await orch.skills.list()}

    @app.get("/api/review/skills/{name}")
    async def review_skill(name: str, _: Principal = Depends(reviewer)):
        got = await orch.skills.get(name)
        if not got:
            raise HTTPException(404, "Skill not found")
        return got

    @app.put("/api/review/skills/{name}")
    async def review_skill_save(name: str, request: Request, p: Principal = Depends(reviewer)):
        body = await json_body(request)
        try:
            got = await orch.skills.save(name, str(body.get("description") or ""), str(body.get("body") or ""), p.user_id)
        except SkillError as e:
            raise skill_error(e)
        await orch.audit.log(p.user_id, "skill", f"saved skill {name}")
        return got

    @app.delete("/api/review/skills/{name}")
    async def review_skill_revert(name: str, p: Principal = Depends(reviewer)):
        if not await orch.skills.revert(name):
            raise HTTPException(404, "That skill has no saved edit")
        await orch.audit.log(p.user_id, "skill", f"reverted skill {name}")
        return {"ok": True}

    @app.get("/api/review/flags")
    async def review_flags(status: str = "open", _: Principal = Depends(reviewer)):
        return {"flags": await orch.flags.list(status if status in ("open", "used", "dismissed") else "open"),
                "metrics": await orch.flags.metrics(14)}

    @app.post("/api/review/flags/{flag_id}/status")
    async def review_flag_status(flag_id: int, request: Request, p: Principal = Depends(reviewer)):
        try:
            ok = await orch.flags.set_status(flag_id, str((await json_body(request)).get("status") or ""))
        except FlagError as e:
            raise HTTPException(e.status, e.message)
        if not ok:
            raise HTTPException(404, "Flag not found")
        return {"ok": True}

    @app.get("/api/review/proposals")
    async def review_proposals(_: Principal = Depends(reviewer)):
        return {"proposals": await orch.curator.pending()}

    @app.post("/api/review/curator/run")
    async def review_curator_run(p: Principal = Depends(reviewer)):
        try:
            out = await orch.curator.run(p.user_id)
        except CuratorError as e:
            raise HTTPException(e.status, e.message)
        await orch.audit.log(p.user_id, "skill", f"ran the curator: {out['created']} proposal(s)")
        return out

    @app.post("/api/review/proposals/{proposal_id}/{decision}")
    async def review_proposal_decide(proposal_id: int, decision: str, request: Request, p: Principal = Depends(reviewer)):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "Not found")
        body = await json_body(request)
        try:
            out = await orch.curator.decide(proposal_id, decision == "approve", p.user_id, str(body.get("body") or "") if decision == "approve" else "")
        except CuratorError as e:
            raise HTTPException(e.status, e.message)
        await orch.audit.log(p.user_id, "skill", f"{decision}d skill proposal {proposal_id}")
        return out

    @app.post("/api/admin/users/{user_id}/manager")
    async def admin_set_manager(user_id: str, request: Request, _: Principal = Depends(admin)):
        on = bool((await json_body(request)).get("manager", True))
        if not valid_identifier(user_id) or not await orch.auth.set_manager(user_id, on):
            raise HTTPException(404, "User not found")
        await orch.audit.log(user_id, "manager", "made a manager" if on else "manager role removed")
        return {"ok": True, "manager": on}

    @app.post("/api/admin/users/{user_id}/suspend")
    async def admin_suspend(user_id: str, request: Request, _: Principal = Depends(admin)):
        body = await json_body(request)
        on = bool(body.get("suspended", True))
        if not valid_identifier(user_id) or not await orch.auth.set_suspended(user_id, on):
            raise HTTPException(404, "User not found")
        if on:
            await orch.shell.stop_user(user_id)
            await orch.stop_hosted_for(user_id)
            await orch.audit.log(user_id, "suspended", "account suspended by the administrator")
        return {"ok": True, "suspended": on}

    # ---- reports (Reporter agent) ----
    @app.get("/api/admin/reports")
    async def admin_reports(status: str = "", user: str = "", _: Principal = Depends(admin)):
        rows = await orch.reporter.list(status, user)
        return {"reports": rows, "enabled": orch.settings.effective.reporter_enabled}

    @app.post("/api/admin/reports/generate")
    async def admin_reports_generate(_: Principal = Depends(admin)):
        return await orch.reporter.run_all()

    @app.get("/api/admin/reports/{report_id}")
    async def admin_report(report_id: int, _: Principal = Depends(admin)):
        r = await orch.reporter.get(report_id)
        if not r:
            raise HTTPException(404, "Report not found")
        return r

    @app.post("/api/admin/reports/{report_id}/status")
    async def admin_report_status(report_id: int, request: Request, _: Principal = Depends(admin)):
        body = await json_body(request)
        if not await orch.reporter.set_status(report_id, str(body.get("status") or "")):
            raise HTTPException(404, "Report not found")
        return {"ok": True}

    @app.delete("/api/admin/reports/{report_id}")
    async def admin_report_delete(report_id: int, _: Principal = Depends(admin)):
        if not await orch.reporter.delete(report_id):
            raise HTTPException(404, "Report not found")
        return {"ok": True}

    # ---- web UI (must be last) -------------------------------------------------------------------
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
    return app
