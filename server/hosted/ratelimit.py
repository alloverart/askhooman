"""Per-instance sliding-window rate limits for the auth endpoints, the resource server and the App API.

Each Cloud Run instance counts on its own, so the real limit is roughly N × instances: enough to stop
brute force and floods from one address, not a quota system. IPv6 clients are counted per /64 (one
customer's network), so rotating addresses inside it doesn't reset anything. The table is bounded: when full,
idle keys go first, then the least recently used ones (never everything at once).
"""

import ipaddress
import threading
import time
from collections import OrderedDict, deque

# bucket -> (requests, per seconds)
LIMITS = {
    "authorize": (30, 60.0),
    "consent": (30, 60.0),
    "token": (60, 60.0),
    "register": (10, 60.0),
    "revoke": (30, 60.0),
    "app": (120, 60.0),
    "drive_token": (10, 600.0),  # Google refreshes for the web app's Drive token, per user (app_api.DriveTokens)
    "agent": (300, 60.0),  # MCP/REST requests of one agent
    "user": (1200, 60.0),  # MCP/REST requests of all agents of one user
    "bearer_fail": (60, 60.0),  # failed bearer checks from one address
    "look_fetch": (10, 600.0),  # set_my_look image_url fetches of one agent (looks.own_look)
    "webhook_set": (10, 3600.0),  # set_my_webhook calls of one agent (self_webhook.py)
    "webhook_test": (10, 3600.0),  # test_my_webhook calls of one agent
    "media": (60, 3600.0),  # attachment calls of one agent: attach, upload request, detach, each item of a create
    "voice": (60, 60.0),  # GET /media/v1/<token> per user and agent: an agent fetching voice answers (voice_api.py)
    "files": (20, 60.0),  # GET /media/v1/<token> for a file, per user and agent (files_api; each can pin a slot)
}
FILE_BYTES = (1024 ** 3, 3600.0)  # bytes per seconds, per agent: the egress of file downloads (ByteBudget)
MAX_KEYS = 20000


class RateLimiter:
    def __init__(self, limits: dict[str, tuple[int, float]] | None = None, clock=time.monotonic,
                 max_keys: int = MAX_KEYS):
        self.limits = dict(LIMITS if limits is None else limits)
        self.clock = clock
        self.max_keys = max_keys
        self._hits: OrderedDict[tuple[str, str], deque] = OrderedDict()
        self._lock = threading.Lock()

    def _window(self, bucket: str, key: str, now: float) -> deque | None:
        hits = self._hits.get((bucket, key))
        if hits is not None:
            window = self.limits[bucket][1]
            while hits and now - hits[0] >= window:
                hits.popleft()
        return hits

    def allow(self, bucket: str, key: str, n: int = 1) -> bool:
        """Count `n` requests (one by default; an ask_create counts each of its attachments); False, counting none,
        if fewer than `n` are left in this key's window."""
        limit = self.limits.get(bucket)
        if limit is None:
            return True
        count, _ = limit
        now = self.clock()
        with self._lock:
            hits = self._window(bucket, key, now)
            if hits is None:
                if len(self._hits) >= self.max_keys:
                    self._evict(now)
                hits = self._hits[(bucket, key)] = deque()
            self._hits.move_to_end((bucket, key))
            if len(hits) + n > count:
                return False
            hits.extend([now] * n)
            return True

    def blocked(self, bucket: str, key: str) -> bool:
        """Would the next request of this key be refused? (Doesn't count one.)"""
        limit = self.limits.get(bucket)
        if limit is None:
            return False
        with self._lock:
            hits = self._window(bucket, key, self.clock())
            return hits is not None and len(hits) >= limit[0]

    def retry_after(self, bucket: str) -> int:
        return int(self.limits.get(bucket, (0, 60.0))[1])

    def _evict(self, now: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] >= self.limits[k[0]][1]]:
            del self._hits[key]
        while len(self._hits) >= self.max_keys:  # still full of live keys: drop the least recently used
            self._hits.popitem(last=False)


class ByteBudget:
    """The byte bucket `file_bytes`: at most `limit` bytes per `window` seconds per key (the agent), charged whole when
    a download starts. A refused charge records nothing. Per instance, like RateLimiter; bounded the same way."""

    def __init__(self, limit: int = FILE_BYTES[0], window: float = FILE_BYTES[1], clock=time.monotonic,
                 max_keys: int = MAX_KEYS):
        self.limit, self.window, self.clock, self.max_keys = limit, window, clock, max_keys
        self._charges: OrderedDict[str, deque] = OrderedDict()  # key -> deque of (time, bytes)
        self._lock = threading.Lock()

    def _expire(self, charges: deque, now: float) -> int:
        while charges and now - charges[0][0] >= self.window:
            charges.popleft()
        return sum(n for _, n in charges)

    def charge(self, key: str, n: int) -> bool:
        """Record `n` bytes for this key and return True, or return False, recording nothing, if they don't fit."""
        now = self.clock()
        with self._lock:
            charges = self._charges.get(key)
            if charges is None:
                if len(self._charges) >= self.max_keys:
                    for idle in [k for k, c in self._charges.items() if self._expire(c, now) == 0]:
                        del self._charges[idle]
                    while len(self._charges) >= self.max_keys:
                        self._charges.popitem(last=False)
                charges = self._charges[key] = deque()
            self._charges.move_to_end(key)
            if n < 0 or self._expire(charges, now) + n > self.limit:
                return False
            charges.append((now, n))
            return True

    def retry_after(self) -> int:
        return int(self.window)


def client_ip(headers, peer: str | None, trust_forwarded: bool) -> str:
    """The caller's address for rate limits. Behind Cloud Run's front end the last X-Forwarded-For entry is the
    address it saw (anything before it was sent by the client and can be forged). IPv6 is grouped by /64."""
    address = None
    if trust_forwarded:
        forwarded = headers.get("x-forwarded-for", "")
        address = forwarded.rsplit(",", 1)[-1].strip() or None
    address = address or peer or "unknown"
    try:
        ip = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is None:
        return str(ipaddress.IPv6Network(f"{ip}/64", strict=False))
    return str(ip.ipv4_mapped or ip) if isinstance(ip, ipaddress.IPv6Address) else str(ip)
