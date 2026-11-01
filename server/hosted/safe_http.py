"""Outbound HTTP to URLs that users or clients choose (Client ID Metadata Documents, agent webhooks), safe
against server-side request forgery and resource exhaustion (CIMD draft §6.5/§6.6, MCP "Client ID Metadata
Document Security").

- Only https URLs on port 443, no credentials in the URL, no IP literals that aren't public.
- The host name is resolved at connect time (with a bounded wait) and every address it resolves to must be
  public (no loopback, private, link-local — e.g. the metadata server 169.254.169.254 — CGNAT, multicast,
  reserved); the connection goes to a checked address, so DNS tricks (rebinding, internal names) can't reach
  internal services. Each checked address is tried in turn. TLS is still verified against the host name.
- A wall-clock deadline (`with deadline(seconds):`) bounds the whole exchange — connect, TLS, headers, body —
  at the socket layer, so a server that drips one byte at a time can't hold a thread longer than that.
- No compression: requests say `Accept-Encoding: identity`, a response with any Content-Encoding is refused,
  and bodies are read raw (iter_raw) up to a byte cap, so a small compressed payload can't expand in memory.
- No redirects, no proxies from the environment.
"""

import ipaddress
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpcore
import httpx

DNS_WAIT_MAX = 3.0  # seconds to wait for the resolver when no deadline is set
_local = threading.local()


class UnsafeURL(ValueError):
    pass


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check_url(url: str, what: str = "URL") -> str:
    """The host of an https URL on port 443 we may fetch; UnsafeURL otherwise. DNS is checked at connect time."""
    if not isinstance(url, str) or len(url) > 2000:
        raise UnsafeURL(f"{what} must be a URL of at most 2000 characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL(f"{what} is not a valid URL") from None
    if parts.scheme != "https":
        raise UnsafeURL(f"{what} must use https")
    host = parts.hostname
    if not host or parts.username is not None or parts.password is not None:
        raise UnsafeURL(f"{what} must have a host and no user name or password")
    if port not in (None, 443):
        raise UnsafeURL(f"{what} must use the standard https port 443")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
            raise UnsafeURL(f"{what} must have a public host name") from None
    else:
        if not is_public_address(host):
            raise UnsafeURL(f"{what} points to a non-public address")
    return host


# ---------- wall-clock deadline ----------

@contextmanager
def deadline(seconds: float):
    """Every socket operation of outbound requests made by this thread inside the block shares one deadline."""
    previous = getattr(_local, "deadline", None)
    _local.deadline = time.monotonic() + seconds
    try:
        yield
    finally:
        _local.deadline = previous


def _remaining(timeout: float | None, error=httpcore.ReadTimeout) -> float | None:
    end = getattr(_local, "deadline", None)
    if end is None:
        return timeout
    left = end - time.monotonic()
    if left <= 0:
        raise error("deadline exceeded")
    return left if timeout is None else min(timeout, left)


class DeadlineStream(httpcore.NetworkStream):
    """A network stream whose every read/write waits at most until the thread's deadline."""

    def __init__(self, stream: httpcore.NetworkStream):
        self._stream = stream

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, _remaining(timeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, _remaining(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return DeadlineStream(self._stream.start_tls(ssl_context, server_hostname,
                                                     _remaining(timeout, httpcore.ConnectTimeout)))

    def get_extra_info(self, info: str):
        return self._stream.get_extra_info(info)


def _resolve_bounded(resolve, host: str, port: int, wait: float):
    """getaddrinfo can't be interrupted; run it aside and stop waiting after `wait` seconds."""
    result: dict = {}

    def run():
        try:
            result["infos"] = resolve(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            result["error"] = exc

    worker = threading.Thread(target=run, name="dns", daemon=True)
    worker.start()
    worker.join(wait)
    if worker.is_alive():
        raise httpcore.ConnectTimeout(f"resolving {host} took too long")
    if "error" in result:
        raise httpcore.ConnectError(f"cannot resolve {host}: {result['error']}")
    return result["infos"]


class PublicOnlyBackend(httpcore.SyncBackend):
    """Resolves the host itself and connects only if every address is public; the address checked is the address
    connected to (no DNS rebinding window). `check` and `ports` are relaxed only by tests."""

    def __init__(self, resolve=socket.getaddrinfo, check=is_public_address, ports: tuple[int, ...] | None = (443,)):
        self.resolve = resolve
        self.check = check
        self.ports = ports

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if self.ports is not None and port not in self.ports:
            raise httpcore.ConnectError(f"port {port} is not allowed")
        wait = _remaining(timeout, httpcore.ConnectTimeout)
        infos = _resolve_bounded(self.resolve, host, port, wait if wait is not None else DNS_WAIT_MAX)
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses:
            raise httpcore.ConnectError(f"{host} has no address")
        if not all(self.check(a) for a in addresses):
            raise httpcore.ConnectError(f"{host} resolves to a non-public address")
        error: Exception = httpcore.ConnectError(f"cannot connect to {host}")
        for address in addresses:
            try:
                stream = super().connect_tcp(address, port, _remaining(timeout, httpcore.ConnectTimeout),
                                             local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
                continue
            return DeadlineStream(stream)
        raise error


class PublicOnlyTransport(httpx.HTTPTransport):
    def __init__(self, resolve=socket.getaddrinfo, check=is_public_address, ports: tuple[int, ...] | None = (443,)):
        super().__init__(verify=True, trust_env=False, retries=0)
        # httpx has no public hook for the network backend; replace its connection pool with one that uses ours.
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(), max_connections=20,
                                             network_backend=PublicOnlyBackend(resolve, check, ports))


def public_client(timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An HTTP client for user-chosen URLs. `transport` replaces the safe one in tests (no network there)."""
    return httpx.Client(transport=transport or PublicOnlyTransport(), timeout=timeout, follow_redirects=False,
                        trust_env=False, headers={"User-Agent": "Qtracker/0.4 (+https://qtracker.allover.art)",
                                                  "Accept-Encoding": "identity"})


def refuse_encoded(response: httpx.Response) -> None:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise ValueError(f"compressed responses are refused (Content-Encoding: {encoding[:40]})")


def raw_chunks(response: httpx.Response):
    """The body as sent, never decoded. A response already held in memory (mock transports build them so) is
    returned as it is."""
    if response.is_stream_consumed:
        yield response.content
    else:
        yield from response.iter_raw()


def read_capped(response: httpx.Response, limit: int) -> bytes:
    """The raw body of a streamed response, or ValueError if it is compressed or longer than `limit` bytes."""
    refuse_encoded(response)
    body = bytearray()
    for chunk in raw_chunks(response):
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError(f"response larger than {limit} bytes")
    return bytes(body)
