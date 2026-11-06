"""Regression tests for the security review of server/hosted/ (31.10): each test is one finding, written as the
attack and asserting that it no longer works (ported from the reviewer's exploit tests)."""

import gzip
import http.server
import json
import socket
import socketserver
import threading
import time
import tracemalloc

import anyio.to_thread
import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from server.hosted import clients as clients_mod
from server.hosted import records as records_mod
from server.hosted.accounts import Accounts
from server.hosted.app import create_hosted_app
from server.hosted.clients import CimdResolver, ClientError
from server.hosted.config import public_url
from server.hosted.oauth import canonical_resource
from server.hosted.ratelimit import RateLimiter, client_ip
from server.hosted.records import HostedAsks
from server.hosted.safe_http import PublicOnlyBackend, PublicOnlyTransport, deadline, public_client, read_capped
from server.hosted.store import MemoryStore
from server.hosted.webhooks import WebhookSender
from server.mirror import ASK, app_properties, build_doc

from .fake_google import FakeGoogle
from .hosted_support import (
    DENIS_SUB,
    LOOPBACK,
    PUBLIC,
    authorize,
    connect,
    csrf_of,
    form_action,
    id_token,
    mcp,
    pkce,
    query,
    register,
    settings_for,
    to_consent,
    tool_error,
    tool_result,
)

ATTACKER_CIMD = "https://attacker.example/c.json"


def gzip_response(payload: bytes, encoding: str = "gzip") -> httpx.MockTransport:
    """A streamed (not pre-buffered) compressed response, as a real server would send it."""
    return httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"Content-Type": "application/json", "Content-Encoding": encoding},
        stream=httpx.ByteStream(payload)))


# ---------- H1: decompression bombs ----------

@pytest.mark.parametrize("encoding, payload", [
    ("gzip", gzip.compress(b"\0" * (64 << 20), 9)),
    ("gzip, gzip", gzip.compress(gzip.compress(b"\0" * (32 << 20), 9), 9)),
])
def test_compressed_responses_are_refused_without_decoding(encoding, payload):
    client = httpx.Client(transport=gzip_response(payload, encoding))
    tracemalloc.start()
    with client.stream("GET", ATTACKER_CIMD) as response:
        with pytest.raises(ValueError, match="compressed"):
            read_capped(response, 5 * 1024)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 5e6  # nothing was inflated (it was 60+ MB before)


def test_cimd_fetch_asks_for_identity_and_refuses_gzip():
    seen = {}

    def handle(request):
        seen["accept-encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                              stream=httpx.ByteStream(gzip.compress(b"\0" * (8 << 20))))

    resolver = CimdResolver(public_client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ClientError, match="compressed"):
        resolver.resolve(ATTACKER_CIMD)
    assert seen["accept-encoding"] == "identity"


# ---------- H2: slow metadata hosts ----------

class Drip(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for _ in range(12):  # one header byte every 0.4 s: every single read is well inside its timeout
                self.wfile.write(b"X")
                self.wfile.flush()
                time.sleep(0.4)
            self.wfile.write(b": y\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        except OSError:
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def drip_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/c.json"
    server.shutdown()


def loopback_client() -> httpx.Client:
    """The real safe transport (sockets, deadline), with the public-address and port checks relaxed only so the
    test can reach its own loopback server."""
    return public_client(transport=PublicOnlyTransport(check=lambda address: True, ports=None))


def test_drip_feeding_server_is_cut_at_the_deadline(drip_server):
    start = time.monotonic()
    with pytest.raises(httpx.TimeoutException), deadline(1.0):
        loopback_client().get(drip_server, timeout=3.0)
    assert time.monotonic() - start < 2.0  # it took 5+ s before (per-read timeout only)


def test_cimd_fetch_has_a_total_deadline_and_caches_the_failure(drip_server, monkeypatch):
    monkeypatch.setattr(clients_mod, "CIMD_DEADLINE", 1.0)
    monkeypatch.setattr(clients_mod, "check_cimd_id", lambda client_id: None)
    resolver = CimdResolver(loopback_client())
    start = time.monotonic()
    with pytest.raises(ClientError, match="could not be fetched"):
        resolver.resolve(drip_server)
    assert time.monotonic() - start < 2.0
    start = time.monotonic()
    with pytest.raises(ClientError):  # answered from memory: the slow host is not asked again for 60 s
        resolver.resolve(drip_server)
    assert time.monotonic() - start < 0.1


def test_outbound_connections_port_443_only_and_every_address_tried(monkeypatch):
    backend = PublicOnlyBackend(resolve=lambda host, port, type: [(socket.AF_INET, type, 6, "", ("8.8.8.8", port))])
    with pytest.raises(httpcore.ConnectError, match="port"):
        backend.connect_tcp("example.com", 80)
    tried = []

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        tried.append(host)
        if host == "93.184.216.34":
            raise httpcore.ConnectError("unreachable")
        return "stream"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = PublicOnlyBackend(resolve=lambda host, port, type: [
        (socket.AF_INET, type, 6, "", ("93.184.216.34", port)), (socket.AF_INET, type, 6, "", ("8.8.4.4", port))])
    assert backend.connect_tcp("example.com", 443)._stream == "stream"
    assert tried == ["93.184.216.34", "8.8.4.4"]


def test_slow_metadata_hosts_do_not_starve_other_requests():
    google = FakeGoogle(f"{PUBLIC}/oauth/google/callback")

    def slow_internet(request):  # stands in for a drip-feeding host
        time.sleep(1.5)
        return httpx.Response(404)

    public_http = httpx.Client(transport=httpx.MockTransport(slow_internet))
    app = create_hosted_app(settings_for(), store=MemoryStore(), http=httpx.Client(transport=google.transport()),
                            public_http=public_http, webhooks=WebhookSender(public_http))
    with TestClient(app, base_url=PUBLIC) as client:
        def shrink():
            anyio.to_thread.current_default_thread_limiter().total_tokens = 2
        client.portal.call(shrink)
        attackers = [threading.Thread(target=client.get, args=("/authorize",),
                                      kwargs={"params": {"client_id": f"https://slow{i}.example/c.json"}})
                     for i in range(2)]
        for thread in attackers:
            thread.start()
        time.sleep(0.3)
        start = time.monotonic()
        assert client.get("/healthz").status_code == 200
        waited = time.monotonic() - start
        # every outbound slot is busy: another metadata fetch fails at once instead of queueing
        app.state.auth_server.outbound.total_tokens = 2
        busy = client.get("/authorize", params={"client_id": "https://slow9.example/c.json"})
        for thread in attackers:
            thread.join()
    assert waited < 0.5  # it waited 3.5 s behind 2 unauthenticated /authorize requests before
    assert busy.status_code == 503 and "busy" in busy.text


# ---------- H3: webhooks ----------

def test_webhook_reply_body_is_not_read():
    consumed = {"n": 0}

    def body():
        for _ in range(200):  # 200 MB; a real receiver can stream forever
            consumed["n"] += 1 << 20
            yield b"\0" * (1 << 20)

    sender = WebhookSender(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=body()))), retry_delays=())
    sender.send("https://hook.example/x", "whsec_x", "ask.answered", "d1", {})
    assert sender.flush(5)
    assert consumed["n"] <= 2 << 20  # 200 MB before


def test_one_broken_receiver_does_not_delay_others():
    delivered = {}

    def handle(request):
        if request.url.host == "slow.example":
            return httpx.Response(503)
        delivered["at"] = time.monotonic()
        return httpx.Response(200)

    sender = WebhookSender(httpx.Client(transport=httpx.MockTransport(handle)), retry_delays=(0.5, 0.5, 0.5))
    start = time.monotonic()
    sender.send("https://slow.example/h", "s", "ask.answered", "a", {})  # user A's broken receiver
    sender.send("https://fast.example/h", "s", "ask.answered", "b", {})  # user B
    assert sender.flush(10)
    assert delivered["at"] - start < 0.4  # it waited behind all of A's retries before


def test_webhook_schedule_is_bounded_and_retry_after_capped():
    sender = WebhookSender(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
                           queue_max=2, workers=0)
    assert sender.send("https://a.example/h", "s", "t", "1", {})
    assert sender.send("https://a.example/h", "s", "t", "2", {})
    assert sender.send("https://a.example/h", "s", "t", "3", {}) is False  # full: dropped, not queued forever
    flood = WebhookSender(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        429, headers={"Retry-After": "100000"}))))
    from server.hosted.webhooks import Delivery
    item = Delivery("https://a.example/h", "s", "t", "x", b"{}", "a.example")
    assert flood._attempt(item) == 300.0  # capped (it could be 600 s and slept in the only worker before)


def outcomes_of(statuses, retry_delays=(0.01, 0.01, 0.01)):
    """Deliver once through a receiver answering `statuses` in turn (an int, or an exception to raise)."""
    replies = list(statuses)
    calls, results = [], []

    def handle(request):
        calls.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(reply)
    sender = WebhookSender(httpx.Client(transport=httpx.MockTransport(handle)), retry_delays=retry_delays)
    sender.on_result = lambda *outcome: results.append(outcome)
    sender.send("https://hook.example/x", "whsec_x", "ask.answered", "d1", {}, agent_id="ag_1")
    assert sender.flush(5)
    return calls, results


def test_final_outcome_is_reported_once():
    calls, results = outcomes_of([500, 200])
    assert len(calls) == 2 and results == [("ag_1", True, 200, None)]
    calls, results = outcomes_of([404])
    assert len(calls) == 1 and results == [("ag_1", False, 404, "HTTP 404")]
    down = httpx.ConnectError("refused")
    calls, results = outcomes_of([down, down, down, down])
    assert len(calls) == 4 and results == [("ag_1", False, None, "ConnectError")]


def test_try_once_does_not_retry():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503)
    sender = WebhookSender(httpx.Client(transport=httpx.MockTransport(handle)))
    result = sender.try_once("https://hook.example/x", "whsec_x", "ping", "d2", {"type": "ping"})
    assert result == {"ok": False, "status": 503, "error": "HTTP 503"} and len(calls) == 1
    assert calls[0].headers["askhooman-event"] == "ping" and calls[0].headers["askhooman-delivery"] == "d2"
    ok = WebhookSender(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204))))
    assert ok.try_once("https://hook.example/x", "s", "ping", "d3", {}) == {"ok": True, "status": 204, "error": None}


# ---------- M1: no open redirect before the user acted ----------

def test_no_redirect_to_a_cimd_redirect_uri_on_bad_requests(hosted, google):
    google.publish(ATTACKER_CIMD, {"client_id": ATTACKER_CIMD, "client_name": "x",
                                   "redirect_uris": ["https://phish.example/login"]})
    response = hosted.get("/authorize", params={"client_id": ATTACKER_CIMD, "response_type": "nope"},
                          follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers
    assert "phish.example" not in response.text


def test_no_redirect_to_a_dcr_redirect_uri_on_bad_requests(hosted):
    client = register(hosted, redirect_uris=("https://phish.example/x",))
    response = hosted.get("/authorize", params={"client_id": client["client_id"]}, follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers


# ---------- M2: a new client can't take over an existing agent by its name ----------

def test_new_client_named_like_an_agent_must_be_confirmed(hosted, google):
    legit_client = register(hosted, client_name="Allen")
    legit = connect(hosted, google, legit_client["client_id"], agent="Allen")
    ask = tool_result(mcp(hosted, legit["access_token"], "ask_create", {"title": "Pay invoice 42?"}))

    evil = register(hosted, redirect_uris=("https://attacker.example/cb",), client_name="Allen")
    verifier, challenge = pkce()
    url = to_consent(hosted, google, authorize(hosted, evil["client_id"], challenge, "https://attacker.example/cb"))
    page = hosted.get(url).text
    assert 'value="Allen"' not in page and 'placeholder="My favorite agent"' in page  # not prefilled
    # the user types (or keeps) "Allen" and clicks Allow: no code, a confirmation page instead
    typed = hosted.post(form_action(page), data={"csrf": csrf_of(page), "agent_name": "Allen", "action": "allow"},
                        follow_redirects=False)
    assert typed.status_code == 200 and "location" not in typed.headers
    assert "Connect as existing agent Allen" in typed.text and "already connected" in typed.text
    agents = hosted.get("/api/agents", headers=id_token(google)).json()["agents"]
    assert [a["client_id"] for a in agents] == [legit_client["client_id"]]
    # "Allow as new agent" with another name: a separate agent that can't touch Allen's asks
    allowed = hosted.post(form_action(typed.text), data={"csrf": csrf_of(typed.text), "agent_name": "Allen 2",
                                                         "action": "allow"}, follow_redirects=False)
    code = query(allowed.headers["location"])["code"]
    tokens = hosted.post("/token", data={"grant_type": "authorization_code", "code": code,
                                         "redirect_uri": "https://attacker.example/cb",
                                         "client_id": evil["client_id"], "code_verifier": verifier}).json()
    assert "forbidden" in tool_error(mcp(hosted, tokens["access_token"], "ask_close", {"id": ask["id"]}))


def test_explicit_confirmation_adds_the_client_without_overwriting(hosted, google):
    first = register(hosted, client_name="Claude Code")
    connect(hosted, google, first["client_id"], agent="Allen")
    second = register(hosted, client_name="Claude Desktop")
    verifier, challenge = pkce()
    url = to_consent(hosted, google, authorize(hosted, second["client_id"], challenge))
    page = hosted.get(url).text
    typed = hosted.post(form_action(page), data={"csrf": csrf_of(page), "agent_name": "Allen", "action": "allow"})
    confirmed = hosted.post(form_action(typed.text), data={"csrf": csrf_of(typed.text), "agent_name": "Allen",
                                                           "confirm": "allen", "action": "allow"},
                            follow_redirects=False)
    assert confirmed.status_code == 303 and "code=" in confirmed.headers["location"]
    agent = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]
    assert agent["client_id"] == first["client_id"]  # the original client stays
    assert [c["client_name"] for c in agent["clients"]] == ["Claude Code", "Claude Desktop"]
    # a forged confirm for another name doesn't count
    url = to_consent(hosted, google, authorize(hosted, register(hosted)["client_id"], pkce()[1]))
    page = hosted.get(url).text
    forged = hosted.post(form_action(page), data={"csrf": csrf_of(page), "agent_name": "Allen", "confirm": "bob",
                                                  "action": "allow"}, follow_redirects=False)
    assert forged.status_code == 200 and "Connect as existing agent" in forged.text


def test_agent_keys_do_not_silently_join_an_existing_agent(hosted, google):
    connect(hosted, google, register(hosted)["client_id"], agent="Allen")
    taken = hosted.post("/api/agents/keys", json={"name": "Allen"}, headers=id_token(google))
    assert taken.status_code == 409 and "existing" in taken.json()["detail"]
    joined = hosted.post("/api/agents/keys", json={"name": "Allen", "existing": True}, headers=id_token(google))
    assert joined.status_code == 201 and joined.json()["agent"]["slug"] == "allen"
    assert len(hosted.get("/api/agents", headers=id_token(google)).json()["agents"]) == 1


# ---------- M3: Drive reads are bounded ----------

def _old_ask(item_id: int, status: str = "closed", **extra) -> dict:
    return {"id": item_id, "agent": "allen", "title": f"#{item_id}", "context": None, "options": [], "links": [],
            "priority": "normal", "ref": None, "status": status, "answer": None, "answered_at": None,
            "resolution": "done" if status == "closed" else None, "closed_by": "allen" if status == "closed" else None,
            "closed_at": f"2026-10-01T10:{item_id // 60:02d}:{item_id % 60:02d}.000Z" if status == "closed" else None,
            "created_at": "2026-10-01T10:00:00.000Z", "updated_at": "2026-10-01T10:00:00.000Z", "events": [],
            **extra}


def test_huge_files_are_skipped(hosted, google):
    token = connect(hosted, google, register(hosted)["client_id"])["access_token"]
    drive = google.drive(DENIS_SUB)
    huge = _old_ask(5, "open", context="x" * 10, padding="y" * (300 * 1024))
    drive.add("ask-5.json", build_doc(huge, 1, {"padding": huge["padding"]}), app_properties(huge, 1, ASK))
    listed = tool_result(mcp(hosted, token, "asks_list", {"status": "all"}))
    assert listed["asks"] == []
    assert not [r for r in drive.requests if r[2].get("alt") == "media"]  # skipped by its size, not downloaded
    assert "not readable" in tool_error(mcp(hosted, token, "ask_get", {"id": 5}))


def test_lists_download_only_what_the_limit_needs(hosted, google):
    token = connect(hosted, google, register(hosted)["client_id"])["access_token"]
    drive = google.drive(DENIS_SUB)
    for item_id in range(1, 151):
        ask = _old_ask(item_id)
        drive.add(f"ask-{item_id}.json", build_doc(ask, 1), app_properties(ask, 1, ASK))
    listed = hosted.get("/api/asks", params={"status": "closed", "limit": 10},
                        headers={"Authorization": f"Bearer {token}"}).json()
    assert len(listed["asks"]) == 10 and listed["counts"]["closed"] == 150
    # closing is a file's last write, so the latest closed come first (guessed from modifiedTime, then exact)
    assert [a["id"] for a in listed["asks"]] == list(range(150, 140, -1))
    downloads = [r for r in drive.requests if r[2].get("alt") == "media"]
    assert len(downloads) <= 10 + records_mod.LIST_SLACK  # 150 before


def test_listing_stops_at_max_files(hosted, google, monkeypatch):
    monkeypatch.setattr(records_mod, "MAX_FILES", 3)
    token = connect(hosted, google, register(hosted)["client_id"])["access_token"]
    drive = google.drive(DENIS_SUB)
    for item_id in range(1, 6):
        ask = _old_ask(item_id, "open")
        drive.add(f"ask-{item_id}.json", build_doc(ask, 1), app_properties(ask, 1, ASK))
    listed = tool_result(mcp(hosted, token, "asks_list", {"status": "all"}))
    assert len(listed["asks"]) == 3


# ---------- M4: resource-server limits ----------

def test_junk_bearers_cost_no_store_lookup(hosted, monkeypatch):
    lookups = []
    original = hosted.store.get

    def counting_get(collection, key):
        lookups.append(collection)
        return original(collection, key)

    monkeypatch.setattr(hosted.store, "get", counting_get)
    for junk in ("nope", "qta_short", "qta_" + "x" * 44, "eyJhbGciOi.fake.jwt", "qtk_" + "/" * 40):
        assert mcp(hosted, junk, "asks_mine").status_code == 401
    assert "tokens" not in lookups and "keys" not in lookups


def test_failed_bearers_from_one_address_are_slowed_down(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"])
    hosted.app.state.limiter.limits["bearer_fail"] = (3, 60.0)
    for _ in range(3):
        assert mcp(hosted, "qta_" + "A" * 43, "asks_mine").status_code == 401
    assert mcp(hosted, "qta_" + "B" * 43, "asks_mine").status_code == 429
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 429  # before any lookup


def test_per_agent_rate_limit(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"])
    hosted.app.state.limiter.limits["agent"] = (2, 60.0)
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 200
    assert hosted.get("/api/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).status_code == 200
    limited = mcp(hosted, tokens["access_token"], "asks_mine")
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"


# ---------- LOW ----------

def test_rate_limiter_groups_ipv6_and_evicts_instead_of_clearing():
    assert client_ip({"x-forwarded-for": "2001:db8:1:2:aaaa::1"}, None, True) == "2001:db8:1:2::/64"
    assert client_ip({"x-forwarded-for": "2001:db8:1:2:ffff::9"}, None, True) == "2001:db8:1:2::/64"
    assert client_ip({}, "203.0.113.7", True) == "203.0.113.7"
    now = [0.0]
    limiter = RateLimiter({"register": (2, 60.0)}, clock=lambda: now[0], max_keys=3)
    for _ in range(2):
        assert limiter.allow("register", "victim")
    assert not limiter.allow("register", "victim")
    limiter.allow("register", "a")
    limiter.allow("register", "victim")  # still trying: recently used
    limiter.allow("register", "b")
    limiter.allow("register", "c")  # table full: the least recently used ("a") goes, not everything
    assert limiter.blocked("register", "victim")


def test_refresh_reuse_race_kills_the_winner_too(hosted, google, monkeypatch):
    client = register(hosted)
    tokens = connect(hosted, google, client["client_id"])
    r1 = tokens["refresh_token"]
    original = Accounts.issue_tokens

    def slow_issue(self, grant, family):
        time.sleep(0.3)  # widen the window between "mark used" and "write new tokens" (Firestore latency)
        return original(self, grant, family)

    monkeypatch.setattr(Accounts, "issue_tokens", slow_issue)
    results = {}
    form = {"grant_type": "refresh_token", "refresh_token": r1, "client_id": client["client_id"]}
    winner = threading.Thread(target=lambda: results.update(win=hosted.post("/token", data=form)))
    winner.start()
    time.sleep(0.1)
    loser = hosted.post("/token", data=form)
    winner.join()
    assert loser.status_code == 400
    win = results["win"]
    if win.status_code == 200:  # the winner got tokens before the revocation: they must be dead anyway
        assert mcp(hosted, win.json()["access_token"], "asks_mine").status_code == 401
        assert hosted.post("/token", data={**form, "refresh_token": win.json()["refresh_token"]}).status_code == 400
    else:
        assert win.json()["error"] == "invalid_grant"


def test_agent_named_denis_does_not_write_the_humans_actor(hosted, google):
    token = connect(hosted, google, register(hosted)["client_id"], agent="Denis")["access_token"]
    ask = tool_result(mcp(hosted, token, "ask_create", {"title": "x?"}))
    closed = tool_result(mcp(hosted, token, "ask_close", {"id": ask["id"]}))
    assert ask["agent"] == "denis-agent" and closed["closed_by"] == "denis-agent"


def test_non_latin_names_are_different_agents(hosted, google):
    connect(hosted, google, register(hosted)["client_id"], agent="日本語")
    connect(hosted, google, register(hosted)["client_id"], agent="中文助手")
    agents = hosted.get("/api/agents", headers=id_token(google)).json()["agents"]
    assert len(agents) == 2 and len({a["slug"] for a in agents}) == 2
    assert all(a["slug"].startswith("agent-") for a in agents)


def test_public_url_is_canonical():
    url = public_url("HTTPS://Qtracker.Allover.art:443/")
    assert url == "https://qtracker.allover.art"
    assert canonical_resource(url + "/mcp") == url + "/mcp"
    assert public_url("http://localhost:8799") == "http://localhost:8799"


def test_localhost_warning_follows_the_redirect_used(hosted, google):
    client = register(hosted, redirect_uris=("https://app.example/cb", LOOPBACK))
    page = hosted.get(to_consent(hosted, google, authorize(hosted, client["client_id"], pkce()[1],
                                                           "https://app.example/cb"))).text
    assert "runs on your own computer" not in page and "app.example" in page
    page = hosted.get(to_consent(hosted, google, authorize(hosted, client["client_id"], pkce()[1], LOOPBACK))).text
    assert "runs on your own computer" in page


def test_registrations_are_validated_expire_unused_and_names_are_clean(hosted, google):
    accounts = hosted.app.state.accounts
    assert accounts.dcr_client("qtc_../../users/1001") is None
    assert hosted.get("/authorize", params={"client_id": "qtc_a/b"}).status_code == 400
    unused = register(hosted, client_name="Al‮le​n⁦")
    assert unused["client_name"] == "Allen"  # bidi overrides, isolates and zero-width characters removed
    used = register(hosted)
    connect(hosted, google, used["client_id"], agent="Ze‮ta")
    assert hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["name"] == "Zeta"
    real_clock = accounts.clock
    accounts.clock = lambda: real_clock() + 2 * 86400
    assert accounts.dcr_client(unused["client_id"]) is None  # nobody signed in with it within a day
    assert accounts.dcr_client(used["client_id"]) is not None


def test_hosted_tool_errors_hide_exception_text(hosted, google, monkeypatch):
    token = connect(hosted, google, register(hosted)["client_id"])["access_token"]

    def broken(self, *args, **kwargs):
        raise RuntimeError("projects/qtracker/databases/(default)/documents/users/1001 secret detail")

    monkeypatch.setattr(HostedAsks, "list", broken)
    text = tool_error(mcp(hosted, token, "asks_mine"))
    assert "RuntimeError" in text and "secret detail" not in text and "documents" not in text
    assert json.dumps(text)
