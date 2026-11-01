"""Hosted bridge end to end with the official MCP SDK client and its OAuth support: 401 -> protected resource
metadata -> authorization server metadata -> DCR or CIMD -> /authorize (PKCE, resource) -> Google -> consent ->
code + iss (validated by the SDK, RFC 9207) -> /token -> MCP tool calls over a real HTTP server."""

import asyncio
import json
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from .fake_google import FakeGoogle
from .hosted_support import DENIS_SUB, build

REDIRECT = "http://localhost:3030/callback"
CIMD_URL = "https://client.example/sdk-client.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_hosted():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    google = FakeGoogle(f"{base}/oauth/google/callback")
    app, _ = build(google, public=base)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield base, google
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Storage:
    def __init__(self):
        self.tokens = None
        self.client_info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


class Browser:
    """The user's browser: follows the bridge's pages, signs in at (fake) Google, allows on the consent page."""

    def __init__(self, base: str, google: FakeGoogle, agent_name: str):
        self.base, self.google, self.agent_name = base, google, agent_name
        self.result: AuthorizationCodeResult | None = None

    async def redirect(self, authorization_url: str) -> None:
        async with httpx.AsyncClient(base_url=self.base) as browser:
            response = await browser.get(authorization_url)
            assert response.status_code == 302, response.text
            response = await browser.get(self.google.sign_in(response.headers["location"], DENIS_SUB,
                                                             "owner@example.com"))
            assert response.status_code == 303, response.text
            consent_url = response.headers["location"]
            page = await browser.get(consent_url)
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            action = re.search(r'<form method="post" action="([^"]+)"', page.text).group(1)
            response = await browser.post(action, data={"csrf": csrf, "agent_name": self.agent_name,
                                                             "action": "allow"})
            assert response.status_code == 303 and response.headers["location"].startswith(REDIRECT)
            params = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
            self.result = AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=params.get("iss"))

    async def callback(self) -> AuthorizationCodeResult:
        return self.result


async def _session(base: str, google: FakeGoogle, agent: str, mode: str, client_metadata_url: str | None):
    browser = Browser(base, google, agent)
    storage = Storage()
    provider = OAuthClientProvider(
        server_url=f"{base}/mcp",
        client_metadata=OAuthClientMetadata(client_name="SDK test client", redirect_uris=[REDIRECT],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=browser.redirect, callback_handler=browser.callback,
        client_metadata_url=client_metadata_url)
    async with create_mcp_http_client(auth=provider) as http:
        async with Client(streamable_http_client(f"{base}/mcp", http_client=http), mode=mode) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            created = await client.call_tool("ask_create", {"title": "Connected with one URL?"})
            mine = await client.call_tool("asks_mine", {})
    return tools, created, mine, storage


def _payload(result):
    assert not result.is_error, result.content
    return result.structured_content if result.structured_content is not None else json.loads(result.content[0].text)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_client_with_dynamic_registration(live_hosted, mode):
    base, google = live_hosted
    tools, created, mine, storage = asyncio.run(_session(base, google, "Claude", mode, None))
    assert "ask_create" in tools and "tasks_mine" in tools and "ask_answer" not in tools
    ask = _payload(created)
    assert (ask["agent"], ask["title"]) == ("claude", "Connected with one URL?")
    assert [a["id"] for a in _payload(mine)["asks"]] == [ask["id"]]
    assert storage.client_info.client_id.startswith("qtc_")  # registered dynamically
    assert storage.tokens.refresh_token.startswith("qtr_")
    assert google.drive(DENIS_SUB).doc(ask["id"])["agent"] == "claude"


def test_sdk_client_with_client_id_metadata_document(live_hosted):
    base, google = live_hosted
    google.publish(CIMD_URL, {"client_id": CIMD_URL, "client_name": "SDK CIMD client", "redirect_uris": [REDIRECT],
                              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                              "token_endpoint_auth_method": "none"})
    tools, created, _, storage = asyncio.run(_session(base, google, "Feynman", "auto", CIMD_URL))
    assert _payload(created)["agent"] == "feynman"
    assert storage.client_info.client_id == CIMD_URL  # no registration: the URL is the client id
