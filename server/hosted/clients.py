"""OAuth clients of the hosted authorization server: redirect URI rules, dynamic registration (RFC 7591,
deprecated by MCP 2026-07-28 but still what most clients use) and Client ID Metadata Documents (CIMD,
draft-ietf-oauth-client-id-metadata-document-00, MCP's preferred registration).

A client is a dict: client_id, client_name, redirect_uris, token_endpoint_auth_method ("none",
"client_secret_post" or "client_secret_basic"), grant_types, kind ("cimd" or "dcr"), secret_hash (DCR with a
secret only). Redirect URIs: https, or http on a loopback host (any port at request time, RFC 8252 §7.3);
otherwise exact string match. No custom schemes, no fragments.
"""

import json
import logging
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit

import httpx

from ..rules import CONTROL_CHARS
from .safe_http import UnsafeURL, check_url, deadline, read_capped

log = logging.getLogger("asks.hosted.clients")

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")  # as urlsplit().hostname gives them
REDIRECTS_MAX = 10
CLIENT_NAME_MAX = 100
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
GRANT_TYPES = ("authorization_code", "refresh_token")
CIMD_MAX_BYTES = 5 * 1024  # the draft's recommended cap
CIMD_TIMEOUT = 3.0  # per socket operation
CIMD_DEADLINE = 5.0  # the whole fetch, wall clock
CIMD_FAILURE_TTL = 60.0  # a failed fetch is answered from memory for this long
DCR_ID = re.compile(r"^qtc_[A-Za-z0-9_-]{22}$")
CIMD_TTL_MIN, CIMD_TTL_DEFAULT, CIMD_TTL_MAX = 300.0, 3600.0, 86400.0
CIMD_CACHE_MAX = 1000


class ClientError(Exception):
    """An invalid client or registration. `code` is the OAuth error code to return."""

    def __init__(self, message: str, code: str = "invalid_client_metadata"):
        super().__init__(message)
        self.code = code


# ---------- redirect URIs ----------

def _loopback(parts) -> bool:
    try:
        return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS
    except ValueError:
        return False


def check_redirect_uri(uri) -> str:
    if not isinstance(uri, str) or not uri or len(uri) > 2000 or CONTROL_CHARS.search(uri) or " " in uri:
        raise ClientError("redirect_uris must be URL strings", "invalid_redirect_uri")
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018  (raises on a bad port)
    except ValueError:
        raise ClientError(f"invalid redirect URI {uri!r}", "invalid_redirect_uri") from None
    if parts.fragment or "#" in uri:
        raise ClientError("redirect URIs must not contain a fragment", "invalid_redirect_uri")
    if not parts.hostname or parts.username is not None:
        raise ClientError(f"redirect URI {uri!r} needs a host and no user info", "invalid_redirect_uri")
    if parts.scheme == "https" or _loopback(parts):
        return uri
    raise ClientError(f"redirect URI {uri!r} must use https (or http on 127.0.0.1, [::1] or localhost)",
                      "invalid_redirect_uri")


def redirect_matches(registered: str, given: str) -> bool:
    """Exact match; for loopback http URIs the port may differ (native apps pick a free port each time)."""
    if registered == given:
        return True
    a, b = urlsplit(registered), urlsplit(given)
    if not (_loopback(a) and _loopback(b)):
        return False
    try:
        b.port  # noqa: B018
    except ValueError:
        return False
    return ((a.scheme, a.hostname, a.path, a.query) == (b.scheme, b.hostname, b.path, b.query)
            and not b.fragment and b.username is None)


def pick_redirect(client: dict, given: str | None) -> tuple[str, bool]:
    """The redirect URI to use and whether the client sent it; ClientError if it isn't registered."""
    uris = client.get("redirect_uris") or []
    if given is None or given == "":
        if len(uris) == 1:
            return uris[0], False
        raise ClientError("redirect_uri is required (the client has several)", "invalid_request")
    if any(redirect_matches(uri, given) for uri in uris):
        return given, True
    raise ClientError("redirect_uri is not registered for this client", "invalid_request")


def is_loopback_uri(uri: str) -> bool:
    """The redirect URI returns to a loopback address (an app on the user's own computer)."""
    try:
        return _loopback(urlsplit(uri))
    except ValueError:
        return False


# ---------- shared metadata checks ----------

def clean_label(value: str) -> str:
    """A single-line display name: control and format characters (incl. bidi overrides and zero-width
    characters, which can make a name look like another) removed, line/paragraph separators as spaces."""
    text = "".join(" " if unicodedata.category(ch) in ("Zl", "Zp") or ch in "\n\t" else ch for ch in value)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    return " ".join(text.split())


def clean_name(value, fallback: str) -> str:
    name = clean_label(value if isinstance(value, str) else "")
    return name[:CLIENT_NAME_MAX].strip() or fallback


def _metadata(data: dict) -> dict:
    """Checks shared by registration requests and metadata documents; returns the normalised parts."""
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        raise ClientError("redirect_uris must be a non-empty list", "invalid_redirect_uri")
    if len(uris) > REDIRECTS_MAX:
        raise ClientError(f"at most {REDIRECTS_MAX} redirect_uris", "invalid_redirect_uri")
    uris = [check_redirect_uri(u) for u in uris]
    grants = data.get("grant_types", ["authorization_code"])
    if not isinstance(grants, list) or "authorization_code" not in grants:
        raise ClientError("grant_types must include authorization_code")
    responses = data.get("response_types", ["code"])
    if not isinstance(responses, list) or "code" not in responses:
        raise ClientError("response_types must include code")
    return {"redirect_uris": uris, "grant_types": [g for g in grants if g in GRANT_TYPES]}


# ---------- dynamic client registration ----------

def registration(data) -> dict:
    """Validate an RFC 7591 registration request; the client record without id/secret."""
    if not isinstance(data, dict):
        raise ClientError("the registration request must be a JSON object")
    meta = _metadata(data)
    method = data.get("token_endpoint_auth_method") or "client_secret_basic"  # RFC 7591 §2 default
    if method not in AUTH_METHODS:
        raise ClientError(f"token_endpoint_auth_method must be one of: {', '.join(AUTH_METHODS)}")
    client = {
        **meta,
        "kind": "dcr",
        "client_name": clean_name(data.get("client_name"), "Unnamed app"),
        "token_endpoint_auth_method": method,
    }
    for field in ("client_uri", "software_id", "software_version"):
        value = data.get(field)
        if isinstance(value, str) and value and len(value) <= 500 and not CONTROL_CHARS.search(value):
            client[field] = value
    if data.get("application_type") in ("web", "native"):
        client["application_type"] = data["application_type"]
    return client


# ---------- client ID metadata documents ----------

def is_cimd_id(client_id: str) -> bool:
    return client_id.startswith("https://")


def check_cimd_id(client_id: str) -> None:
    """URL rules of the draft (§3): https, a path, no fragment/user info/dot segments; plus our SSRF rules."""
    try:
        check_url(client_id, "client_id")
    except UnsafeURL as exc:
        raise ClientError(str(exc), "invalid_client") from None
    parts = urlsplit(client_id)
    segments = parts.path.split("/")
    if not parts.path or parts.path == "/" or "." in segments or ".." in segments:
        raise ClientError("client_id URL must have a path without . or .. segments", "invalid_client")
    if parts.fragment or "#" in client_id:
        raise ClientError("client_id URL must not have a fragment", "invalid_client")


def parse_cimd(client_id: str, body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ClientError("client metadata document is not JSON", "invalid_client") from None
    if not isinstance(data, dict):
        raise ClientError("client metadata document is not a JSON object", "invalid_client")
    if data.get("client_id") != client_id:  # simple string comparison (draft §4.1)
        raise ClientError("client metadata document's client_id does not match its URL", "invalid_client")
    if not isinstance(data.get("client_name"), str) or not data["client_name"].strip():
        raise ClientError("client metadata document has no client_name", "invalid_client")
    if "client_secret" in data or "client_secret_expires_at" in data:
        raise ClientError("client metadata documents must not contain a client secret", "invalid_client")
    method = data.get("token_endpoint_auth_method", "none")
    if method != "none":  # shared secrets are forbidden; private_key_jwt is not supported by this server
        raise ClientError(f"token_endpoint_auth_method {method!r} is not supported (use none)", "invalid_client")
    try:
        meta = _metadata(data)
    except ClientError as exc:
        raise ClientError(f"client metadata document: {exc}", "invalid_client") from None
    client = {**meta, "kind": "cimd", "client_id": client_id,
              "client_name": clean_name(data.get("client_name"), urlsplit(client_id).hostname or "app"),
              "token_endpoint_auth_method": "none"}
    if isinstance(data.get("client_uri"), str):
        client["client_uri"] = data["client_uri"][:500]
    return client


class CimdResolver:
    """Fetches, validates and caches client metadata documents, per instance: valid documents respecting
    Cache-Control within [5 min, 24 h]; failures for 60 s (so a slow or broken host isn't fetched again for every
    request), as an answer only — a failure is never treated as a document."""

    def __init__(self, http: httpx.Client, clock=time.monotonic):
        self.http = http
        self.clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}
        self._failures: dict[str, tuple[float, str, str]] = {}
        self._lock = threading.Lock()

    def cached(self, client_id: str) -> dict | None:
        """The cached client, None on a miss; raises the cached failure. No network."""
        check_cimd_id(client_id)
        now = self.clock()
        with self._lock:
            hit = self._cache.get(client_id)
            if hit and hit[0] > now:
                return dict(hit[1])
            failed = self._failures.get(client_id)
            if failed and failed[0] > now:
                raise ClientError(failed[1], failed[2])
        return None

    def resolve(self, client_id: str) -> dict:
        hit = self.cached(client_id)
        if hit is not None:
            return hit
        now = self.clock()
        try:
            with deadline(CIMD_DEADLINE):
                client, ttl = self._fetch(client_id)
        except ClientError as exc:
            with self._lock:
                if len(self._failures) >= CIMD_CACHE_MAX:
                    self._failures.pop(next(iter(self._failures)))
                self._failures[client_id] = (now + CIMD_FAILURE_TTL, str(exc), exc.code)
            raise
        if ttl > 0:
            with self._lock:
                if len(self._cache) >= CIMD_CACHE_MAX:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[client_id] = (now + ttl, client)
        return dict(client)

    def _fetch(self, client_id: str) -> tuple[dict, float]:
        try:
            with self.http.stream("GET", client_id, headers={"Accept": "application/json",
                                                             "Accept-Encoding": "identity"},
                                  timeout=CIMD_TIMEOUT) as response:
                if response.status_code != 200:
                    raise ClientError(f"client metadata document: HTTP {response.status_code}", "invalid_client")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type != "application/json" and not (content_type.startswith("application/")
                                                                and content_type.endswith("+json")):
                    raise ClientError("client metadata document is not served as JSON", "invalid_client")
                body = read_capped(response, CIMD_MAX_BYTES)
                cache_control = response.headers.get("cache-control", "")
        except ValueError as exc:
            raise ClientError(f"client metadata document: {exc}", "invalid_client") from None
        except httpx.HTTPError as exc:
            log.info("CIMD fetch of %s failed: %s", urlsplit(client_id).hostname, type(exc).__name__)
            raise ClientError("client metadata document could not be fetched", "invalid_client") from None
        client = parse_cimd(client_id, body)
        return client, _ttl(cache_control)


def _ttl(cache_control: str) -> float:
    directives = {}
    for part in cache_control.lower().split(","):
        name, _, value = part.strip().partition("=")
        directives[name] = value.strip().strip('"')
    if "no-store" in directives:
        return 0.0
    try:
        ttl = float(directives["max-age"])
    except (KeyError, ValueError):
        ttl = CIMD_TTL_DEFAULT
    return min(max(ttl, CIMD_TTL_MIN), CIMD_TTL_MAX)
