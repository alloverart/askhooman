"""Google as the upstream identity provider of the hosted bridge.

- The bridge's sign-in leg (SPEC v4 "Login = Google as the upstream IdP"): the project's Web application client,
  authorization code + PKCE + state + nonce, scopes openid email drive.appdata, access_type=offline.
- Google ID tokens: signature (RS256, Google's published keys, cached per Cache-Control), iss, aud, exp, iat;
  used both for the sign-in leg (aud = the Web client) and for the App API (aud = the iOS client).
No token or secret appears in an error message or log line.
"""

import threading
import time
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWK

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
SCOPES = ("openid", "email", DRIVE_SCOPE)
LEEWAY = 60  # seconds of clock skew tolerated on exp/iat
CERTS_TTL_DEFAULT = 3600.0
CERTS_REFETCH_MIN = 60.0  # an unknown key id refetches the keys at most this often


class GoogleError(Exception):
    """A refused or failed call to Google, or an invalid ID token. `public` is safe to show to the user."""

    def __init__(self, message: str, public: str = "Google sign-in failed"):
        super().__init__(message)
        self.public = public


def _max_age(cache_control: str) -> float | None:
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        if name.lower() == "max-age" and value.strip().isdigit():
            return float(value)
    return None


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, http: httpx.Client,
                 clock=time.time):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http
        self.clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._keys_expire = 0.0
        self._keys_fetched = 0.0
        self._lock = threading.Lock()

    # ---------- sign-in leg ----------

    def auth_url(self, state: str, nonce: str, code_challenge: str, login_hint: str | None = None,
                 force_consent: bool = False) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # consent forces a new refresh token (Google issues one only on consent); otherwise let the user
            # pick the account, which is the one whose Drive the agent will use.
            "prompt": "consent" if force_consent else "select_account",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange(self, code: str, code_verifier: str) -> dict:
        """The token response for a sign-in code: {id_token, access_token, refresh_token?, scope, ...}."""
        data = {"code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier}
        try:
            response = self.http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise GoogleError(f"Google token endpoint unreachable: {type(exc).__name__}",
                              "Google could not be reached; try again") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("id_token"):
            error = str(body.get("error", "")) if isinstance(body, dict) else ""
            raise GoogleError(f"Google refused the sign-in code (HTTP {response.status_code} {error})")
        return body

    # ---------- ID tokens ----------

    def _fetch_keys(self) -> None:
        try:
            response = self.http.get(CERTS_URL)
            response.raise_for_status()
            keys = {k["kid"]: PyJWK(k) for k in response.json()["keys"] if k.get("kid") and k.get("kty") == "RSA"}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
            raise GoogleError(f"cannot load Google's signing keys: {type(exc).__name__}",
                              "Google could not be reached; try again") from None
        now = time.monotonic()
        self._keys = keys
        self._keys_fetched = now
        self._keys_expire = now + (_max_age(response.headers.get("cache-control", "")) or CERTS_TTL_DEFAULT)

    def _key(self, kid: str) -> PyJWK:
        with self._lock:
            now = time.monotonic()
            if now >= self._keys_expire or (kid not in self._keys and now - self._keys_fetched >= CERTS_REFETCH_MIN):
                self._fetch_keys()
            key = self._keys.get(kid)
        if key is None:
            raise GoogleError(f"ID token signed with an unknown key {kid!r}", "invalid Google ID token")
        return key

    def verify_id_token(self, token: str, audiences, nonce: str | None = None) -> dict:
        """The claims of a valid Google ID token issued to one of `audiences`; GoogleError otherwise."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise GoogleError("not a JWT", "invalid Google ID token") from None
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise GoogleError("ID token is not RS256 with a key id", "invalid Google ID token")
        key = self._key(header["kid"])
        try:
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=list(audiences), leeway=LEEWAY,
                                options={"require": ["iss", "aud", "exp", "iat", "sub"]})
        except jwt.ExpiredSignatureError:
            raise GoogleError("ID token expired", "Google ID token expired") from None
        except jwt.PyJWTError as exc:
            raise GoogleError(f"ID token rejected: {type(exc).__name__}", "invalid Google ID token") from None
        if claims.get("iss") not in ISSUERS:
            raise GoogleError("ID token not issued by Google", "invalid Google ID token")
        if claims.get("iat", 0) > self.clock() + LEEWAY:
            raise GoogleError("ID token issued in the future", "invalid Google ID token")
        if nonce is not None and claims.get("nonce") != nonce:
            raise GoogleError("ID token nonce mismatch", "Google sign-in could not be verified; start again")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise GoogleError("ID token without sub", "invalid Google ID token")
        return claims


def granted_scopes(token_response: dict) -> set[str]:
    return set(str(token_response.get("scope", "")).split())

