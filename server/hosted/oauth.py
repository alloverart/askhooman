"""Authorization server of the hosted bridge (SPEC v4; MCP Authorization 2026-07-28).

  GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728 (also named in every 401 from /mcp)
  GET  /.well-known/oauth-authorization-server       RFC 8414, with client_id_metadata_document_supported and
                                                     authorization_response_iss_parameter_supported
  GET  /authorize          client (DCR id or CIMD URL) + redirect URI check, PKCE S256, RFC 8707 resource
                           -> Google sign-in (Web client, PKCE, state, nonce; the state is bound to this browser
                           by a __Host- cookie)
  GET  /oauth/google/callback   -> stores the Google refresh token (encrypted) -> /oauth/consent
  GET  /oauth/consent      "<client> wants to ask you questions…", agent name, Allow / Cancel (CSRF token)
  POST /oauth/consent      -> code (+ state, + iss, RFC 9207) to the client's redirect URI
  POST /token              authorization_code (PKCE, redirect URI, resource) and refresh_token (rotation,
                           reuse revokes the family); access tokens live 1 h and are bound to <public>/mcp
  POST /register           RFC 7591 dynamic client registration (deprecated by MCP, still used by clients)
  POST /revoke             RFC 7009
  GET  /oauth/link         Google sign-in only: lets the bridge reach the user's Drive (for agent keys)

Redirect URIs of DCR and CIMD clients are chosen by whoever registered them, so the browser is sent there only
after the user acted on our consent page (Allow / Cancel). Earlier problems are shown as a page of ours; where the
client should still hear about it (the user cancelled at Google), the page offers a link back instead of
redirecting. Client metadata documents are fetched on a small limiter of their own, never on the worker threads
that serve /mcp and the API. No CORS headers anywhere. Secrets never reach a log line.
"""

import base64
import hashlib
import json
import logging
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import anyio
import anyio.to_thread
from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..rules import Invalid
from .accounts import SCOPE, Accounts, NameTaken, OAuthError, clean_agent_name
from .clients import (
    CimdResolver,
    ClientError,
    is_cimd_id,
    is_loopback_uri,
    pick_redirect,
    registration,
)
from .config import HostedSettings
from .crypto import digest, new_secret, same
from .google import DRIVE_SCOPE, GoogleError, GoogleOAuth, granted_scopes
from .pages import consent_page, error_page, linked_page
from .ratelimit import RateLimiter, client_ip

log = logging.getLogger("asks.hosted.oauth")

PKCE_VALUE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")  # RFC 7636 §4.1/§4.2
STATE_MAX = 1000
REGISTER_MAX_BYTES = 16 * 1024
OUTBOUND_FETCHES = 4  # client metadata documents fetched at once per instance
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
SLOW_DOWN = "Too many attempts. Wait a minute and try again."


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def canonical_resource(value: str) -> str | None:
    """RFC 8707 resource in canonical form (lower-case scheme/host, no default port, no trailing slash,
    no fragment), or None if it isn't an absolute http(s) URI."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except (ValueError, AttributeError):
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.fragment:
        return None
    netloc = parts.hostname.lower()
    scheme = parts.scheme.lower()
    if port and (scheme, port) not in (("https", 443), ("http", 80)):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))


def oauth_error(code: str, description: str, status: int = 400, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": description}, status_code=status,
                        headers={**NO_STORE, **(headers or {})})


def _host(uri: str) -> str:
    try:
        return urlsplit(uri).hostname or uri
    except ValueError:
        return uri


class AuthServer:
    def __init__(self, settings: HostedSettings, accounts: Accounts, google: GoogleOAuth, cimd: CimdResolver,
                 limiter: RateLimiter):
        self.settings = settings
        self.accounts = accounts
        self.google = google
        self.cimd = cimd
        self.limiter = limiter
        self.secure = settings.public_url.startswith("https://")
        self.outbound = anyio.CapacityLimiter(OUTBOUND_FETCHES)

    # ---------- plumbing ----------

    def routes(self) -> list[Route]:
        return [
            Route("/.well-known/oauth-protected-resource", self.protected_resource, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource/mcp", self.protected_resource, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", self.authorization_server, methods=["GET"]),
            Route("/authorize", self.authorize, methods=["GET"]),
            Route("/oauth/google/callback", self.google_callback, methods=["GET"]),
            Route("/oauth/consent", self.consent, methods=["GET", "POST"]),
            Route("/oauth/link", self.link, methods=["GET"]),
            Route("/token", self.token, methods=["POST"]),
            Route("/register", self.register, methods=["POST"]),
            Route("/revoke", self.revoke, methods=["POST"]),
        ]

    def _ip(self, request: Request) -> str:
        return client_ip(request.headers, request.client.host if request.client else None,
                         self.settings.trust_forwarded)

    def _limited(self, request: Request, bucket: str) -> bool:
        return not self.limiter.allow(bucket, self._ip(request))

    def _cookie_name(self, rid: str) -> str:
        """One cookie per pending authorization (two tabs can connect two agents at once)."""
        return ("__Host-" if self.secure else "") + "qt_auth_" + digest(rid)[:16]

    def _with_cookie(self, response: Response, rid: str) -> Response:
        response.set_cookie(self._cookie_name(rid), rid, max_age=self.settings.request_ttl, path="/",
                            secure=self.secure, httponly=True, samesite="lax")
        return response

    def _drop_cookie(self, response: Response, rid: str) -> Response:
        response.delete_cookie(self._cookie_name(rid), path="/", secure=self.secure, httponly=True, samesite="lax")
        return response

    def _pending(self, request: Request, rid: str | None, stage: str) -> dict | None:
        """The pending authorization `rid`, only in the browser that started it (cookie) and at `stage`."""
        if not rid:
            return None
        cookie = request.cookies.get(self._cookie_name(rid))
        if not cookie or not same(cookie, rid):
            return None
        pending = self.accounts.request(rid)
        return pending if pending is not None and pending.get("stage") == stage else None

    def _client_url(self, pending: dict, **params) -> str:
        """An authorization response URL for the client, always with its state and our issuer (RFC 9207)."""
        return construct_redirect_uri(pending["redirect_uri"], **params, state=pending.get("state"),
                                      iss=self.settings.issuer)

    def _to_client(self, pending: dict, **params) -> RedirectResponse:
        """Redirect to the client: only after the user answered our consent page."""
        return RedirectResponse(self._client_url(pending, **params), status_code=303,
                                headers={"Cache-Control": "no-store"})

    def _back_page(self, pending: dict, message: str, status: int = 400, **params) -> Response:
        """A problem before consent: our page, with a link (not a redirect) back to the client."""
        return error_page(message, status, back=(self._client_url(pending, **params), _host(pending["redirect_uri"])))

    async def client(self, client_id: str) -> dict:
        """The client: a registration from the store, or a metadata document. Fetching a document runs on the
        outbound limiter; if every slot is busy (slow hosts), fail at once instead of queueing."""
        if not is_cimd_id(client_id):
            client = await anyio.to_thread.run_sync(self.accounts.dcr_client, client_id)
            if client is None:
                raise ClientError("unknown client_id", "invalid_client")
            return client
        cached = self.cimd.cached(client_id)
        if cached is not None:
            return cached
        if self.outbound.available_tokens < 1:
            raise ClientError("the server is busy fetching client metadata, try again shortly",
                              "temporarily_unavailable")
        return await anyio.to_thread.run_sync(self.cimd.resolve, client_id, limiter=self.outbound)

    # ---------- metadata ----------

    async def protected_resource(self, request: Request) -> Response:
        doc = ProtectedResourceMetadata(resource=self.settings.resource, authorization_servers=[self.settings.issuer],
                                        scopes_supported=[SCOPE], bearer_methods_supported=["header"],
                                        resource_name="Qtracker")
        return JSONResponse(doc.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "max-age=300"})

    async def authorization_server(self, request: Request) -> Response:
        base = self.settings.issuer
        doc = OAuthMetadata(
            issuer=base,
            authorization_endpoint=f"{base}/authorize",
            token_endpoint=f"{base}/token",
            registration_endpoint=f"{base}/register",
            revocation_endpoint=f"{base}/revoke",
            scopes_supported=[SCOPE],
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            token_endpoint_auth_methods_supported=["none", "client_secret_post", "client_secret_basic"],
            revocation_endpoint_auth_methods_supported=["none", "client_secret_post", "client_secret_basic"],
            code_challenge_methods_supported=["S256"],
            client_id_metadata_document_supported=True,
            authorization_response_iss_parameter_supported=True,
        )
        return JSONResponse(doc.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "max-age=300"})

    # ---------- /authorize ----------

    async def authorize(self, request: Request) -> Response:
        if self._limited(request, "authorize"):
            return error_page(SLOW_DOWN, 429, "Slow down")
        params = dict(request.query_params)
        client_id = params.get("client_id") or ""
        if not client_id:
            return error_page("The app didn't say who it is (client_id is missing).")
        try:
            client = await self.client(client_id)
        except ClientError as exc:
            status = 503 if exc.code == "temporarily_unavailable" else 400
            return error_page(f"This app can't connect to Qtracker: {exc}.", status)
        return await anyio.to_thread.run_sync(self._authorize, params, client)

    def _authorize(self, params: dict, client: dict) -> Response:
        try:
            redirect_uri, explicit = pick_redirect(client, params.get("redirect_uri"))
        except ClientError as exc:
            return error_page(f"This app can't connect to Qtracker: {exc}.")
        # A malformed request is the app's bug: say so here, never bounce the browser to its redirect URI.
        state = params.get("state")
        problem = None
        if state is not None and len(state) > STATE_MAX:
            problem = "state is too long"
        elif params.get("response_type") != "code":
            problem = "response_type must be code"
        elif not PKCE_VALUE.match(params.get("code_challenge") or ""):
            problem = "a PKCE code_challenge is required"
        elif params.get("code_challenge_method") != "S256":
            problem = "code_challenge_method must be S256"
        elif not params.get("resource"):
            problem = f"the resource parameter is required (RFC 8707): {self.settings.resource}"
        elif canonical_resource(params["resource"]) != self.settings.resource:
            problem = f"unknown resource; this server issues tokens for {self.settings.resource}"
        if problem:
            return error_page(f"This app sent an invalid sign-in request: {problem}.")
        verifier, nonce = new_secret(nbytes=48), new_secret(nbytes=24)
        rid = self.accounts.start_request({
            "purpose": "agent", "stage": "google", "client_id": client["client_id"], "client_kind": client["kind"],
            "client_name": client["client_name"], "localhost": is_loopback_uri(redirect_uri),
            "redirect_uri": redirect_uri, "redirect_explicit": explicit, "state": state,
            "code_challenge": params["code_challenge"], "resource": self.settings.resource, "scope": SCOPE,
            "nonce": nonce, "google_verifier": verifier, "forced": False,
        })
        url = self.google.auth_url(rid, nonce, s256(verifier))
        return self._with_cookie(RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"}), rid)

    async def link(self, request: Request) -> Response:
        """Google sign-in only, so the bridge can reach this account's Drive (agent keys need it)."""
        if self._limited(request, "authorize"):
            return error_page(SLOW_DOWN, 429, "Slow down")

        def start() -> Response:
            verifier = new_secret(nbytes=48)
            nonce = new_secret(nbytes=24)
            rid = self.accounts.start_request({"purpose": "link", "stage": "google", "nonce": nonce,
                                               "google_verifier": verifier, "forced": True})
            url = self.google.auth_url(rid, nonce, s256(verifier), force_consent=True)
            return self._with_cookie(RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"}),
                                     rid)
        return await anyio.to_thread.run_sync(start)

    # ---------- Google's answer ----------

    async def google_callback(self, request: Request) -> Response:
        if self._limited(request, "consent"):
            return error_page(SLOW_DOWN, 429, "Slow down")
        return await anyio.to_thread.run_sync(self._google_callback, request, dict(request.query_params))

    def _google_callback(self, request: Request, params: dict) -> Response:
        rid = params.get("state") or ""
        pending = self._pending(request, rid, "google")
        if pending is None:
            return error_page("This sign-in has expired, was already used, or started in another browser.")
        agent_flow = pending["purpose"] == "agent"

        def fail(message: str, error: str, status: int = 400) -> Response:
            self.accounts.finish_request(rid)
            page = self._back_page(pending, message, status, error=error, error_description=message) if agent_flow \
                else error_page(message, status)
            return self._drop_cookie(page, rid)

        if params.get("error") or not params.get("code"):
            return fail("Google sign-in was cancelled.", "access_denied")
        try:
            tokens = self.google.exchange(params["code"], pending["google_verifier"])
            claims = self.google.verify_id_token(tokens["id_token"], [self.settings.web_client_id],
                                                 nonce=pending["nonce"])
        except GoogleError as exc:
            log.warning("Google sign-in failed: %s", exc)
            return fail(exc.public, "server_error", 502)
        sub, email = claims["sub"], claims.get("email")
        refresh = tokens.get("refresh_token")
        has_scope = DRIVE_SCOPE in granted_scopes(tokens)
        if not has_scope or (not refresh and not self.accounts.drive_linked(sub)):
            if not pending.get("forced"):
                # Only Google's consent screen issues a refresh token (and shows the Drive box again): ask once more.
                verifier, nonce = new_secret(nbytes=48), new_secret(nbytes=24)
                self.accounts.update_request(rid, {"forced": True, "google_verifier": verifier, "nonce": nonce})
                url = self.google.auth_url(rid, nonce, s256(verifier), login_hint=email, force_consent=True)
                return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
            return fail("Qtracker needs access to its own data in your Google Drive (the box on Google's screen).",
                        "access_denied")
        self.accounts.touch_user(sub, email)
        if refresh and has_scope:
            self.accounts.link_google(sub, email, refresh)
        if not agent_flow:
            self.accounts.finish_request(rid)
            return self._drop_cookie(linked_page(email), rid)
        self.accounts.update_request(rid, {"stage": "consent", "user": sub, "email": email,
                                           "csrf": new_secret(nbytes=24)})
        return RedirectResponse(f"/oauth/consent?r={digest(rid)[:16]}", status_code=303,
                                headers={"Cache-Control": "no-store"})

    # ---------- consent ----------

    def _consent_rid(self, request: Request) -> str | None:
        r = request.query_params.get("r") or ""
        if not re.fullmatch(r"[0-9a-f]{16}", r):
            return None
        rid = request.cookies.get(("__Host-" if self.secure else "") + "qt_auth_" + r)
        return rid if rid and digest(rid)[:16] == r else None

    async def consent(self, request: Request) -> Response:
        if self._limited(request, "consent"):
            return error_page(SLOW_DOWN, 429, "Slow down")
        rid = self._consent_rid(request)
        form = dict(await request.form()) if request.method == "POST" else None
        return await anyio.to_thread.run_sync(self._consent, request, rid, form)

    def _suggested_name(self, pending: dict, agents: list[dict]) -> str:
        """The name to prefill: the agent this client connected as before, only for a client that one install
        registered for itself (DCR). A metadata-document client (CIMD) is the same client_id on every machine: every
        Claude Code is one client, so the agent it connected as last says nothing about who connects now (connecting
        a second agent used to prefill the first one's name). Otherwise empty: the field shows a placeholder and the user types."""
        if pending.get("client_kind") != "dcr":
            return ""
        for agent in agents:
            if pending["client_id"] in self.accounts.client_ids(agent):
                return agent["name"]
        return ""

    def _consent_page(self, rid: str, pending: dict, agent_name: str | None = None, error: str | None = None,
                      collision: dict | None = None) -> Response:
        agents = self.accounts.agents(pending["user"])
        cimd = pending.get("client_kind") == "cimd"
        return consent_page(
            action_url=f"/oauth/consent?r={digest(rid)[:16]}",
            client_name=pending["client_name"], client_label=pending["client_name"],
            verified_by=_host(pending["client_id"]) if cimd else None, redirect_host=_host(pending["redirect_uri"]),
            localhost=bool(pending.get("localhost")), email=pending.get("email"),
            agent_name=agent_name if agent_name is not None else self._suggested_name(pending, agents),
            existing=[a["name"] for a in agents], csrf=pending["csrf"], error=error,
            collision=collision and {"name": collision["name"], "slug": collision["slug"],
                                     "clients": [c.get("client_name") or "an agent key"
                                                 for c in self.accounts.client_list(collision)]
                                                or ["an agent key" if collision.get("key_prefix") else "another app"]})

    def _consent(self, request: Request, rid: str | None, form: dict | None) -> Response:
        pending = self._pending(request, rid, "consent") if rid else None
        if pending is None:
            return error_page("This connection request has expired or was already answered.")
        if form is None:
            return self._consent_page(rid, pending)
        if not same(str(form.get("csrf", "")), pending["csrf"]):
            return error_page("This form has expired. Start connecting again from your app.")
        action = form.get("action")
        if action not in ("allow", "cancel"):
            return error_page("Unknown action.")
        name = None
        if action == "allow":
            try:
                name = clean_agent_name(form.get("agent_name"))
            except Invalid as exc:
                return self._consent_page(rid, pending, str(form.get("agent_name", ""))[:40], error=str(exc))
            confirm = str(form.get("confirm") or "")
            try:
                agent = self.accounts.connect_agent(pending["user"], name, pending["client_id"],
                                                    pending["client_name"], confirm=confirm or None, dry_run=True,
                                                    shared_client=pending["client_kind"] == "cimd")
            except NameTaken as taken:
                return self._consent_page(rid, pending, name, collision=taken.agent)
        pending = self.accounts.finish_request(rid)  # single use: a second POST finds nothing
        if pending is None:
            return error_page("This connection request has expired or was already answered.")
        if action == "cancel":
            return self._drop_cookie(self._to_client(pending, error="access_denied",
                                                     error_description="the user declined"), rid)
        try:
            agent = self.accounts.connect_agent(pending["user"], name, pending["client_id"], pending["client_name"],
                                                confirm=confirm or None, shared_client=pending["client_kind"] == "cimd")
        except NameTaken:  # someone took the name between the check and now
            return self._drop_cookie(error_page("That agent name was just taken. Start connecting again."), rid)
        code = self.accounts.issue_code({
            "user": pending["user"], "agent_id": agent["id"], "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"], "redirect_explicit": pending["redirect_explicit"],
            "code_challenge": pending["code_challenge"], "resource": pending["resource"], "scope": pending["scope"],
        })
        if pending["client_kind"] == "dcr":
            self.accounts.client_used(pending["client_id"])
        log.info("agent %s authorized for client %s", agent["id"],
                 _host(pending["client_id"]) if pending["client_kind"] == "cimd" else pending["client_id"])
        return self._drop_cookie(self._to_client(pending, code=code), rid)

    # ---------- client authentication (token, revoke) ----------

    @staticmethod
    def _basic(request: Request) -> tuple[str, str] | None:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic" or not value:
            return None
        try:
            user, sep, password = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            raise OAuthError("invalid_client", "malformed Basic authorization", 401) from None
        if not sep:
            raise OAuthError("invalid_client", "malformed Basic authorization", 401)
        return unquote(user), unquote(password)  # RFC 6749 §2.3.1: form-urlencoded before Base64

    async def _authenticate_client(self, request: Request, form: dict) -> dict:
        basic = self._basic(request)
        client_id = form.get("client_id") or ""
        secret = form.get("client_secret")
        if basic:
            if client_id and client_id != basic[0]:
                raise OAuthError("invalid_request", "client_id differs from the Basic authorization")
            client_id, secret = basic
        if not client_id:
            raise OAuthError("invalid_client", "client authentication failed", 401)
        try:
            client = await self.client(client_id)
        except ClientError as exc:
            if exc.code == "temporarily_unavailable":
                raise OAuthError("temporarily_unavailable", str(exc), 503) from None
            raise OAuthError("invalid_client", "client authentication failed", 401) from None
        if not self.accounts.client_secret_ok(client, secret):
            raise OAuthError("invalid_client", "client authentication failed", 401)
        return client

    async def _form(self, request: Request) -> dict:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise OAuthError("invalid_request", "send the request as application/x-www-form-urlencoded")
        form = await request.form()
        return {key: value for key, value in form.items() if isinstance(value, str)}

    # ---------- /token ----------

    async def token(self, request: Request) -> Response:
        if self._limited(request, "token"):
            return oauth_error("slow_down", "too many requests", 429, {"Retry-After": "60"})
        try:
            form = await self._form(request)
            client = await self._authenticate_client(request, form)
            tokens = await anyio.to_thread.run_sync(self._token, client, form)
        except OAuthError as exc:
            headers = {"WWW-Authenticate": 'Basic realm="qtracker"'} if exc.status == 401 and \
                request.headers.get("authorization") else None
            return oauth_error(exc.code, exc.description, exc.status, headers)
        return JSONResponse(tokens, headers=NO_STORE)

    def _token(self, client: dict, form: dict) -> dict:
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return self._code_grant(client, form)
        if grant_type == "refresh_token":
            return self._refresh_grant(client, form)
        raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")

    def _check_resource(self, form: dict, grant: dict) -> None:
        resource = form.get("resource")
        if resource is not None and canonical_resource(resource) != grant["resource"]:
            raise OAuthError("invalid_target", f"tokens of this grant are for {grant['resource']} only")

    def _code_grant(self, client: dict, form: dict) -> dict:
        code, verifier = form.get("code"), form.get("code_verifier") or ""
        if not code:
            raise OAuthError("invalid_request", "code is required")
        if not PKCE_VALUE.match(verifier):
            raise OAuthError("invalid_request", "a valid PKCE code_verifier is required")
        grant = self.accounts.redeem_code(code, client["client_id"])
        given = form.get("redirect_uri")
        if (grant["redirect_explicit"] or given is not None) and given != grant["redirect_uri"]:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if not same(s256(verifier), grant["code_challenge"]):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        self._check_resource(form, grant)
        return self.accounts.issue_tokens(grant, grant["family"])

    def _refresh_grant(self, client: dict, form: dict) -> dict:
        token = form.get("refresh_token")
        if not token:
            raise OAuthError("invalid_request", "refresh_token is required")
        grant = self.accounts.use_refresh(token, client["client_id"])
        self._check_resource(form, grant)
        requested = set((form.get("scope") or "").split())
        if requested - set(grant["scope"].split()):
            raise OAuthError("invalid_scope", "a refresh can't widen the granted scope")
        tokens = self.accounts.issue_tokens(grant, grant["family"])
        if client["kind"] == "dcr":
            self.accounts.client_used(client["client_id"])
        return tokens

    # ---------- /register ----------

    async def register(self, request: Request) -> Response:
        if self._limited(request, "register"):
            return oauth_error("slow_down", "too many registrations", 429, {"Retry-After": "60"})
        body = await request.body()
        if len(body) > REGISTER_MAX_BYTES:
            return oauth_error("invalid_client_metadata", "registration request too large")
        try:
            data = json.loads(body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return oauth_error("invalid_client_metadata", "the registration request must be JSON")
        try:
            client = registration(data)
        except ClientError as exc:
            return oauth_error(exc.code, str(exc))
        record, secret = await anyio.to_thread.run_sync(self.accounts.register_client, client)
        reply = {
            "client_id": record["client_id"], "client_id_issued_at": record["client_id_issued_at"],
            "client_name": record["client_name"], "redirect_uris": record["redirect_uris"],
            "grant_types": record["grant_types"], "response_types": ["code"],
            "token_endpoint_auth_method": record["token_endpoint_auth_method"],
        }
        for field in ("client_uri", "software_id", "software_version", "application_type"):
            if field in record:
                reply[field] = record[field]
        if secret:
            reply.update(client_secret=secret, client_secret_expires_at=0)
        log.info("registered OAuth client %s (%s)", record["client_id"], record["token_endpoint_auth_method"])
        return JSONResponse(reply, status_code=201, headers=NO_STORE)

    # ---------- /revoke ----------

    async def revoke(self, request: Request) -> Response:
        if self._limited(request, "revoke"):
            return oauth_error("slow_down", "too many requests", 429, {"Retry-After": "60"})
        try:
            form = await self._form(request)
            client = await self._authenticate_client(request, form)
            token = form.get("token")
            if not token:
                raise OAuthError("invalid_request", "token is required")
            await anyio.to_thread.run_sync(self.accounts.revoke, token, client["client_id"])
        except OAuthError as exc:
            return oauth_error(exc.code, exc.description, exc.status)
        return Response(status_code=200, headers=NO_STORE)
