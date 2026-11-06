"""The hosted, multi-tenant bridge (SPEC v4): `ASKS_MODE=hosted uvicorn server.app:app` builds this app.

  /mcp                  MCP (streamable HTTP, stateless) for agents: OAuth access tokens or agent keys
  /api/asks, /api/tasks the same REST as the single-tenant bridge, for agents (same tokens as /mcp)
  /api/agents…, /api/connect-info, /api/events, /api/drive/token, DELETE /api/me
                        the App API (Google ID tokens), app_api.py; the only routes with CORS, and only for the
                        origins in ASKS_CORS_ORIGINS
  /authorize, /token, /register, /revoke, /oauth/…, /.well-known/…   the authorization server, oauth.py
  /healthz

Each agent's data lives in its user's Drive appDataFolder (records.py); everything else in the metadata store.
No local disk state, so any number of instances can run side by side.
"""

import logging
from contextlib import asynccontextmanager

import anyio.to_thread
import httpx
from fastapi import FastAPI, HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..app import RequestGuard, add_api_routes, add_error_handlers, setup_logging
from ..mcp_tools import IDENTITY_SCOPE_KEY, LookTool, build_mcp
from ..tokens import bearer_token
from . import site
from .accounts import SCOPE, Accounts, HostedIdentity
from .app_api import AppCors, AppUser, add_app_api, is_account_deletion, is_app_path
from .clients import CimdResolver
from .config import HostedSettings
from .crypto import Keyring
from .feed import EventFeed
from .google import GoogleError, GoogleOAuth
from .looks import AGENT_COLORS, own_look
from .operator import OperatorPing, telegram_sender
from .oauth import AuthServer
from .ratelimit import RateLimiter, client_ip
from .records import DrivePool, HostedAsks, HostedTasks
from .safe_http import public_client
from .stats import internal_check
from .store import MemoryStore
from .webhooks import WebhookSender

log = logging.getLogger("asks.hosted")


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


class HostedMCPEndpoint:
    """/mcp: bearer check (in a worker thread: it reads the metadata store), then the MCP session manager."""

    def __init__(self, auth: ResourceAuth, mcp, trust_forwarded: bool):
        self.auth = auth
        self.mcp = mcp
        self.trust_forwarded = trust_forwarded

    async def __call__(self, scope, receive, send):
        headers = Headers(scope=scope)
        ip = client_ip(headers, (scope.get("client") or (None,))[0], self.trust_forwarded)
        try:
            identity = await anyio.to_thread.run_sync(self.auth.identify, headers.get("authorization"), ip,
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
        await self.mcp.session_manager.handle_request(scope, receive, send)


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
    auth_server = AuthServer(settings, accounts, google, CimdResolver(public_http), limiter)
    resource_auth = ResourceAuth(settings, accounts, limiter)

    def services(who: HostedIdentity):
        records = pool.records(who.user)
        return HostedAsks(records), HostedTasks(records)

    look = LookTool(AGENT_COLORS, lambda who, changes: own_look(accounts, public_http, limiter, who, changes))
    mcp = build_mcp(services=services, hosted=True, look=look)
    mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Every request needs a bearer token, and Cloud Run answers on its own hostnames too.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def authenticate(path: str, headers, client=None, method: str = "GET") -> HostedIdentity | AppUser:
        """RequestGuard's check for /api/*: Google ID tokens on the App API, agent bearer tokens elsewhere. The first
        App API call of a Google account creates its user, except Delete Account (nothing to create it for)."""
        if not is_app_path(path, method):
            ip = client_ip(headers, (client or (None,))[0], settings.trust_forwarded)
            return resource_auth.identify(headers.get("authorization"), ip, headers.get("host"))
        token = bearer_token(headers.get("authorization"))
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("AskHooman hosted bridge: %s (aliases: %s, CORS for the App API: %s, store=%s)", settings.public_url,
                 ", ".join(settings.aliases) or "none", ", ".join(settings.cors_origins) or "none", settings.store)
        async with mcp.session_manager.run():
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
    app.state.auth_server = auth_server
    app.state.mcp = mcp
    add_error_handlers(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    add_api_routes(app, services)
    add_app_api(app, settings, accounts, pool, webhooks, limiter, google)
    app.router.routes.extend(auth_server.routes())
    app.router.routes.extend(site.routes(settings))
    app.router.routes.append(Route("/mcp", endpoint=HostedMCPEndpoint(resource_auth, mcp, settings.trust_forwarded),
                                   include_in_schema=False))
    app.add_middleware(RequestGuard, authenticate=authenticate)
    if settings.cors_origins:  # added last = outermost: preflights are answered before RequestGuard's token check
        app.add_middleware(AppCors, origins=settings.cors_origins)
    return app
