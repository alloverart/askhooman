"""The hosted, multi-tenant bridge (SPEC v4): `ASKS_MODE=hosted uvicorn server.app:app` builds this app.

  /mcp                  MCP (streamable HTTP, stateless) for agents: OAuth access tokens or agent keys
  /api/asks, /api/tasks the same REST as the single-tenant bridge, for agents (same tokens as /mcp)
  /api/asks/{id}/attachments, /api/asks/{id}/uploads, PUT /api/uploads
                        pictures and video in asks (media_api.py); PUT /api/uploads takes upload tokens (qtu_…,
                        uploads.py) and nothing else, and they work nowhere else
  /api/agents…, /api/connect-info, /api/events, /api/drive/token, DELETE /api/me
                        the App API (Google ID tokens), app_api.py; the only routes with CORS, and only for the
                        origins in ASKS_CORS_ORIGINS
  /media/v1/<token>     a voice answer's recording for the agent that asked (voice_api.py): the token in the path is
                        the credential, no Authorization
  /authorize, /token, /register, /revoke, /oauth/…, /.well-known/…   the authorization server, oauth.py
  /healthz

Each agent's data lives in its user's Drive appDataFolder (records.py); everything else in the metadata store.
No local disk state, so any number of instances can run side by side.
"""

import logging
import time
from contextlib import asynccontextmanager

import anyio.to_thread
import httpx
from fastapi import FastAPI, HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..app import RequestGuard, add_api_routes, add_error_handlers, setup_logging
from ..db import utc_now
from ..mcp_tools import IDENTITY_SCOPE_KEY, FeedTool, LookTool, MediaTool, SecretFileTool, WebhookTool, build_mcp
from ..tokens import bearer_token
from . import site
from .accounts import SCOPE, Accounts, HostedIdentity
from .app_api import AppCors, AppUser, add_app_api, is_account_deletion, is_app_path
from .attachments import AskMedia
from .clients import CimdResolver
from .config import HostedSettings
from .crypto import Keyring
from .feed import EventFeed, add_feed_route
from .files_api import add_file_routes, is_content_path, secret_file_tool
from .google import GoogleError, GoogleOAuth
from .looks import AGENT_COLORS, own_look
from .media_api import MEDIA_SLOTS, MediaGuard, MediaService, Slots, add_media_routes
from .operator import OperatorPing, telegram_sender
from .oauth import AuthServer
from .ratelimit import RateLimiter, client_ip
from .records import DrivePool, HostedAsks, HostedTasks
from .safe_http import public_client
from .self_webhook import ping_own_webhook, set_own_webhook
from .stats import internal_check
from .store import MemoryStore
from .sweep import sweep
from .uploads import UPLOAD_PATH, UPLOAD_PREFIX, UploadGrant, UploadGrants
from .files import FileClaims, FileLinks
from .voice import VoiceLinks
from .voice_api import add_voice_route
from .webhooks import WebhookSender

log = logging.getLogger("asks.hosted")

SWEEP_BUDGET = 600.0  # seconds a run works, files included: the deploy's request timeout is 900
SWEEP_LIMIT = 20  # users per slice
SWEEP_BEHIND_RUNS = 2  # runs in a row that end with users left before the sweep says it is falling behind


class ResourceAuth:
    """The resource server's bearer check (/mcp and the agents' REST): OAuth access tokens issued for our MCP
    URL, or agent keys. Failures are 401 with a WWW-Authenticate challenge naming the protected resource
    metadata (RFC 9728 §5.1), so an MCP client can discover how to sign in.

    Bearers not in our own formats are refused without a store lookup; an address with many failed checks is
    slowed down (429) before any lookup; accepted requests are rate-limited per agent and per user."""

    def __init__(self, settings: HostedSettings, accounts: Accounts, limiter: RateLimiter):
        self.settings = settings
        self.accounts = accounts
        self.limiter = limiter

    def challenge(self, origin: str, error: str | None = None, description: str | None = None) -> str:
        parts = [f'resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"', f'scope="{SCOPE}"']
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        return "Bearer " + ", ".join(parts)

    def identify(self, authorization: str | None, ip: str = "unknown", host: str | None = None) -> HostedIdentity:
        """host: the request's Host header. A token is valid only on the origin it was issued for (its audience):
        the public URL, or an alias such as the domain before a rename."""
        origin = self.settings.origin(host)
        token = bearer_token(authorization)
        if token is None:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": self.challenge(origin)})
        if self.limiter.blocked("bearer_fail", ip):
            raise HTTPException(429, "too many invalid tokens from this address", headers={"Retry-After": "60"})
        try:
            identity = self.accounts.identify(token, self.settings.resource_for(origin))
        except Exception as exc:  # the metadata store is down: say so, as JSON, without details
            log.exception("bearer check failed: %s", type(exc).__name__)
            raise HTTPException(503, "temporarily unavailable, try again", headers={"Retry-After": "5"}) from None
        if identity is None:
            self.limiter.allow("bearer_fail", ip)
            raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": self.challenge(
                origin, "invalid_token", "the token is invalid, expired or revoked")})
        if not self.limiter.allow("agent", identity.agent_id) or not self.limiter.allow("user", identity.user):
            raise HTTPException(429, "too many requests", headers={"Retry-After": "60"})
        return identity

    def chatgpt(self, identity: HostedIdentity) -> bool:
        """The token was issued to ChatGPT's OAuth client (Accounts.is_chatgpt_client)."""
        try:
            return self.accounts.is_chatgpt_client(identity.client_id)
        except Exception as exc:  # the metadata store is down
            log.exception("client check failed: %s", type(exc).__name__)
            raise HTTPException(503, "temporarily unavailable, try again", headers={"Retry-After": "5"}) from None


class HostedMCPEndpoint:
    """/mcp: bearer check (in a worker thread: it reads the metadata store), then the MCP session manager of the
    caller's server: ChatGPT's OAuth client gets the one without secret files (build_mcp(chatgpt=True), SPEC 7.8
    «ChatGPT: no secret files»), every other client and agent key the full one."""

    def __init__(self, auth: ResourceAuth, mcp, chatgpt_mcp, trust_forwarded: bool):
        self.auth = auth
        self.mcp = mcp
        self.chatgpt_mcp = chatgpt_mcp
        self.trust_forwarded = trust_forwarded

    def caller(self, authorization: str | None, ip: str, host: str | None) -> tuple[HostedIdentity, object]:
        identity = self.auth.identify(authorization, ip, host)
        return identity, self.chatgpt_mcp if self.auth.chatgpt(identity) else self.mcp

    async def __call__(self, scope, receive, send):
        headers = Headers(scope=scope)
        ip = client_ip(headers, (scope.get("client") or (None,))[0], self.trust_forwarded)
        try:
            identity, mcp = await anyio.to_thread.run_sync(self.caller, headers.get("authorization"), ip,
                                                           headers.get("host"))
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(
                scope, receive, send)
            return
        if scope["method"] == "GET":  # stateless: nothing is pushed, no SSE stream (as on the single-tenant bridge)
            await JSONResponse({"detail": "no SSE stream here; POST JSON-RPC to /mcp"}, status_code=405,
                               headers={"Allow": "POST"})(scope, receive, send)
            return
        scope[IDENTITY_SCOPE_KEY] = identity
        await mcp.session_manager.handle_request(scope, receive, send)


def make_store(settings: HostedSettings):
    if settings.store == "memory":
        log.warning("ASKS_STORE=memory: agents, tokens and keys live in this process only (development)")
        return MemoryStore()
    from .firestore_store import FirestoreStore  # needs google-cloud-firestore (deploy/hosted/requirements.txt)
    return FirestoreStore(settings.firestore_project, settings.firestore_database)


def create_hosted_app(settings: HostedSettings | None = None, *, store=None, http: httpx.Client | None = None,
                      public_http: httpx.Client | None = None, webhooks: WebhookSender | None = None,
                      operator: OperatorPing | None = None) -> FastAPI:
    """Tests pass an in-memory store, `http` (Google + Drive) and `public_http` (CIMD documents, webhooks)
    with fake transports; production builds the real ones."""
    setup_logging()
    settings = settings or HostedSettings.from_env()
    keyring = Keyring(settings.encryption_keys)
    store = store if store is not None else make_store(settings)
    http = http or httpx.Client(timeout=20.0)
    public_http = public_http or public_client()
    internal = internal_check(settings.admin_emails, settings.internal_emails)
    if operator is None and settings.operator_chat and settings.operator_token:
        operator = OperatorPing(telegram_sender(settings.operator_token, http), settings.operator_chat, internal,
                                site.WEB_APP)
    feed = EventFeed(store)  # SPEC v5: each agent's events, read by its long poll / ask_wait
    accounts = Accounts(store, keyring, settings, on_new_user=operator.new_user if operator else None,
                        feed=feed)
    google = GoogleOAuth(settings.web_client_id, settings.web_client_secret, settings.google_redirect_uri, http)
    limiter = RateLimiter()
    pool = DrivePool(accounts, settings.web_client_id, settings.web_client_secret, http,
                     link_url=f"{settings.public_url}/oauth/link")
    webhooks = webhooks or WebhookSender(public_http)
    if webhooks.on_result is None:  # each delivery's outcome becomes the agent's last_delivery
        webhooks.on_result = accounts.record_delivery
    auth_server = AuthServer(settings, accounts, google, CimdResolver(public_http), limiter)
    resource_auth = ResourceAuth(settings, accounts, limiter)

    # Voice answers (SPEC v5 "5. Voice answers"): the agent that asked gets a link to the recording on each read.
    voice_links = VoiceLinks(keyring, settings.public_url)
    # Files in answers (SPEC 7.4): the same for a normal file, sealed under its own context.
    file_links = FileLinks(keyring, settings.public_url)
    file_claims = FileClaims(store)

    def services(who: HostedIdentity):
        records = pool.records(who.user)
        return HostedAsks(records, voice_links, who, file_links, file_claims), HostedTasks(records)

    # Pictures and video in asks (SPEC v5 step 4): one service and one guard (the `media` bucket, the slots) for
    # the REST routes and the MCP tools alike.
    grants = UploadGrants(store)
    media = MediaService(lambda who: AskMedia(pool.records(who.user)), public_http, grants,
                         MediaGuard(limiter, Slots(MEDIA_SLOTS)), settings.public_url)

    look = LookTool(AGENT_COLORS, lambda who, changes: own_look(accounts, public_http, limiter, who, changes))

    async def wait_for_user(who: HostedIdentity, timeout: float) -> dict:
        """ask_wait: the caller's feed from the cursor the bridge keeps for MCP (MCP clients keep no state)."""
        await anyio.to_thread.run_sync(accounts.listened, who.agent_id)
        cursor = await anyio.to_thread.run_sync(feed.mcp_cursor, who.agent_id)
        page = await feed.wait(who.agent_id, cursor, timeout)
        await anyio.to_thread.run_sync(feed.set_mcp_cursor, who.agent_id, page["cursor"])
        return {"events": page["events"], **({"missed": True} if page.get("missed") else {})}

    own_webhook = WebhookTool(
        lambda who, url, rotate: set_own_webhook(accounts, limiter, who, url, rotate),
        lambda who: ping_own_webhook(accounts, webhooks, limiter, who))
    def hosted_mcp(chatgpt: bool):
        """The MCP server for every client but ChatGPT, or ChatGPT's: the same tools on the same services, without
        secret files (SPEC 7.8 «ChatGPT: no secret files»)."""
        secret_file = None if chatgpt else SecretFileTool(lambda who, ask_id, n: secret_fetch(who, ask_id, n))
        server = build_mcp(services=services, hosted=True, look=look, feed=FeedTool(wait_for_user),
                           webhook=own_webhook, media=MediaTool(media.create, media.attach, media.upload, media.detach),
                           secret_file=secret_file, chatgpt=chatgpt)
        server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            # Every request needs a bearer token, and Cloud Run answers on its own hostnames too.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        return server

    mcp, chatgpt_mcp = hosted_mcp(chatgpt=False), hosted_mcp(chatgpt=True)

    def upload_grant(token: str | None, ip: str) -> UploadGrant:
        """PUT /api/uploads: an upload token only, claimed here (used up, whatever the outcome of the PUT)."""
        challenge = {"WWW-Authenticate": "Bearer"}
        if token is None or not token.startswith(UPLOAD_PREFIX):
            raise HTTPException(401, "PUT /api/uploads takes the upload token from ask_upload "
                                     "(Authorization: Bearer qtu_…)", headers=challenge)
        if limiter.blocked("bearer_fail", ip):
            raise HTTPException(429, "too many invalid tokens from this address", headers={"Retry-After": "60"})
        try:
            grant = grants.claim(token)
            agent = accounts.own_agent(grant.user, grant.agent_id) if grant is not None else None
        except Exception as exc:  # the metadata store is down
            log.exception("upload token check failed: %s", type(exc).__name__)
            raise HTTPException(503, "temporarily unavailable, try again", headers={"Retry-After": "5"}) from None
        if grant is None:
            limiter.allow("bearer_fail", ip)
            raise HTTPException(401, "the upload token is invalid, expired or already used: get a new one with "
                                     "ask_upload", headers=challenge)
        if agent is None:
            raise HTTPException(401, "the agent this upload was for was disconnected", headers=challenge)
        return grant

    def authenticate(path: str, headers, client=None, method: str = "GET") -> HostedIdentity | AppUser | UploadGrant:
        """RequestGuard's check for /api/*: Google ID tokens on the App API, upload tokens on PUT /api/uploads (and
        nowhere else), agent bearer tokens elsewhere. The first App API call of a Google account creates its user,
        except Delete Account (nothing to create it for)."""
        token = bearer_token(headers.get("authorization"))
        ip = client_ip(headers, (client or (None,))[0], settings.trust_forwarded)
        if path == UPLOAD_PATH and method == "PUT":
            return upload_grant(token, ip)
        if token is not None and token.startswith(UPLOAD_PREFIX):  # not looked up: it is never valid here
            raise HTTPException(401, "an upload token works only for its own PUT /api/uploads",
                                headers={"WWW-Authenticate": "Bearer"})
        if not is_app_path(path, method):
            if token is not None and is_content_path(path) and token.count(".") == 2:  # a Google ID token?
                try:
                    google.verify_id_token(token, settings.app_client_ids)
                except Exception:  # GoogleError, or Google's certs unreachable: not a Google ID token as far as we
                    pass  # can tell; the agent check below says what is wrong with it
                else:
                    raise HTTPException(403, "this file is for the agent that asked: fetch it with its own token")
            return resource_auth.identify(headers.get("authorization"), ip, headers.get("host"))
        if token is None:
            raise HTTPException(401, "sign in with Google", headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = google.verify_id_token(token, settings.app_client_ids)
        except GoogleError as exc:
            raise HTTPException(401, exc.public, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
        try:
            if accounts.user(claims["sub"]) is None and not is_account_deletion(path, method):
                accounts.touch_user(claims["sub"], claims.get("email"))
        except Exception as exc:
            log.exception("app user lookup failed: %s", type(exc).__name__)
            raise HTTPException(503, "temporarily unavailable, try again", headers={"Retry-After": "5"}) from None
        return AppUser(claims["sub"], claims.get("email"), claims.get("email_verified") in (True, "true"))

    def sweep_caller_ok(headers, client) -> bool:
        """POST /internal/sweep: a Google OIDC ID token of the configured service account for this service's URL
        (SPEC 7.6). A failed check counts against the usual bearer-failure limit; a blocked address is refused."""
        ip = client_ip(headers, (client or (None,))[0], settings.trust_forwarded)
        if limiter.blocked("bearer_fail", ip):
            return False
        token = bearer_token(headers.get("authorization"))
        ok = False
        if token is not None and settings.sweep_service_account and token.count(".") == 2:
            try:
                claims = google.verify_id_token(token, [settings.public_url])
                ok = (isinstance(claims.get("email"), str) and claims["email"].lower() == settings.sweep_service_account
                      and claims.get("email_verified") in (True, "true"))
            except Exception:  # GoogleError, or Google's certs unreachable: not proven, so not let in
                ok = False
        if not ok:
            limiter.allow("bearer_fail", ip)
        return ok

    async def sweep_endpoint(request: Request) -> Response:
        if not await anyio.to_thread.run_sync(sweep_caller_ok, request.headers, request.client and
                                               (request.client.host, request.client.port)):
            return Response(status_code=401)  # nothing said
        explicit = request.query_params.get("cursor") or None
        scheduled = explicit is None and "limit" not in request.query_params  # Cloud Scheduler's bare POST
        try:
            limit = min(max(int(request.query_params.get("limit", SWEEP_LIMIT)), 1), 200)
        except ValueError:
            limit = SWEEP_LIMIT
        started = time.monotonic()
        deadline, totals = started + SWEEP_BUDGET, {}
        # a bare POST (Cloud Scheduler) resumes where the last run stopped: the cursor is kept in the store
        cursor = explicit or ((await anyio.to_thread.run_sync(store.get, "sweep", "cursor")) or {}).get("cursor")
        users = await anyio.to_thread.run_sync(store.all, "users")  # once per request; the slices are cut from it
        while True:  # slices until every user is done or the budget is
            cursor, stats = await anyio.to_thread.run_sync(
                lambda: sweep(store, pool, time.time(), cursor, limit, users, deadline))
            for key, count in stats.items():
                totals[key] = totals.get(key, 0) + count
            if not explicit:
                await anyio.to_thread.run_sync(store.put, "sweep", "cursor", {"cursor": cursor})
            if cursor is None or time.monotonic() >= deadline:
                break
        log.info("hosted sweep: %s, next cursor %s", totals, "yes" if cursor else "none")
        try:  # what the operator sees (GET /api/admin/stats): when it ran and what it did, counts only
            try:
                before = (await anyio.to_thread.run_sync(store.get, "sweep", "last_run")) or {}
            except Exception as exc:  # unknown count: the note is still written (a finished run resets it)
                log.warning("hosted sweep: could not read the last run: %s", type(exc).__name__)
                before = {}
            if scheduled:  # runs in a row with work left: only the scheduler's runs count, a hand-made one is not it
                behind = (before.get("more_in_a_row", 0) + 1) if cursor is not None else 0
            else:
                behind = before.get("more_in_a_row", 0)
            await anyio.to_thread.run_sync(
                store.put, "sweep", "last_run",
                {"at": utc_now(), "stats": totals, "more": cursor is not None, "more_in_a_row": behind})
            if scheduled and behind >= SWEEP_BEHIND_RUNS:
                log.warning("hosted sweep falling behind: %d runs in a row ended with users left (counts %s): the "
                            "slice or the budget is too small for the number of users", behind, totals)
        except Exception as exc:  # the run is done; a note that did not get written is not an error
            log.warning("hosted sweep: could not note the run: %s", type(exc).__name__)
        return JSONResponse({"next_cursor": cursor, "stats": totals})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("AskHooman hosted bridge: %s (aliases: %s, CORS for the App API: %s, store=%s)", settings.public_url,
                 ", ".join(settings.aliases) or "none", ", ".join(settings.cors_origins) or "none", settings.store)
        if not settings.sweep_service_account:
            log.warning("hosted: ASKS_SWEEP_SERVICE_ACCOUNT is not set: POST /internal/sweep refuses everyone, so no "
                        "answer file is ever swept (a secret file nobody fetched is not deleted after 24 h, the files "
                        "of a closed ask stay): set it and create the Cloud Scheduler job, deploy/hosted/README.md, "
                        "Upgrading an existing deployment")
        async with mcp.session_manager.run(), chatgpt_mcp.session_manager.run():
            yield
        await anyio.to_thread.run_sync(webhooks.flush, 5.0)
        pool.close()

    app = FastAPI(title="AskHooman", version="0.4.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.settings = settings
    app.state.tokens = None
    app.state.accounts = accounts
    app.state.store = store
    app.state.pool = pool
    app.state.webhooks = webhooks
    app.state.feed = feed
    app.state.limiter = limiter
    app.state.uploads = grants
    app.state.media = media
    app.state.voice_links = voice_links
    app.state.file_links = file_links
    app.state.file_claims = file_claims
    app.state.auth_server = auth_server
    app.state.mcp = mcp
    app.state.chatgpt_mcp = chatgpt_mcp
    add_error_handlers(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    add_api_routes(app, services, ask_create=media.create)
    add_media_routes(app, media)
    file_download = add_file_routes(app, file_links, accounts, pool, limiter, file_claims)
    secret_fetch = secret_file_tool(app, accounts, pool, limiter, file_claims)  # ask_secret_file (built before app)
    add_voice_route(app, voice_links, accounts, pool, limiter, media.guard.slots, file_download)
    add_app_api(app, settings, accounts, pool, webhooks, limiter, google, feed, voice_links)
    add_feed_route(app, feed, accounts)
    app.router.routes.extend(auth_server.routes())
    app.router.routes.extend(site.routes(settings))
    app.router.routes.append(Route("/internal/sweep", endpoint=sweep_endpoint, methods=["POST"],
                                   include_in_schema=False))  # no CORS: AppCors answers its own paths only
    app.router.routes.append(Route("/mcp", endpoint=HostedMCPEndpoint(resource_auth, mcp, chatgpt_mcp,
                                                                      settings.trust_forwarded),
                                   include_in_schema=False))
    app.add_middleware(RequestGuard, authenticate=authenticate)
    if settings.cors_origins:  # added last = outermost: preflights are answered before RequestGuard's token check
        app.add_middleware(AppCors, origins=settings.cors_origins)
    return app
