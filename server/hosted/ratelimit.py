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
    "agent": (300, 60.0),  # MCP/REST requests of one agent
    "user": (1200, 60.0),  # MCP/REST requests of all agents of one user
    "bearer_fail": (60, 60.0),  # failed bearer checks from one address
}
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

    def allow(self, bucket: str, key: str) -> bool:
        """Count one request; False if this key already used up the bucket's window."""
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
            if len(hits) >= count:
                return False
            hits.append(now)
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
