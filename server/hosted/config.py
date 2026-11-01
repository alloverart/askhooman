"""Settings of the hosted bridge, read from environment variables (Cloud Run: plain env + Secret Manager).

ASKS_PUBLIC_URL             public origin (default https://askhooman.com): the OAuth issuer; MCP at <it>/mcp
ASKS_PUBLIC_URL_ALIASES     more origins of the same bridge, comma-separated, e.g. the old domain after a rename.
                            A request that arrives on one gets that origin's OAuth metadata, issuer and token
                            audience, so agents connected there keep working; new connections get the public URL
ASKS_GOOGLE_WEB_CLIENT      the Google "Web application" OAuth client: JSON {"client_id", "client_secret"} (flat or
                            {"web": {...}} as the console downloads it), given inline or as a path to such a file
ASKS_GOOGLE_APP_CLIENT_IDS  comma-separated client ids the app's Google ID tokens are issued to (the iOS client)
ASKS_ENCRYPTION_KEY         key for Google refresh tokens and webhook secrets at rest: base64 of 32 random bytes,
                            or "kid:base64,kid2:base64" (the first encrypts, all decrypt: for rotation); inline or path
ASKS_STORE                  "firestore" (default) or "memory" (one process, data lost on restart: development only)
ASKS_FIRESTORE_PROJECT      Google Cloud project of the Firestore database (default: the runtime's project)
ASKS_FIRESTORE_DATABASE     Firestore database id (default "(default)")
ASKS_TRUST_FORWARDED        "1" (default): the client IP for rate limits is the last X-Forwarded-For entry
                            (Cloud Run's front end appends it); "0": the TCP peer
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ACCESS_TTL = 3600  # seconds an access token lives (SPEC: 1 h)
REFRESH_TTL = 60 * 86400  # a refresh token not used for this long expires; every use rotates it
CODE_TTL = 120  # authorization codes
REQUEST_TTL = 600  # a pending authorization (Google sign-in + consent) must finish within this
DEFAULT_PUBLIC_URL = "https://askhooman.com"


class ConfigError(RuntimeError):
    pass


def _inline_or_file(value: str, name: str) -> str:
    """Secret Manager secrets reach Cloud Run as an env value or as a mounted file: accept both."""
    value = value.strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    path = Path(value).expanduser()
    if not value.startswith(("/", "~", "./")) or not path.is_file():  # base64 may start with "/" too
        return value
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read {name} file {value}: {exc.strerror}") from None


def parse_web_client(text: str) -> tuple[str, str]:
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("ASKS_GOOGLE_WEB_CLIENT is not valid JSON") from None
    if isinstance(data, dict):
        data = data.get("web") or data
    client_id = data.get("client_id") if isinstance(data, dict) else None
    secret = data.get("client_secret") if isinstance(data, dict) else None
    if not isinstance(client_id, str) or not client_id or not isinstance(secret, str) or not secret:
        raise ConfigError("ASKS_GOOGLE_WEB_CLIENT has no client_id/client_secret")
    return client_id, secret


def public_url(value: str, name: str = "ASKS_PUBLIC_URL") -> str:
    """The canonical origin (lower-case scheme and host, no default port, no trailing slash): it is the OAuth
    issuer and the base of the token audience, which are compared as exact strings."""
    value = (value or "").strip().rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ConfigError(f"{name} is not a valid URL") from None
    scheme, host = parts.scheme.lower(), (parts.hostname or "")
    local = host in ("localhost", "127.0.0.1")
    if scheme not in ("https", "http") or (scheme == "http" and not local) or not host:
        raise ConfigError(f"{name} must be an https origin, e.g. https://askhooman.example.com")
    if parts.path or parts.query or parts.fragment or parts.username:
        raise ConfigError(f"{name} must be an origin only (no path, query or fragment)")
    default = {"https": 443, "http": 80}[scheme]
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


@dataclass(frozen=True)
class HostedSettings:
    public_url: str
    web_client_id: str
    web_client_secret: str
    app_client_ids: tuple[str, ...]
    encryption_keys: str  # parsed by crypto.Keyring
    store: str = "firestore"
    firestore_project: str | None = None
    firestore_database: str = "(default)"
    trust_forwarded: bool = True
    access_ttl: int = ACCESS_TTL
    refresh_ttl: int = REFRESH_TTL
    code_ttl: int = CODE_TTL
    request_ttl: int = REQUEST_TTL
    aliases: tuple[str, ...] = ()

    @property
    def issuer(self) -> str:
        return self.public_url

    @property
    def resource(self) -> str:
        """The MCP endpoint's canonical URI: the audience every access token is bound to (RFC 8707)."""
        return self.resource_for(self.public_url)

    @property
    def google_redirect_uri(self) -> str:
        return self.google_redirect_for(self.public_url)

    def origin(self, host: str | None) -> str:
        """The origin a request arrived on, by its Host header: one of the aliases, otherwise the public URL."""
        if host and self.aliases:
            try:
                candidate = public_url(f"{self.public_url.split(':', 1)[0]}://{host.strip()}")
            except ConfigError:
                return self.public_url
            if candidate in self.aliases:
                return candidate
        return self.public_url

    @staticmethod
    def resource_for(origin: str) -> str:
        return f"{origin}/mcp"

    @staticmethod
    def google_redirect_for(origin: str) -> str:
        """Google returns to the origin the sign-in started on (its cookie lives there); every origin's callback
        must be an authorized redirect URI of the Web client."""
        return f"{origin}/oauth/google/callback"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "HostedSettings":
        env = os.environ if env is None else env
        client_id, secret = parse_web_client(_inline_or_file(env.get("ASKS_GOOGLE_WEB_CLIENT", ""),
                                                             "ASKS_GOOGLE_WEB_CLIENT"))
        app_ids = tuple(x.strip() for x in env.get("ASKS_GOOGLE_APP_CLIENT_IDS", "").split(",") if x.strip())
        if not app_ids:
            raise ConfigError("ASKS_GOOGLE_APP_CLIENT_IDS is not set (the iOS OAuth client id)")
        store = env.get("ASKS_STORE", "firestore").strip().lower() or "firestore"
        public = public_url(env.get("ASKS_PUBLIC_URL", "").strip() or DEFAULT_PUBLIC_URL)
        aliases = tuple(dict.fromkeys(
            public_url(a, "ASKS_PUBLIC_URL_ALIASES") for a in env.get("ASKS_PUBLIC_URL_ALIASES", "").split(",")
            if a.strip() and public_url(a, "ASKS_PUBLIC_URL_ALIASES") != public))
        if store not in ("firestore", "memory"):
            raise ConfigError("ASKS_STORE must be 'firestore' or 'memory'")
        return cls(
            public_url=public,
            web_client_id=client_id,
            web_client_secret=secret,
            app_client_ids=app_ids,
            encryption_keys=_inline_or_file(env.get("ASKS_ENCRYPTION_KEY", ""), "ASKS_ENCRYPTION_KEY"),
            store=store,
            firestore_project=env.get("ASKS_FIRESTORE_PROJECT", "").strip() or None,
            firestore_database=env.get("ASKS_FIRESTORE_DATABASE", "").strip() or "(default)",
            trust_forwarded=env.get("ASKS_TRUST_FORWARDED", "1").strip().lower() not in ("0", "false", "no", "off"),
            aliases=aliases,
        )
