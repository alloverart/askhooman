"""Agent webhooks (SPEC v4 "Wake-up"): after the user answers, closes or reopens an ask in the app, the app calls
POST /api/events and the bridge POSTs to the asking agent's webhook URL.

Request: JSON {type: "ask.answered"|"ask.closed"|"ask.reopened", ask, answer?, resolution?, event, delivery_id,
sent_at}, headers AskHooman-Event, AskHooman-Delivery and
    AskHooman-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
with the agent's webhook secret (shown once in the app). Receivers should check the signature in constant time
and reject old timestamps (e.g. > 5 minutes) to stop replays. The same three also go out under the names from
before the rename (Qtracker-Event, Qtracker-Delivery, Qtracker-Signature), for receivers written then.

Delivery: a few worker threads take due deliveries from one bounded schedule; a receiver host has at most one
delivery in flight, so a slow or broken receiver can only delay its own deliveries. An attempt has a wall-clock
deadline; the reply body is never decompressed and at most a few KB of it are read. A network error, 5xx or
429 is retried after 5 s, 30 s and 2 min (429: at least its Retry-After, capped at 5 min) by rescheduling,
never by sleeping in a worker. Other answers are final. When the schedule is full, new deliveries are dropped
(and logged). URLs go through the SSRF-safe client; logs show only the URL's host (URLs can carry secrets).
"""

import hashlib
import hmac
import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .safe_http import UnsafeURL, check_url, deadline, raw_chunks

log = logging.getLogger("asks.hosted.webhooks")

RETRY_DELAYS = (5.0, 30.0, 120.0)
RETRY_AFTER_MAX = 300.0
ATTEMPT_DEADLINE = 10.0  # seconds, wall clock, per attempt
REPLY_READ_MAX = 4096  # bytes of the receiver's reply that are read (and thrown away)
QUEUE_MAX = 1000
WORKERS = 4
SIGNATURE_HEADER = "AskHooman-Signature"
LEGACY_SIGNATURE_HEADER = "Qtracker-Signature"  # the product was called Qtracker until 01.11.2026


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, body: bytes, header: str, now: float | None = None, tolerance: float = 300.0) -> bool:
    """What a receiver does (also used by the tests)."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        return False
    expected = sign(secret, body, timestamp).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def check_webhook_url(url: str) -> str:
    try:
        check_url(url, "webhook_url")
    except UnsafeURL as exc:
        raise ValueError(str(exc)) from None
    return url


@dataclass
class Delivery:
    url: str
    secret: str
    event_type: str
    delivery_id: str
    body: bytes
    host: str
    attempt: int = 1
    due: float = 0.0
    seq: int = field(default=0)
    agent_id: str | None = None


class WebhookSender:
    def __init__(self, http: httpx.Client, retry_delays: tuple[float, ...] = RETRY_DELAYS, clock=time.time,
                 workers: int = WORKERS, queue_max: int = QUEUE_MAX, attempt_deadline: float = ATTEMPT_DEADLINE):
        self.http = http
        self.retry_delays = tuple(retry_delays)
        self.clock = clock
        self.workers = workers
        self.queue_max = queue_max
        self.attempt_deadline = attempt_deadline
        self._pending: list[Delivery] = []
        self._busy_hosts: set[str] = set()
        self._in_flight = 0
        self._cond = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._seq = itertools.count()
        # Called once per delivery with its final outcome: (agent_id, ok, status, error). The hosted app writes it
        # to the agent as last_delivery (SPEC v5), so the app can show "Last ping ok" / "Last ping failed: 404".
        self.on_result: Callable[[str | None, bool, int | None, str | None], None] | None = None

    # ---------- queueing ----------

    def send(self, url: str, secret: str, event_type: str, delivery_id: str, payload: dict,
             agent_id: str | None = None) -> bool:
        """Schedule one delivery (returns at once); False if the schedule is full and it was dropped."""
        item = self._item(url, secret, event_type, delivery_id, payload, agent_id)
        with self._cond:
            if len(self._pending) >= self.queue_max:
                log.error("webhook %s to %s dropped: %d deliveries already waiting", delivery_id, item.host,
                          len(self._pending))
                return False
            self._pending.append(item)
            self._start_workers()
            self._cond.notify()
        return True

    def _item(self, url: str, secret: str, event_type: str, delivery_id: str, payload: dict,
              agent_id: str | None = None) -> Delivery:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return Delivery(url, secret, event_type, delivery_id, body, urlsplit(url).hostname or "?",
                        due=time.monotonic(), seq=next(self._seq), agent_id=agent_id)

    def try_once(self, url: str, secret: str, event_type: str, delivery_id: str, payload: dict) -> dict:
        """One attempt now, in this thread, no retries (test_my_webhook): {ok, status, error}."""
        status, problem, _ = self._post(self._item(url, secret, event_type, delivery_id, payload))
        return {"ok": problem is None, "status": status, "error": problem}

    def _report(self, item: Delivery, ok: bool, status: int | None, error: str | None) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(item.agent_id, ok, status, error)
        except Exception:
            log.exception("recording the outcome of webhook %s failed", item.delivery_id)

    def _start_workers(self) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self.workers:
            thread = threading.Thread(target=self._worker, name=f"asks-webhooks-{len(self._threads)}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _take(self) -> Delivery:
        """The earliest due delivery whose host has nothing in flight (waits for one)."""
        with self._cond:
            while True:
                now = time.monotonic()
                ready = [d for d in self._pending if d.host not in self._busy_hosts]
                if ready:
                    first = min(ready, key=lambda d: (d.due, d.seq))
                    if first.due <= now:
                        self._pending.remove(first)
                        self._busy_hosts.add(first.host)
                        self._in_flight += 1
                        return first
                    self._cond.wait(first.due - now)
                else:
                    self._cond.wait(1.0)

    def _done(self, item: Delivery, retry_in: float | None) -> None:
        with self._cond:
            self._busy_hosts.discard(item.host)
            self._in_flight -= 1
            if retry_in is not None:
                item.attempt += 1
                item.due = time.monotonic() + retry_in
                self._pending.append(item)
            self._cond.notify_all()

    def _worker(self) -> None:
        while True:
            item = self._take()
            retry_in = None
            try:
                retry_in = self._attempt(item)
            except Exception:
                log.exception("webhook delivery crashed")
            finally:
                self._done(item, retry_in)

    # ---------- one attempt ----------

    def _post(self, item: Delivery) -> tuple[int | None, str | None, float | None]:
        """POST once: (HTTP status or None, problem or None when delivered, Retry-After seconds or None)."""
        timestamp = int(self.clock())
        signature = sign(item.secret, item.body, timestamp)
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                   "AskHooman-Event": item.event_type, "AskHooman-Delivery": item.delivery_id,
                   SIGNATURE_HEADER: signature,
                   "Qtracker-Event": item.event_type, "Qtracker-Delivery": item.delivery_id,
                   LEGACY_SIGNATURE_HEADER: signature}
        retry_after = None
        try:
            with deadline(self.attempt_deadline), \
                    self.http.stream("POST", item.url, content=item.body, headers=headers, timeout=5.0) as response:
                status = response.status_code
                read = 0
                for chunk in raw_chunks(response):  # never decoded; a few KB at most, then dropped
                    read += len(chunk)
                    if read >= REPLY_READ_MAX:
                        break
                try:
                    retry_after = float(response.headers.get("retry-after", ""))
                except ValueError:
                    retry_after = None
        except httpx.HTTPError as exc:
            return None, type(exc).__name__, None
        return status, None if 200 <= status < 300 else f"HTTP {status}", retry_after

    def _attempt(self, item: Delivery) -> float | None:
        """POST once; the delay before the next attempt, or None when finished (delivered or given up)."""
        attempts = len(self.retry_delays) + 1
        status, problem, retry_after = self._post(item)
        if problem is None:
            log.info("webhook %s delivered to %s (attempt %d)", item.delivery_id, item.host, item.attempt)
            self._report(item, True, status, None)
            return None
        final = status is not None and status != 429 and status < 500
        if final or item.attempt >= attempts:
            log.error("webhook %s to %s failed (attempt %d of %d, giving up): %s",
                      item.delivery_id, item.host, item.attempt, attempts, problem)
            self._report(item, False, status, problem)
            return None
        delay = self.retry_delays[item.attempt - 1]
        if retry_after and retry_after > 0:
            delay = max(delay, min(retry_after, RETRY_AFTER_MAX))
        log.warning("webhook %s to %s failed (attempt %d of %d), retrying in %.0f s: %s",
                    item.delivery_id, item.host, item.attempt, attempts, delay, problem)
        return delay

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until nothing is waiting or in flight (tests, shutdown). True if that happened in time."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._in_flight:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
        return True
