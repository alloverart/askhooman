"""The agent's event feed (SPEC v5 step 2, "Waking the agent"): a bounded per-agent queue in the hosted store,
read with a cursor; a read can wait for the next event. Cloud Run runs several instances, so a waiter must also
see events another instance wrote (it re-reads the store while it waits)."""

import threading
import time

import anyio
import pytest

from server.hosted.feed import FEED_MAX, EventFeed
from server.hosted.store import MemoryStore

AGENT = "ag_one"
OTHER = "ag_two"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def ask_event(n: int) -> dict:
    return {"type": "ask.answered", "ask_id": n, "event": {"id": 2, "at": "2026-11-06T10:00:00Z"}}


def test_publish_assigns_increasing_ids_and_read_after_cursor():
    feed = EventFeed(MemoryStore())
    stored = [feed.publish(AGENT, ask_event(n)) for n in (7, 8, 9)]
    assert [e["id"] for e in stored] == [1, 2, 3]
    assert stored[0]["type"] == "ask.answered" and stored[0]["ask_id"] == 7 and stored[0]["at"]
    page = feed.read(AGENT, "1")
    assert [e["ask_id"] for e in page["events"]] == [8, 9] and page["cursor"] == "3"
    assert "missed" not in page
    assert feed.read(AGENT, "3") == {"events": [], "cursor": "3"}
    assert feed.read(OTHER, None) == {"events": [], "cursor": "0"}  # an agent sees only its own feed


def test_no_cursor_starts_now():
    feed = EventFeed(MemoryStore())
    for n in range(3):
        feed.publish(AGENT, ask_event(n))
    assert feed.read(AGENT, None) == {"events": [], "cursor": "3"}


def test_events_carry_no_internal_fields():
    feed = EventFeed(MemoryStore())
    feed.publish(AGENT, ask_event(1))
    assert set(feed.read(AGENT, "0")["events"][0]) == {"id", "type", "ask_id", "event", "at"}


def test_trim_to_200_and_7_days_and_missed():
    now = [1_800_000_000.0]
    feed = EventFeed(MemoryStore(), clock=lambda: now[0])
    for n in range(FEED_MAX + 5):
        feed.publish(AGENT, ask_event(n))
    kept = feed.read(AGENT, "5")["events"]
    assert [e["id"] for e in kept][:1] == [6] and len(kept) == FEED_MAX and kept[-1]["id"] == 205
    assert feed.read(AGENT, "2") == {"events": [], "cursor": "205", "missed": True}
    now[0] += 8 * 86400
    feed.publish(AGENT, ask_event(999))
    assert [e["ask_id"] for e in feed.read(AGENT, "205")["events"]] == [999]
    assert feed.read(AGENT, "204")["missed"] is True  # 205 was dropped by age
    for bad in ("999", "x", "-1", ""):
        assert feed.read(AGENT, bad) == {"events": [], "cursor": "206", "missed": True}


def test_concurrent_publishes_lose_nothing():
    feed = EventFeed(MemoryStore())

    def burst():
        for n in range(25):
            feed.publish(AGENT, ask_event(n))
    threads = [threading.Thread(target=burst) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [e["id"] for e in feed.read(AGENT, "0")["events"]]
    assert ids == list(range(1, 201))


@pytest.mark.anyio
async def test_wait_returns_on_local_publish():
    feed = EventFeed(MemoryStore(), poll_every=30)  # only the in-process signal can wake it in time
    feed.publish(AGENT, ask_event(1))
    timer = threading.Timer(0.2, lambda: feed.publish(AGENT, ask_event(2)))
    start = time.monotonic()
    timer.start()
    page = await feed.wait(AGENT, "1", 5)
    assert time.monotonic() - start < 1.0
    assert [e["ask_id"] for e in page["events"]] == [2] and page["cursor"] == "2"


@pytest.mark.anyio
async def test_wait_sees_another_instance():
    store = MemoryStore()
    writer, reader = EventFeed(store), EventFeed(store, poll_every=0.05)
    threading.Timer(0.2, lambda: writer.publish(AGENT, ask_event(5))).start()
    start = time.monotonic()
    page = await reader.wait(AGENT, None, 5)
    assert time.monotonic() - start < 1.0
    assert [e["ask_id"] for e in page["events"]] == [5]


@pytest.mark.anyio
async def test_wait_times_out_empty_and_does_not_wait_for_zero():
    feed = EventFeed(MemoryStore(), poll_every=0.05)
    start = time.monotonic()
    assert await feed.wait(AGENT, None, 0.3) == {"events": [], "cursor": "0"}
    assert 0.25 <= time.monotonic() - start < 1.0
    start = time.monotonic()
    assert await feed.wait(AGENT, None, 0) == {"events": [], "cursor": "0"}
    assert time.monotonic() - start < 0.1


@pytest.mark.anyio
async def test_waiting_does_not_block_other_tasks():
    feed = EventFeed(MemoryStore(), poll_every=0.05)
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(time.monotonic())
            await anyio.sleep(0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(feed.wait, AGENT, None, 0.3)
        tg.start_soon(ticker)
    assert len(ticks) == 5 and ticks[-1] - ticks[0] < 0.25


def test_mcp_cursor_and_forget():
    store = MemoryStore()
    feed = EventFeed(store)
    assert feed.mcp_cursor(AGENT) is None
    feed.publish(AGENT, ask_event(1))
    feed.set_mcp_cursor(AGENT, "1")
    assert feed.mcp_cursor(AGENT) == "1"
    feed.publish(AGENT, ask_event(2))
    assert feed.mcp_cursor(AGENT) == "1"  # publishing keeps the MCP cursor
    feed.forget(AGENT)
    assert store.get("feeds", AGENT) is None and feed.mcp_cursor(AGENT) is None
    feed.forget(AGENT)  # nothing left: no error
