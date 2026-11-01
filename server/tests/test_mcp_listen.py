"""No endless `subscriptions/listen` streams (MCP 2026-07-28).

A 2026-07-28 client (Claude Code 2.1.31x) opens with `server/discover` and, when the server advertises
`listChanged`, POSTs `subscriptions/listen`, whose response is an SSE stream the server holds open until the
client goes away. Our tool list never changes and nothing is ever pushed, so such a stream only keeps a
request open: on Cloud Run until the 300 s request timeout, after which the client re-listens, for as long as
an agent stays connected. The server must neither advertise list changes nor serve the method."""

import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn

from .conftest import ALLEN, _free_port, auth
from .fake_google import FakeGoogle
from .hosted_support import DENIS_EMAIL, DENIS_SUB, build

MODERN = "2026-07-28"
# The envelope Claude Code 2.1.316 sends with every request (captured from a local run).
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "2.1.316"},
    "io.modelcontextprotocol/clientCapabilities": {"roots": {"listChanged": True}},
}


def _post(http: httpx.Client, request_id, method: str, params: dict) -> httpx.Response:
    return http.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method,
                                   "params": {"_meta": META, **params}},
                     headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": method})


def assert_no_listen_stream(base_url: str, token: str) -> None:
    headers = {**auth(token), "Accept": "application/json, text/event-stream"}
    # A held-open stream sends its acknowledgement at once and then only a keepalive every 15 s: the 5 s read
    # timeout turns a regression into a failure instead of a hung test.
    with httpx.Client(base_url=base_url, headers=headers, timeout=5) as http:
        discover = _post(http, "server-discover-probe-1", "server/discover", {})
        assert discover.status_code == 200, discover.text
        capabilities = discover.json()["result"]["capabilities"]
        for kind in ("tools", "prompts", "resources"):
            assert not capabilities.get(kind, {}).get("listChanged"), capabilities
        assert not capabilities.get("resources", {}).get("subscribe"), capabilities

        listen = _post(http, "listen:0", "subscriptions/listen", {
            "notifications": {"toolsListChanged": True, "promptsListChanged": True, "resourcesListChanged": True}})
        assert listen.headers["content-type"].startswith("application/json"), listen.headers
        assert listen.json()["error"]["code"] == -32601, listen.text  # method not found

        tools = _post(http, 2, "tools/list", {})
        assert tools.status_code == 200 and tools.json()["result"]["tools"], tools.text


@contextmanager
def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_single_tenant_bridge_serves_no_listen_stream(live_server):
    assert_no_listen_stream(live_server, ALLEN)


def test_hosted_bridge_serves_no_listen_stream():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    app, _ = build(FakeGoogle(f"{base}/oauth/google/callback"), public=base)
    app.state.accounts.touch_user(DENIS_SUB, DENIS_EMAIL)
    _, key = app.state.accounts.create_key(DENIS_SUB, "claude")
    with _serve(app, port):
        assert_no_listen_stream(base, key)
