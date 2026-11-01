"""Fake Google for the hosted bridge: the OAuth token endpoint (sign-in codes and refreshes for the Web client),
token revocation, the signing keys (JWKS), ID tokens, and one FakeDrive per Google account — all behind httpx.MockTransport.
`public_transport()` serves client metadata documents and records webhook calls (the bridge's "internet").

sign_in() plays the user in the browser at accounts.google.com: it reads the bridge's authorization URL and
returns the callback path Google would redirect to.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from .fake_drive import FakeDrive

WEB_CLIENT_ID = "922-web.apps.googleusercontent.com"
WEB_CLIENT_SECRET = "GOCSPX-web-secret-SECRET"
IOS_CLIENT_ID = "922-ios.apps.googleusercontent.com"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.appdata"


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class FakeGoogle:
    def __init__(self, redirect_uri: str, *more_redirect_uris: str):
        self.redirect_uri = redirect_uri
        self.redirect_uris = {redirect_uri, *more_redirect_uris}  # the Web client's authorized redirect URIs
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "fake-kid-1"
        self.drives: dict[str, FakeDrive] = {}
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> sub
        self.access_tokens: dict[str, str] = {}  # access token -> sub
        self.revoked_refresh: set[str] = set()
        self.revocations: list[str] = []  # every token the bridge asked Google to revoke
        self.revoke_status: int | None = None  # set to make the revocation endpoint fail (e.g. 503)
        self.auth_requests: list[dict] = []  # params of every authorization URL the bridge sent a user to
        self.lock = threading.RLock()
        # the public internet seen by the bridge (CIMD documents, webhook receivers)
        self.documents: dict[str, tuple[int, dict, bytes]] = {}
        self.webhook_calls: list[httpx.Request] = []
        self.webhook_statuses: deque[int] = deque()

    # ---------- accounts ----------

    def drive(self, sub: str) -> FakeDrive:
        with self.lock:
            return self.drives.setdefault(sub, FakeDrive())

    def id_token(self, sub: str, email: str, aud: str, nonce: str | None = None, expires_in: int = 3600,
                 iss: str = "https://accounts.google.com", key=None, kid: str | None = None) -> str:
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": True,
                  "iat": now, "exp": now + expires_in}
        if nonce is not None:
            claims["nonce"] = nonce
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid or self.kid})

    def sign_in(self, auth_url: str, sub: str, email: str, grant_drive: bool = True,
                give_refresh: bool | None = None, deny: bool = False) -> str:
        """The user signs in at Google: returns the bridge's callback path (with code and state)."""
        parts = urlsplit(auth_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.auth_requests.append(params)
        assert params["client_id"] == WEB_CLIENT_ID and params["redirect_uri"] in self.redirect_uris
        assert params["response_type"] == "code" and params["code_challenge_method"] == "S256"
        assert params["access_type"] == "offline" and params["state"] and params["nonce"]
        assert set(params["scope"].split()) == {"openid", "email", DRIVE_SCOPE}
        callback = urlsplit(params["redirect_uri"]).path
        if deny:
            return f"{callback}?{urlencode({'error': 'access_denied', 'state': params['state']})}"
        if give_refresh is None:  # Google issues a refresh token on the consent screen (first time or forced)
            give_refresh = params.get("prompt") == "consent" or not self.drive_linked_before(sub)
        code = "4/0A" + secrets.token_urlsafe(24)
        with self.lock:
            self.codes[code] = {"sub": sub, "email": email, "challenge": params["code_challenge"],
                                "nonce": params["nonce"], "drive": grant_drive, "refresh": give_refresh,
                                "redirect_uri": params["redirect_uri"]}
        return f"{callback}?{urlencode({'code': code, 'state': params['state'], 'scope': params['scope']})}"

    def drive_linked_before(self, sub: str) -> bool:
        return sub in self.refresh_tokens.values()

    # ---------- HTTP ----------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            return self._token(request)
        if url.host == "oauth2.googleapis.com" and url.path == "/revoke" and request.method == "POST":
            return self._revoke(request)
        if url.host == "www.googleapis.com" and url.path == "/oauth2/v3/certs":
            jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
            jwk.update(kid=self.kid, alg="RS256", use="sig")
            return httpx.Response(200, json={"keys": [jwk]}, headers={"Cache-Control": "public, max-age=19000"})
        if url.host == "www.googleapis.com":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            with self.lock:
                sub = self.access_tokens.get(token)
            if sub is None:
                return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
            return self.drive(sub).handle(request)
        return httpx.Response(404, json={"error": f"fake google: no {url}"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != WEB_CLIENT_ID or form.get("client_secret") != WEB_CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        with self.lock:
            if form.get("grant_type") == "authorization_code":
                grant = self.codes.pop(form.get("code", ""), None)
                if grant is None or form.get("redirect_uri") != grant["redirect_uri"] \
                        or s256(form.get("code_verifier", "")) != grant["challenge"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                sub = grant["sub"]
                body = {"access_token": self._access(sub), "expires_in": 3599, "token_type": "Bearer",
                        "scope": "openid https://www.googleapis.com/auth/userinfo.email"
                                 + (f" {DRIVE_SCOPE}" if grant["drive"] else ""),
                        "id_token": self.id_token(sub, grant["email"], WEB_CLIENT_ID, grant["nonce"])}
                if grant["refresh"]:
                    refresh = "1//" + secrets.token_urlsafe(24)
                    self.refresh_tokens[refresh] = sub
                    body["refresh_token"] = refresh
                return httpx.Response(200, json=body)
            if form.get("grant_type") == "refresh_token":
                refresh = form.get("refresh_token", "")
                sub = self.refresh_tokens.get(refresh)
                if sub is None or refresh in self.revoked_refresh:
                    return httpx.Response(400, json={"error": "invalid_grant",
                                                     "error_description": "Token has been expired or revoked."})
                return httpx.Response(200, json={"access_token": self._access(sub), "expires_in": 3599,
                                                 "token_type": "Bearer", "scope": DRIVE_SCOPE})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        """Like Google: 200 for a live refresh token (the grant ends: it refreshes no more), 400 otherwise."""
        token = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}.get("token", "")
        with self.lock:
            self.revocations.append(token)
            if self.revoke_status is not None:
                return httpx.Response(self.revoke_status, json={"error": "backend_error"})
            if token in self.refresh_tokens and token not in self.revoked_refresh:
                self.revoked_refresh.add(token)
                return httpx.Response(200, json={})
        return httpx.Response(400, json={"error": "invalid_token", "error_description": "Token expired or revoked"})

    def _access(self, sub: str) -> str:
        token = "ya29." + secrets.token_urlsafe(16)
        self.access_tokens[token] = sub
        self.drive(sub).valid_tokens.add(token)
        return token

    # ---------- the public internet (CIMD documents, webhooks) ----------

    def publish(self, url: str, document: dict | bytes, status: int = 200, headers: dict | None = None) -> None:
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        self.documents[url] = (status, {"Content-Type": "application/json", **(headers or {})}, body)

    def public_transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and str(request.url) in self.documents:
                status, headers, body = self.documents[str(request.url)]
                return httpx.Response(status, headers=headers, content=body)
            if request.method == "POST":
                with self.lock:
                    self.webhook_calls.append(request)
                    status = self.webhook_statuses.popleft() if self.webhook_statuses else 200
                return httpx.Response(status, json={})
            return httpx.Response(404)
        return httpx.MockTransport(handle)
