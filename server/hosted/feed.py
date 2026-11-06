"""The agent's event feed (SPEC v5 "Waking the agent"). Most agents run where nothing can reach them (a Claude Code
session behind NAT, Codex on a desktop), so instead of only calling webhooks the bridge keeps each agent's events and
the agent's side fetches them over a connection it opens itself: the REST long poll GET /api/agent/events, MCP
ask_wait, the Claude Code channel plugin and `asks.py listen` all read this feed.

  feeds/{agent id}   {seq, events: [{id, type, ask_id | task_id, event: {id, at}, at, t}], mcp_cursor}

Events are slim (type and ids): an event only wakes the agent, what happened is read with asks_mine / tasks_mine.
`id` is the agent's sequence number and the cursor is its string. The feed keeps the last FEED_MAX events and none
older than FEED_DAYS; a cursor older than what is kept (or not one of ours) answers `missed: true`, so the agent
rereads its asks and tasks. Delivery is at least once.

Waiting: Cloud Run runs several instances, and the user's event may land on another instance than the agent's long
poll. A waiter wakes at once for events published by its own instance and re-reads the store every POLL_EVERY
seconds for the others'. Waiting never holds a thread: the store is read in a worker thread for a moment, the wait
itself is an asyncio event.
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

import anyio.to_thread
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..mcp_tools import IDENTITY_SCOPE_KEY
from .store import UNCHANGED, Store

FEED_MAX = 200
FEED_DAYS = 7
WAIT_MAX = 50  # seconds; well under Cloud Run's request timeout
POLL_EVERY = 3.0  # seconds between store re-reads while waiting (events from other instances)
COLLECTION = "feeds"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _public(event: dict) -> dict:
    return {k: v for k, v in event.items() if k != "t"}


class EventFeed:
    def __init__(self, store: Store, clock=time.time, poll_every: float = POLL_EVERY):
        self.store = store
        self.clock = clock
        self.poll_every = poll_every
        self._waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._lock = threading.Lock()

    def _alive(self, events: list, now: float) -> list[dict]:
        oldest = now - FEED_DAYS * 86400
        return [e for e in events if isinstance(e, dict) and e.get("t", now) >= oldest]

    # ---------- writing ----------

    def publish(self, agent_id: str, event: dict) -> dict:
        """Append one event to the agent's feed (atomically) and wake its waiters here. Returns the event as read."""
        now = self.clock()

        def change(doc):
            doc = dict(doc or {})
            seq = int(doc.get("seq") or 0) + 1
            stored = {"id": seq, **event, "at": _iso(now), "t": now}
            events = self._alive(doc.get("events") or [], now) + [stored]
            doc.update(seq=seq, events=events[-FEED_MAX:])
            return doc, stored
        stored = self.store.transact(COLLECTION, agent_id, change)
        self._wake(agent_id)
        return _public(stored)

    def forget(self, agent_id: str) -> None:
        """The agent was revoked or its user deleted the account: its feed goes too."""
        self.store.delete(COLLECTION, agent_id)

    def mcp_cursor(self, agent_id: str) -> str | None:
        """ask_wait's cursor, kept by the bridge (MCP clients keep no state between calls)."""
        return (self.store.get(COLLECTION, agent_id) or {}).get("mcp_cursor")

    def set_mcp_cursor(self, agent_id: str, cursor: str) -> None:
        def change(doc):
            if doc is not None and doc.get("mcp_cursor") == cursor:
                return UNCHANGED, None
            return {**(doc or {"seq": 0, "events": []}), "mcp_cursor": cursor}, None
        self.store.transact(COLLECTION, agent_id, change)

    # ---------- reading ----------

    def read(self, agent_id: str, after: str | None) -> dict:
        """{events, cursor}: the events after `after`. No cursor = start now (no events, the latest cursor). A cursor
        that is not ours or older than what is kept: {events: [], cursor: <latest>, missed: true}."""
        doc = self.store.get(COLLECTION, agent_id) or {}
        seq = int(doc.get("seq") or 0)
        cursor = str(seq)
        if after is None:
            return {"events": [], "cursor": cursor}
        events = self._alive(doc.get("events") or [], self.clock())
        first = events[0]["id"] if events else seq + 1
        n = int(after) if isinstance(after, str) and after.isdigit() else -1
        if n < 0 or n > seq or n < first - 1:
            return {"events": [], "cursor": cursor, "missed": True}
        return {"events": [_public(e) for e in events if e["id"] > n], "cursor": cursor}

    async def wait(self, agent_id: str, after: str | None, wait: float) -> dict:
        """read(), but with nothing new it waits up to `wait` seconds (≤ WAIT_MAX) for the next event."""
        wait = max(0.0, min(float(wait), WAIT_MAX))
        signal = asyncio.Event()
        entry = (asyncio.get_running_loop(), signal)
        with self._lock:  # registered before the first read, so an event in between is not slept through
            self._waiters.setdefault(agent_id, set()).add(entry)
        try:
            page = await anyio.to_thread.run_sync(self.read, agent_id, after)
            deadline = time.monotonic() + wait
            while not page["events"] and not page.get("missed"):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    await asyncio.wait_for(signal.wait(), timeout=min(self.poll_every, left))
                except asyncio.TimeoutError:
                    pass
                signal.clear()
                page = await anyio.to_thread.run_sync(self.read, agent_id, page["cursor"])
            return page
        finally:
            with self._lock:
                waiters = self._waiters.get(agent_id)
                if waiters is not None:
                    waiters.discard(entry)
                    if not waiters:
                        del self._waiters[agent_id]

    def _wake(self, agent_id: str) -> None:
        with self._lock:
            waiters = list(self._waiters.get(agent_id, ()))
        for loop, signal in waiters:
            try:
                loop.call_soon_threadsafe(signal.set)
            except RuntimeError:  # that loop is closed: its waiter is gone
                pass



def add_feed_route(app: FastAPI, feed: EventFeed, accounts) -> None:
    """GET /api/agent/events?after=<cursor>&wait=<seconds> with the agent's token (OAuth access token or agent key)
    -> {events, cursor} (+ missed). A long poll: it answers at once when there are events after `after`, else it
    holds the request up to `wait` seconds (≤ WAIT_MAX). Only the calling agent's own events."""

    @app.get("/api/agent/events")
    async def agent_events(request: Request, after: str | None = None,
                           wait: float = Query(0, ge=0, le=WAIT_MAX)):
        who = request.scope.get(IDENTITY_SCOPE_KEY)  # set by RequestGuard from the agent's bearer token
        agent_id = getattr(who, "agent_id", None)
        if not agent_id:
            raise HTTPException(401, "call this with your agent token", headers={"WWW-Authenticate": "Bearer"})
        await anyio.to_thread.run_sync(accounts.listened, agent_id)
        page = await feed.wait(agent_id, after, wait)
        return JSONResponse(page, headers={"Cache-Control": "no-store"})
