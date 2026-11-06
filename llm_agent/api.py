"""HTTP API (FastAPI). Thin layer: authentication, validation and translation to/from the Orchestrator."""
import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from llm_agent import connections
from llm_agent.ci_build import BuildError
from llm_agent.auth import SESSION_TTL_SECONDS, AuthError, Principal
from llm_agent.config import load_dotenv
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

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
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
                except Exception:
                    log.exception("Stopping expired app runs failed")

        async def reporting():
            while True:
                await asyncio.sleep(max(60, orch.settings.effective.reporter_interval_minutes * 60))
                try:
                    await orch.reporter.run_all()
                except Exception:
                    log.exception("Scheduled reports failed")

        task, reap_task, report_task = asyncio.create_task(periodic()), asyncio.create_task(reaper()), asyncio.create_task(reporting())
        try:
            yield
        finally:
            task.cancel()
            reap_task.cancel()
            report_task.cancel()
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

    app.router.dependencies.append(Depends(csrf_guard))

    def set_cookie(request: Request, response: Response, token: str) -> None:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="strict",
                            secure=secure, path="/")

    def user_json(p: Principal) -> Dict[str, Any]:
        return {"user_id": p.user_id, "role": p.role}

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
        run = await orch.start_run(p.user_id, check_sid(sid), body.prompt)
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

    @app.delete("/api/memory/{fact_id}")
    async def memory_delete(fact_id: int, p: Principal = Depends(principal)):
        if not await orch.memory.delete_fact(p.user_id, fact_id):
            raise HTTPException(404, "Memory not found")
        return {"ok": True}

    # ---- workspace -------------------------------------------------------------------------------
    @app.get("/api/workspace")
    async def workspace(p: Principal = Depends(principal)):
        files = orch.workspace_for(p.user_id)
        listing = await asyncio.to_thread(files.list, "", 2000)
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
        """Manage-app requests carry the app id and password in headers; being signed in is also required."""
        return await orch.apps.authenticate(request.headers.get("x-app-id", ""), request.headers.get("x-app-password", ""),
                                            request.client.host if request.client else "")

    @app.post("/api/apps/build")
    async def build_app(request: Request, name: str = Query(max_length=80), p: Principal = Depends(principal)):
        built = await orch.apps.build(p.user_id, name, await read_limited(request, app_limit()))
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
        await orch.apps.delete(row["app_id"])
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
        built = await orch.apps.create_app(p.user_id, str(body.get("name") or ""), str(body.get("version") or "1.0.0"), base,
                                           b64(body.get("project"), "project zip") or b"", b64(body.get("logo"), "logo"))
        await orch.audit.log(p.user_id, "build", f"created managed app {str(body.get('name') or '')[:60]!r}")
        return {**built, "local_url": any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0"))}

    @app.post("/api/apps/manage/backend")
    async def manage_backend_download(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        data = await asyncio.to_thread(orch.apps.backend_bundle, row, public_base(request))
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{row["app_id"]}-backend.zip"', "Cache-Control": "no-store"})

    @app.post("/api/apps/manage/details")
    async def manage_details(request: Request, row: Dict[str, Any] = Depends(app_owner)):
        body = await json_body(request)
        return await orch.apps.update_app(row["app_id"], str(body.get("version") or ""), b64(body.get("project"), "project zip"),
                                          b64(body.get("logo"), "logo"), body.get("name") or None)

    @app.post("/api/apps/manage/items/list")
    async def items_list(request: Request, collection: str = Query("products", max_length=40), row: Dict[str, Any] = Depends(app_owner)):
        return {"collections": await orch.apps.collections(row["app_id"]), "collection": collection,
                "items": await orch.apps.items(row["app_id"], collection)}

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
        return JSONResponse(orch.apps.catalog(app_id, collection, public_base(request)), headers={**CATALOG_CORS, "Cache-Control": "no-store"})

    # ---- branded native builds (GitHub Actions) ----
    @app.exception_handler(BuildError)
    async def build_error(_: Request, e: BuildError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

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
        return FileResponse(path, filename=f"{orch.builds.clean_name(row['name']).replace(' ', '-')}{'.apk' if kind == 'apk' else '-windows.zip'}",
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

    @app.get("/api/admin/apps")
    async def admin_apps(_: Principal = Depends(admin)):
        return {"apps": await orch.apps.list_all()}

    @app.delete("/api/admin/apps/{app_id}")
    async def admin_delete_app(app_id: str, _: Principal = Depends(admin)):
        await orch.apps.get(app_id)
        await orch.apps.delete(app_id)
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
        return {"users": await orch.auth.list_users()}

    @app.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(user_id: str, _: Principal = Depends(admin)):
        if not valid_identifier(user_id) or not await orch.delete_user(user_id):
            raise HTTPException(404, "User not found")
        return {"ok": True}

    @app.post("/api/admin/users/{user_id}/suspend")
    async def admin_suspend(user_id: str, request: Request, _: Principal = Depends(admin)):
        body = await json_body(request)
        on = bool(body.get("suspended", True))
        if not valid_identifier(user_id) or not await orch.auth.set_suspended(user_id, on):
            raise HTTPException(404, "User not found")
        if on:
            await orch.shell.stop_user(user_id)
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
