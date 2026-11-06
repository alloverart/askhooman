"""App API of the hosted bridge (SPEC v4 "App API"), for the AskHooman app's "Connect an agent" and
"Connected agents" screens. Authenticated with the user's Google ID token (audience = the iOS client).

  GET    /api/agents             the user's connected agents (each with its look: color, avatar)
  PATCH  /api/agents/{id}        {name?, webhook_url?, rotate_webhook_secret?, wake?, color?, avatar?}
                                 -> agent (+ webhook_secret, once)
  DELETE /api/agents/{id}        revoke: every token and key of the agent stops working at once
  POST   /api/agents/keys        {name} -> 201 {agent, key}: an agent key (qtk_…), shown only in this response
  GET    /api/connect-info       the MCP URL and per-client setup snippets
  POST   /api/events             {ask_id, kind: answered|closed|reopened} after the user acted in the app
                                 -> the asking agent's webhook is called (webhooks.py)
  POST   /api/drive/token        a Drive access token (drive.appdata) made with the Google grant the bridge holds for
                                 the user, so the web app keeps reading their Drive after its own token ran out
                                 (DriveTokens) -> {access_token, expires_in, scope}; 409 {detail, error} without a
                                 usable grant (error: not_linked | invalid_grant)
  DELETE /api/me                 Delete Account: revoke the bridge's Google grant (best effort), delete everything
                                 the server keeps for the user -> 204; nothing left to delete -> 404.
                                 The asks and tasks in the user's Drive stay (they are the user's files).
                                 (GET /api/me is the agents' REST: who the agent token belongs to.)
  GET    /api/account            {email, admin}: who is signed in; admin = the app shows the operator's Admin page
  GET    /api/admin/stats        the operator's stats (stats.admin_stats): ASKS_ADMIN_EMAILS only, else 403

The web app calls these from a browser on another origin: CORS for them, and only them, is opened to the origins
in ASKS_CORS_ORIGINS (AppCors); empty by default, then CORS stays closed.
"""

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.datastructures import Headers, MutableHeaders

from ..db import utc_now
from ..mcp_tools import IDENTITY_SCOPE_KEY
from ..rules import Conflict, Invalid, NotFound
from .accounts import Accounts, NameTaken, agent_json
from .config import HostedSettings
from .crypto import CryptoError
from .google import DRIVE_SCOPE, GoogleError, GoogleOAuth
from .looks import clean_avatar, clean_color
from .ratelimit import RateLimiter
from .records import DrivePool, HostedAsks
from .stats import admin_stats, internal_check
from .webhooks import WebhookSender, check_webhook_url

log = logging.getLogger("asks.hosted.app_api")

EVENT_STATUS = {"answered": "answered", "closed": "closed", "reopened": "open"}
DRIVE_TOKEN_PATH = "/api/drive/token"
DRIVE_TOKEN_REUSE = 15 * 60  # seconds: a cached Drive token with at least this long left is handed out again
DRIVE_TOKEN_USERS = 10000  # users whose Drive token one instance keeps
DRIVE_TOKEN_STRIPES = 64
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
ACCOUNT_PATH, ADMIN_STATS_PATH = "/api/account", "/api/admin/stats"


@dataclass(frozen=True)
class AppUser:
    """The signed-in app user (from a verified Google ID token)."""
    sub: str
    email: str | None = None
    email_verified: bool = False  # Google's claim; the admin check needs it


def is_app_path(path: str, method: str = "GET") -> bool:
    """App API requests (Google ID tokens); the other /api/* are the agents' (OAuth access tokens, agent keys)."""
    return (path == "/api/agents" or path.startswith("/api/agents/")
            or path in ("/api/connect-info", "/api/events", DRIVE_TOKEN_PATH, ACCOUNT_PATH, ADMIN_STATS_PATH)
            or is_account_deletion(path, method))


def is_account_deletion(path: str, method: str) -> bool:
    return path == "/api/me" and method == "DELETE"


CORS_METHODS = ("GET", "POST", "PATCH", "DELETE")  # the App API's methods
CORS_PREFLIGHT = {
    "Access-Control-Allow-Methods": ", ".join(CORS_METHODS),
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "600",
}


class AppCors:
    """CORS for the App API, for the web app on the origins in ASKS_CORS_ORIGINS (exact strings, never "*").

    Sits outside RequestGuard: a preflight carries no token, so an allowed one is answered here (204) before the
    token check, and the Access-Control-Allow-Origin of an actual response is added here, so the browser can read
    the errors too (401 from RequestGuard, 404, 409, 422, 429). A preflight is judged by the method it asks for
    (Access-Control-Request-Method): DELETE /api/me is the App API's, GET /api/me the agents'. Everything else
    passes through untouched: other origins, the agents' /api/*, /mcp and the OAuth endpoints get no CORS headers,
    and their preflights fail as before (401 or 405). No credentials: the app sends its ID token as a bearer."""

    def __init__(self, app, origins: tuple[str, ...]):
        self.app = app
        self.origins = frozenset(origins)

    async def __call__(self, scope, receive, send):
        request = Headers(scope=scope) if scope["type"] == "http" else Headers()
        origin = request.get("origin")
        if origin not in self.origins:
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        requested = request.get("access-control-request-method")
        if method == "OPTIONS" and requested is not None:  # a preflight
            if requested in CORS_METHODS and is_app_path(path, requested):
                headers = {"Access-Control-Allow-Origin": origin, **CORS_PREFLIGHT, "Vary": "Origin"}
                await Response(status_code=204, headers=headers)(scope, receive, send)
            else:
                await self.app(scope, receive, send)
            return
        if not is_app_path(path, method):
            await self.app(scope, receive, send)
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Access-Control-Allow-Origin"] = origin
                headers.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, send_with_cors)


class NoDriveGrant(Exception):
    """The bridge holds no usable Google grant for the user. error: not_linked (none stored) or invalid_grant
    (Google said it was revoked or expired; it is forgotten, as when an agent's call finds it dead)."""

    def __init__(self, error: str, detail: str):
        super().__init__(detail)
        self.error = error
        self.detail = detail


class DriveTokens:
    """Drive access tokens for the web app (POST /api/drive/token), made with the Google grant the bridge holds for
    the user (from connecting an agent, or /oauth/link). The browser's own Drive token lives an hour and Google opens
    its window for a new one only from a click; this one needs no click. Both OAuth clients belong to one Google
    Cloud project, so the token reaches the same appDataFolder. Google is asked for drive.appdata only.

    Per instance, a user's token is handed out again while it has DRIVE_TOKEN_REUSE left and the grant it came from
    is still the stored one; calls to Google are serialised per user and limited per user ("drive_token" in
    ratelimit.py), so a client can't make the bridge hammer Google. Tokens live in memory only, never in the store
    or a log line."""

    def __init__(self, accounts: Accounts, google: GoogleOAuth, limiter: RateLimiter, link_url: str,
                 clock=time.monotonic, size: int = DRIVE_TOKEN_USERS):
        self.accounts = accounts
        self.google = google
        self.limiter = limiter
        self.link_url = link_url
        self.clock = clock
        self.size = size
        # sub -> (access token, expires at on self.clock, scope, google_linked_at of the grant it came from)
        self._held: OrderedDict[str, tuple[str, float, str, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._stripes = [threading.Lock() for _ in range(DRIVE_TOKEN_STRIPES)]

    def get(self, sub: str) -> dict:
        """{access_token, expires_in, scope} for the user's own grant; NoDriveGrant, or HTTPException 429 / 503."""
        with self._stripes[hash(sub) % DRIVE_TOKEN_STRIPES]:
            user = self.accounts.user(sub) or {}
            if not user.get("google_refresh"):
                self.forget(sub)
                raise NoDriveGrant("not_linked", "AskHooman's server has no access to this account's Google Drive "
                                                 f"(connect an agent, or allow it at {self.link_url})")
            linked_at = str(user.get("google_linked_at") or "")
            now = self.clock()
            with self._lock:
                held = self._held.get(sub)
                if held is not None:
                    self._held.move_to_end(sub)
            if held is not None and held[3] == linked_at and held[1] - now >= DRIVE_TOKEN_REUSE:
                return self._reply(held, now)
            if not self.limiter.allow("drive_token", sub):
                raise HTTPException(429, "too many Drive token requests",
                                    headers={"Retry-After": str(self.limiter.retry_after("drive_token")), **NO_STORE})
            try:
                refresh = self.accounts.google_refresh(sub)
            except CryptoError:  # sealed with a key that is no longer configured: unusable until linked again
                refresh = None
            if not refresh:
                raise NoDriveGrant("not_linked", "AskHooman's server can't use its Google Drive access for this "
                                                 f"account (allow it again at {self.link_url})")
            try:
                body = self._refresh(refresh)
            except GoogleError as exc:
                if exc.reason == "invalid_grant":
                    self._forget_grant(sub, refresh)
                    raise NoDriveGrant("invalid_grant", "Google ended AskHooman's server access to this account's "
                                                        f"Drive (allow it again at {self.link_url})") from None
                log.warning("no Drive token for the web app: %s", exc)
                raise HTTPException(503, "Google is not available right now, try again",
                                    headers={"Retry-After": "30", **NO_STORE}) from None
            scope = str(body.get("scope") or DRIVE_SCOPE)
            if DRIVE_SCOPE not in scope.split():
                raise NoDriveGrant("not_linked", "AskHooman's server has no Google Drive access for this account "
                                                 f"(allow it at {self.link_url})")
            try:
                lifetime = float(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                lifetime = 3600.0
            held = (body["access_token"], now + lifetime, scope, linked_at)
            with self._lock:
                self._held[sub] = held
                self._held.move_to_end(sub)
                while len(self._held) > self.size:
                    self._held.popitem(last=False)
            return self._reply(held, now)

    def _refresh(self, refresh: str) -> dict:
        try:
            return self.google.refresh(refresh, DRIVE_SCOPE)
        except GoogleError as exc:
            if exc.reason != "invalid_scope":
                raise
        # Google wouldn't narrow it: the token then carries the whole grant, openid email drive.appdata (the
        # scopes the web app's own token has too).
        return self.google.refresh(refresh)

    def _forget_grant(self, sub: str, refresh: str) -> None:
        """Google said the grant is dead: forget it (the app can offer to link again), unless the user linked a new
        one meanwhile."""
        self.forget(sub)
        try:
            current = self.accounts.google_refresh(sub)
        except CryptoError:
            current = None
        if current == refresh:
            self.accounts.unlink_google(sub)

    @staticmethod
    def _reply(held: tuple[str, float, str, str], now: float) -> dict:
        return {"access_token": held[0], "expires_in": max(int(held[1] - now), 0), "scope": held[2]}

    def forget(self, sub: str) -> None:
        with self._lock:
            self._held.pop(sub, None)


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    webhook_url: str | None = None
    rotate_webhook_secret: bool = False
    wake: StrictBool | None = None  # SPEC v5: off = no feed events, no webhook calls
    color: str | None = None  # a palette key (looks.AGENT_COLORS)
    avatar: dict | None = None  # {kind: robot} | {kind: emoji, emoji} | {kind: image, data: data URL}


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    existing: bool = False  # add the key to the user's existing agent of that name (else 409 if there is one)


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ask_id: int
    kind: Literal["answered", "closed", "reopened"]


def connect_info(settings: HostedSettings, drive_linked: bool) -> dict:
    url = settings.resource
    return {
        "server_url": url,
        "drive_linked": drive_linked,
        "link_url": f"{settings.public_url}/oauth/link",
        "clients": [
            {"id": "claude", "name": "Claude (web, desktop and mobile)",
             "steps": "Settings → Connectors → Add custom connector → paste the URL → Connect"},
            {"id": "claude-code", "name": "Claude Code",
             "command": f"claude mcp add --scope user --transport http askhooman {url}",
             # --scope user: without it the server exists only in the folder the command ran in. Claude Code reads
             # its servers when it starts, so an open session sees the new one only after a restart (02.11.2026).
             "steps": "Run the command in a terminal, then start Claude Code (restart it if it is open) → /mcp → "
                      "askhooman → Authenticate"},
            {"id": "vscode", "name": "VS Code",
             "config": {"servers": {"askhooman": {"type": "http", "url": url}}},
             "steps": "Add to .vscode/mcp.json (or the user mcp.json) and start the server"},
            {"id": "other", "name": "Any MCP client with OAuth",
             "steps": "Add a remote (streamable HTTP) MCP server with the URL; sign in with Google when asked"},
            {"id": "agent-key", "name": "Clients without OAuth (agent key)",
             "command": f'claude mcp add --scope user --transport http askhooman {url} '
                        '--header "Authorization: Bearer <agent key>"',
             "steps": "Create an agent key in the app and send it as the header Authorization: Bearer <agent key>"},
        ],
    }


def add_app_api(app: FastAPI, settings: HostedSettings, accounts: Accounts, pool: DrivePool,
                webhooks: WebhookSender, limiter: RateLimiter, google: GoogleOAuth) -> None:
    drive_tokens = DriveTokens(accounts, google, limiter, f"{settings.public_url}/oauth/link")
    app.state.drive_tokens = drive_tokens

    def app_user(request: Request) -> AppUser:
        user = request.scope.get(IDENTITY_SCOPE_KEY)  # set by RequestGuard from the ID token
        if not isinstance(user, AppUser):
            raise HTTPException(401, "sign in with Google", headers={"WWW-Authenticate": "Bearer"})
        if not limiter.allow("app", user.sub):
            raise HTTPException(429, "too many requests", headers={"Retry-After": "60"})
        return user

    def own(user: AppUser, agent_id: str) -> dict:
        agent = accounts.own_agent(user.sub, agent_id)
        if agent is None:
            raise NotFound(f"no agent {agent_id}")
        return agent

    @app.get("/api/agents")
    def list_agents(user: AppUser = Depends(app_user)):
        # Agents from before colours existed get theirs here, once (accounts.ensure_colors).
        return {"agents": [agent_json(a) for a in accounts.ensure_colors(accounts.agents(user.sub))]}

    @app.patch("/api/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentPatch, user: AppUser = Depends(app_user)):
        agent = own(user, agent_id)
        given = body.model_dump(exclude_unset=True)
        look = {}  # checked first, written last: a refused name or webhook leaves the look as it was
        if "color" in given:
            look["color"] = clean_color(given["color"])
        if "avatar" in given:
            look["avatar"] = clean_avatar(given["avatar"])
        if body.name is not None:
            agent = accounts.rename_agent(agent, body.name)
        secret = None
        if "webhook_url" in given or body.rotate_webhook_secret:
            url = given.get("webhook_url", agent.get("webhook_url"))
            url = url.strip() if isinstance(url, str) else None
            if url:
                try:
                    check_webhook_url(url)
                except ValueError as exc:
                    raise Invalid(str(exc)) from None
            elif body.rotate_webhook_secret:
                raise Invalid("set a webhook_url to have a webhook secret")
            agent, secret = accounts.set_webhook(agent, url or None, rotate=body.rotate_webhook_secret)
        if body.wake is not None:
            agent = accounts.set_wake(agent, body.wake)
        if look:
            agent = accounts.set_look(agent, **look)
        reply = {"agent": agent_json(agent)}
        if secret:
            reply["webhook_secret"] = secret  # shown once; store it in the receiver
        return reply

    @app.delete("/api/agents/{agent_id}")
    def revoke_agent(agent_id: str, user: AppUser = Depends(app_user)):
        return {"agent": agent_json(accounts.revoke_agent(own(user, agent_id)))}

    @app.post("/api/agents/keys", status_code=201)
    def create_key(body: KeyIn, user: AppUser = Depends(app_user)):
        try:
            agent, key = accounts.create_key(user.sub, body.name, existing=body.existing)
        except NameTaken as taken:
            raise Conflict(f"an agent named {taken.agent['name']} is already connected; send existing: true to "
                           "give it a key, or choose another name") from None
        return {"agent": agent_json(agent), "key": key, "drive_linked": accounts.drive_linked(user.sub),
                "link_url": f"{settings.public_url}/oauth/link"}

    internal = internal_check(settings.admin_emails, settings.internal_emails)

    def is_admin(user: AppUser) -> bool:
        return user.email_verified and (user.email or "").lower() in settings.admin_emails

    @app.get(ACCOUNT_PATH)
    def get_account(user: AppUser = Depends(app_user)):
        """Who is signed in, and whether they see the operator's stats (the app shows its Admin page then)."""
        return JSONResponse({"email": user.email, "admin": is_admin(user)}, headers=NO_STORE)

    @app.get(ADMIN_STATS_PATH)
    def get_admin_stats(user: AppUser = Depends(app_user)):
        """The operator's view (SPEC "Operator stats"): ASKS_ADMIN_EMAILS only."""
        if not is_admin(user):
            raise HTTPException(403, "only the operator's admins see the stats")
        stats = admin_stats(accounts.store.all("users").values(), accounts.store.all("agents").values(),
                            datetime.now(timezone.utc), internal)
        return JSONResponse(stats, headers=NO_STORE)

    @app.get("/api/connect-info")
    def get_connect_info(user: AppUser = Depends(app_user)):
        return connect_info(settings, accounts.drive_linked(user.sub))

    @app.post("/api/events", status_code=202)
    def post_event(body: EventIn = Body(...), user: AppUser = Depends(app_user)):
        ask = HostedAsks(pool.records(user.sub)).get(body.ask_id)
        if ask["status"] != EVENT_STATUS[body.kind]:
            raise Conflict(f"ask #{ask['id']} is {ask['status']} now, not {body.kind}")
        events = [e for e in ask.pop("events") if e.get("kind") == body.kind]
        event = {"id": events[-1].get("id"), "at": events[-1].get("at")} if events else None
        payload = {"type": f"ask.{body.kind}", "ask": ask, "event": event, "sent_at": utc_now()}
        if body.kind == "answered":
            payload["answer"] = ask["answer"]
        elif body.kind == "closed":
            payload["resolution"] = ask["resolution"]
        queued = 0
        for agent in accounts.agents(user.sub):
            if agent["slug"] != ask["agent"] or not agent.get("webhook_url"):
                continue
            secret = accounts.webhook_secret(agent)
            if not secret:
                continue
            delivery = uuid.uuid4().hex
            webhooks.send(agent["webhook_url"], secret, payload["type"], delivery, {**payload, "delivery_id": delivery},
                          agent_id=agent["id"])
            queued += 1
        return {"queued": queued}

    @app.post(DRIVE_TOKEN_PATH)
    def drive_token(user: AppUser = Depends(app_user)):
        """The web app's Drive access after its own token ran out, without Google's window. Never cached by the
        browser or a proxy; 409 tells the app to fall back to "Continue with Google"."""
        try:
            token = drive_tokens.get(user.sub)
        except NoDriveGrant as exc:
            return JSONResponse({"detail": exc.detail, "error": exc.error}, status_code=409, headers=NO_STORE)
        return JSONResponse(token, headers=NO_STORE)

    @app.delete("/api/me", status_code=204, response_class=Response)
    def delete_account(user: AppUser = Depends(app_user)):
        """Delete Account. Google first (so the bridge loses Drive access even if the store fails halfway; a retry
        finds the account again), then everything in the store, then the Drive client and the web app's Drive token
        cached in this instance."""
        try:
            refresh = accounts.google_refresh(user.sub)
        except CryptoError:  # sealed with a key that is no longer configured: nothing usable to revoke
            refresh = None
        if refresh and not google.revoke(refresh):
            log.warning("Google did not confirm revoking a deleted account's grant (unreachable or already revoked)")
        found = accounts.delete_user(user.sub)
        pool.forget(user.sub)
        drive_tokens.forget(user.sub)
        if not found:
            raise NotFound("no account to delete: it was already deleted")
        return Response(status_code=204)
