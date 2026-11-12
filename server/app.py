"""Asks server: REST API (asks + v3 tasks) + MCP (streamable HTTP at /mcp) + static web app at /.

Run from the asks/ directory:  uvicorn server.app:app --host 127.0.0.1 --port 8790
Configuration: environment variables, see server/config.py.
"""

import logging
import mimetypes
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio.to_thread
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.routing import Route

from .config import Settings
from .core import AskError, AskService
from .db import Database
from .drive import DriveClient
from .mcp_tools import IDENTITY_SCOPE_KEY, build_mcp
from .mirror import DriveMirror
from .notify import Notifier
from .tasks import TaskService
from .tokens import Identity, TokenStore, bearer_token

log = logging.getLogger("asks")
mimetypes.add_type("application/manifest+json", ".webmanifest")

# The largest valid request (a full ask with every character \u-escaped) is about 160 KB; a full task is smaller.
MAX_BODY = 256 * 1024


# ---------- request bodies (types only; limits are checked in core) ----------

class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkIn(StrictBody):
    url: str
    title: str | None = None


class AskCreate(StrictBody):
    title: str
    context: str | None = None
    options: list[str] | None = None
    links: list[LinkIn] | None = None
    priority: Literal["normal", "urgent"] | None = None
    ref: str | None = None
    # Hosted only (SPEC v5 step 4). Any: the single-tenant bridge refuses every non-empty value with one message,
    # and the hosted one names the bad item (`attachments[2]: …`), so pydantic checks nothing here.
    attachments: Any = None


class AskPatch(StrictBody):
    title: str | None = None
    context: str | None = None
    options: list[str] | None = None
    links: list[LinkIn] | None = None
    priority: Literal["normal", "urgent"] | None = None
    ref: str | None = None


class AnswerIn(StrictBody):
    answer: str


class CloseIn(StrictBody):  # also the body of POST /api/tasks/{id}/done and /drop
    resolution: str | None = None


class TaskCreate(StrictBody):
    title: str
    context: str | None = None
    status: str | None = None  # active | waiting, checked in tasks.py (with a hint to use done/drop)
    waiting_on: str | None = None
    next_check: str | None = None
    links: list[LinkIn] | None = None
    ref: str | None = None


class ReplyIn(StrictBody):  # POST /api/tasks/{id}/reply (the user; SPEC v5)
    text: str
    preset: str | None = None


class TaskAnswerIn(StrictBody):  # POST /api/tasks/{id}/answer (the owner agent; SPEC v5)
    text: str


class TaskPatch(StrictBody):
    title: str | None = None
    context: str | None = None
    status: str | None = None
    waiting_on: str | None = None
    next_check: str | None = None
    links: list[LinkIn] | None = None
    ref: str | None = None


# ---------- auth ----------

def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _resolve(tokens: TokenStore, authorization: str | None) -> Identity:
    token = bearer_token(authorization)
    if token is None:
        raise _unauthorized("missing bearer token")
    identity = tokens.identify(token)
    if identity is None:
        raise _unauthorized("invalid token")
    return identity


def current_identity(request: Request) -> Identity:
    identity = request.scope.get(IDENTITY_SCOPE_KEY)  # set by RequestGuard
    if isinstance(identity, Identity):
        return identity
    tokens = getattr(request.app.state, "tokens", None)
    if tokens is None:  # the hosted bridge: RequestGuard always sets the identity on /api/*
        raise _unauthorized("missing bearer token")
    return _resolve(tokens, request.headers.get("authorization"))


def _error(status_code: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


class RequestGuard:
    """Runs before routing. FastAPI reads and JSON-parses a request body before any dependency runs,
    so without this an unauthenticated client could make the server buffer an arbitrarily large
    upload. Checks the bearer token on /api/* and caps every request body at MAX_BODY.
    (/mcp checks its token itself in MCPEndpoint, also before the body is read.)

    An identity with its own `max_body` (the hosted bridge's upload grant on PUT /api/uploads) is capped at that
    instead, and must send a Content-Length: its route reads the body itself once it holds a memory slot, so
    nothing may be buffered here before that (no chunked bodies).

    authenticate(path, headers, client, method) -> identity (raising HTTPException) replaces the tokens-file check;
    the hosted bridge uses it (its lookups block on the metadata store, so they run in a worker thread); `client`
    is the ASGI (host, port) of the peer, `method` the HTTP method (DELETE /api/me is the app's, not an agent's)."""

    def __init__(self, app, tokens: TokenStore | None = None, max_body: int = MAX_BODY, authenticate=None):
        self.app = app
        self.tokens = tokens
        self.max_body = max_body
        self.authenticate = authenticate

    async def _identify(self, path: str, headers: Headers, client, method: str):
        if self.authenticate is None:
            return _resolve(self.tokens, headers.get("authorization"))
        return await anyio.to_thread.run_sync(self.authenticate, path, headers, client, method)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        identity = None
        if scope["path"].startswith("/api/"):
            try:
                identity = await self._identify(scope["path"], headers, scope.get("client"), scope["method"])
            except HTTPException as exc:
                await _error(exc.status_code, exc.detail, exc.headers)(scope, receive, send)
                return
            scope[IDENTITY_SCOPE_KEY] = identity
        own_cap = getattr(identity, "max_body", None)
        cap = self.max_body if own_cap is None else own_cap
        too_big = _error(413, f"request body too large (max {cap} bytes)")
        length = headers.get("content-length")
        if length is not None:
            if not length.strip().isdigit():
                await _error(400, "invalid Content-Length")(scope, receive, send)
                return
            if int(length) > cap:
                await too_big(scope, receive, send)
                return
        elif own_cap is not None:
            await _error(411, "send the body with a Content-Length header (curl --data-binary @FILE does)")(
                scope, receive, send)
            return
        elif "chunked" in headers.get("transfer-encoding", "").lower():
            # No length up front: read up to the cap here, then hand the buffered body on.
            messages, size = [], 0
            while True:
                message = await receive()
                messages.append(message)
                if message["type"] != "http.request":
                    break
                size += len(message.get("body", b""))
                if size > cap:
                    await too_big(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            original = receive

            async def receive():
                return messages.pop(0) if messages else await original()
        await self.app(scope, receive, send)


class MCPEndpoint:
    """ASGI endpoint for /mcp: same bearer auth as REST, then hands over to the MCP session manager."""

    def __init__(self, tokens: TokenStore, mcp):
        self.tokens = tokens
        self.mcp = mcp

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode("latin-1") or None
        try:
            identity = _resolve(self.tokens, authorization)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            await response(scope, receive, send)
            return
        if scope["method"] == "GET":
            # Stateless server: nothing is ever pushed, so no standalone SSE stream (the spec allows 405).
            # An idle open GET would also hold up a graceful shutdown.
            response = JSONResponse({"detail": "no SSE stream here; POST JSON-RPC to /mcp"}, status_code=405,
                                    headers={"Allow": "POST"})
            await response(scope, receive, send)
            return
        scope[IDENTITY_SCOPE_KEY] = identity
        await self.mcp.session_manager.handle_request(scope, receive, send)


# ---------- app ----------

class WebFiles(StaticFiles):
    """The PWA. `no-cache` makes browsers revalidate every file (a cheap 304 via ETag), so a redeploy
    reaches the home-screen app at once instead of after a heuristic cache lifetime."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors():
        if error.get("type") == "json_invalid":
            return "request body is not valid JSON"
        location = ".".join(str(p) for p in error.get("loc", ()) if p not in ("body", "query", "path"))
        parts.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return "; ".join(parts) or "invalid request"


def setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)  # one INFO line per request otherwise
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the Drive client: one INFO line per request otherwise


def add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AskError)
    async def ask_error_handler(request: Request, exc: AskError):
        # `headers`: an error that tells the client when to come back (the hosted media guard's Retry-After)
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": _validation_message(exc)}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        # Keeps the {"detail": "..."} contract for bugs too; the server still logs the traceback.
        return JSONResponse({"detail": f"internal server error ({type(exc).__name__})"}, status_code=500)


def add_api_routes(app: FastAPI, services, ask_create: Callable[[Identity, dict], dict] | None = None) -> None:
    """REST for asks and tasks (SPEC "REST", v3 "REST"). services(identity) -> (AskService, TaskService):
    the same two for everyone on the single-tenant bridge, the caller's user's Drive on the hosted one.
    ask_create(identity, data): POST /api/asks instead of the AskService's create (the hosted bridge: a create may
    carry attachments, hosted/media_api.py)."""

    @app.get("/api/me")
    def me(who: Identity = Depends(current_identity)):
        return who.as_json()

    @app.get("/api/asks")
    def list_asks(
        who: Identity = Depends(current_identity),
        status: str = "active",
        agent: str | None = None,
        limit: int = Query(200),
    ):
        return services(who)[0].list(status, agent, limit)

    @app.get("/api/asks/{ask_id}")
    def get_ask(ask_id: int, who: Identity = Depends(current_identity)):
        return services(who)[0].get(ask_id)

    @app.post("/api/asks", status_code=201)
    def create_ask(body: AskCreate, who: Identity = Depends(current_identity)):
        data = body.model_dump(exclude_unset=True)
        return ask_create(who, data) if ask_create is not None else services(who)[0].create(who, data)

    @app.patch("/api/asks/{ask_id}")
    def update_ask(ask_id: int, body: AskPatch, who: Identity = Depends(current_identity)):
        # null = leave unchanged, as in MCP ask_update; clear with "" (context, ref) or [] (options, links).
        changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        return services(who)[0].update(who, ask_id, changes)

    @app.post("/api/asks/{ask_id}/answer")
    def answer_ask(ask_id: int, body: AnswerIn, who: Identity = Depends(current_identity)):
        return services(who)[0].answer(who, ask_id, body.answer)

    @app.post("/api/asks/{ask_id}/close")
    def close_ask(ask_id: int, body: CloseIn | None = Body(None), who: Identity = Depends(current_identity)):
        return services(who)[0].close(who, ask_id, body.resolution if body else None)

    @app.post("/api/asks/{ask_id}/reopen")
    def reopen_ask(ask_id: int, who: Identity = Depends(current_identity)):
        return services(who)[0].reopen(who, ask_id)

    # tasks (v3): everyone reads, only the owner agent writes

    @app.get("/api/tasks")
    def list_tasks(
        who: Identity = Depends(current_identity),
        status: str = "open",
        agent: str | None = None,
        limit: int | None = Query(None),
    ):
        return services(who)[1].list(status, agent, limit)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, who: Identity = Depends(current_identity)):
        return services(who)[1].get(task_id)

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskCreate, who: Identity = Depends(current_identity)):
        return services(who)[1].create(who, body.model_dump(exclude_unset=True))

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: int, body: TaskPatch, who: Identity = Depends(current_identity)):
        # null = leave unchanged; clear with "" (context, waiting_on, next_check, ref) or [] (links).
        changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        return services(who)[1].update(who, task_id, changes)

    @app.post("/api/tasks/{task_id}/done")
    def done_task(task_id: int, body: CloseIn | None = Body(None), who: Identity = Depends(current_identity)):
        return services(who)[1].done(who, task_id, body.resolution if body else None)

    @app.post("/api/tasks/{task_id}/drop")
    def drop_task(task_id: int, body: CloseIn | None = Body(None), who: Identity = Depends(current_identity)):
        return services(who)[1].drop(who, task_id, body.resolution if body else None)

    @app.post("/api/tasks/{task_id}/reopen")
    def reopen_task(task_id: int, who: Identity = Depends(current_identity)):
        return services(who)[1].reopen(who, task_id)

    # replies on tasks (v5): the user replies on an open task, the owner agent answers

    @app.post("/api/tasks/{task_id}/reply")
    def reply_task(task_id: int, body: ReplyIn, who: Identity = Depends(current_identity)):
        return services(who)[1].reply(who, task_id, body.model_dump(exclude_unset=True))

    @app.post("/api/tasks/{task_id}/answer")
    def answer_task(task_id: int, body: TaskAnswerIn, who: Identity = Depends(current_identity)):
        return services(who)[1].answer(who, task_id, body.model_dump())


def create_app(settings: Settings | None = None, notifier: Notifier | None = None,
               drive: DriveClient | None = None) -> FastAPI:
    """drive: a Drive client to use instead of the real one (tests); ignored unless settings.drive is on.
    With ASKS_MODE=hosted (settings.mode) this builds the multi-tenant bridge instead (server/hosted/)."""
    settings = settings or Settings.from_env()
    if settings.mode == "hosted":
        from .hosted.app import create_hosted_app  # only the hosted bridge needs its extra dependencies
        return create_hosted_app()
    setup_logging()

    db = Database(settings.db_path)
    tokens = TokenStore(settings.tokens_path)
    if notifier is None:
        notifier = Notifier(settings.notify, settings.tg_chat, token_file=settings.tg_token_file)
    service = AskService(db, notifier)
    tasks = TaskService(db)
    mirror = None
    if settings.drive:  # off: no Drive client, no thread, no network
        drive = drive or DriveClient(settings.drive_client_file, settings.drive_token_file)
        mirror = DriveMirror(service, drive, poll=settings.drive_poll, tasks=tasks)
        service.mirror = mirror
        tasks.mirror = mirror
    web_enabled = settings.web_dir.is_dir()

    mcp = build_mcp(service, tasks)
    # Builds the session manager. Its Starlette app is not used: /mcp is routed to MCPEndpoint below.
    # DNS-rebinding host checks are off because every request must carry a bearer token anyway,
    # and the server may be reached by a private IP or any hostname.
    mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init()
        tokens.refresh()
        log.info(
            "asks: db=%s tokens=%s web=%s notify=%s chat=%s drive=%s",
            settings.db_path, settings.tokens_path, settings.web_dir if web_enabled else "(missing)",
            "on" if notifier.enabled else "off", settings.tg_chat, "on" if mirror else "off",
        )
        problem = notifier.config_problem()
        if problem:
            log.warning("Telegram notifications are on but every message will fail: %s", problem)
        if mirror:
            problem = mirror.drive.config_problem()
            if problem:
                log.warning("Drive mirror is on but cannot sync until this is fixed (it keeps retrying): %s", problem)
            mirror.start()
        async with mcp.session_manager.run():
            yield
        if mirror:
            await anyio.to_thread.run_sync(mirror.stop, 5.0)
        # Give a just-queued Telegram message a moment to go out before the process exits.
        await anyio.to_thread.run_sync(notifier.flush, 5.0)

    # No /docs, /redoc, /openapi.json: they need no token and /docs, /redoc load unpinned CDN scripts
    # on the PWA's origin, where the human's token lives in localStorage. The contract is SPEC.md.
    app = FastAPI(title="Asks", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.tokens = tokens
    app.state.notifier = notifier
    app.state.service = service
    app.state.tasks = tasks
    app.state.mirror = mirror
    app.state.mcp = mcp

    add_error_handlers(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    add_api_routes(app, lambda who: (service, tasks))

    app.router.routes.append(Route("/mcp", endpoint=MCPEndpoint(tokens, mcp), include_in_schema=False))

    if web_enabled:
        app.mount("/", WebFiles(directory=settings.web_dir, html=True), name="web")
    else:
        log.warning("web dir %s not found: serving the API only", settings.web_dir)

    app.add_middleware(RequestGuard, tokens=tokens)
    return app


app = create_app()
