"""HTTP API (FastAPI). Thin layer: authentication, validation and translation to/from the Orchestrator."""
import asyncio
import contextlib
import json
import logging
import os
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

from llm_agent.auth import SESSION_TTL_SECONDS, AuthError, Principal
from llm_agent.config import load_dotenv
from llm_agent.core.orchestrator import MB, Orchestrator, ServiceError
from llm_agent.core.runs import Run
from llm_agent import preview as previews
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

        task = asyncio.create_task(periodic())
        try:
            yield
        finally:
            task.cancel()
            await orch.shutdown()

    app = FastAPI(title="LLM Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.orch, app.state.settings = orch, settings
    app.add_middleware(SecurityHeaders)

    # ---- errors ----------------------------------------------------------------------------------
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, e: ServiceError):
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

    # ---- web UI (must be last) -------------------------------------------------------------------
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
    return app
