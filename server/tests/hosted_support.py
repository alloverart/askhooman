"""Shared fixtures and helpers for the hosted-bridge tests (no network: FakeGoogle + MemoryStore)."""

import base64
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from server.hosted.app import create_hosted_app
from server.hosted.config import HostedSettings
from server.hosted.store import MemoryStore
from server.hosted.webhooks import WebhookSender

from .fake_google import IOS_CLIENT_ID, WEB_CLIENT_ID, WEB_CLIENT_SECRET, FakeGoogle, s256

PUBLIC = "https://qtracker.test"
RESOURCE = f"{PUBLIC}/mcp"
KEY = base64.b64encode(b"k" * 32).decode()
LOOPBACK = "http://127.0.0.1:33418/callback"
DENIS_SUB, DENIS_EMAIL = "1001", "owner@example.com"
OLGA_SUB, OLGA_EMAIL = "2002", "olga@example.com"


def settings_for(public: str = PUBLIC, aliases: tuple[str, ...] = ()) -> HostedSettings:
    return HostedSettings(public_url=public, web_client_id=WEB_CLIENT_ID, web_client_secret=WEB_CLIENT_SECRET,
                          app_client_ids=(IOS_CLIENT_ID,), encryption_keys=KEY, store="memory", aliases=aliases)


def build(google: FakeGoogle, public: str = PUBLIC, aliases: tuple[str, ...] = ()):
    store = MemoryStore()
    public_http = httpx.Client(transport=google.public_transport())
    app = create_hosted_app(settings_for(public, aliases), store=store, http=httpx.Client(transport=google.transport()),
                            public_http=public_http,
                            webhooks=WebhookSender(public_http, retry_delays=(0.01, 0.01)))
    return app, store


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle(f"{PUBLIC}/oauth/google/callback")


@pytest.fixture
def hosted(google):
    app, store = build(google)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store = store
        client.google = google
        yield client


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, s256(verifier)


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def register(client, redirect_uris=(LOOPBACK,), **extra) -> dict:
    body = {"client_name": "Claude Code", "redirect_uris": list(redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none", **extra}
    response = client.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def authorize(client, client_id: str, challenge: str, redirect_uri: str | None = LOOPBACK, state: str = "st-1",
              resource: str | None = RESOURCE, **extra) -> httpx.Response:
    params = {"response_type": "code", "client_id": client_id, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": state, "scope": "qtracker", **extra}
    if redirect_uri is not None:
        params["redirect_uri"] = redirect_uri
    if resource is not None:
        params["resource"] = resource
    return client.get("/authorize", params=params, follow_redirects=False)


def to_consent(client, google: FakeGoogle, response: httpx.Response, sub: str = DENIS_SUB,
               email: str = DENIS_EMAIL, **sign_in) -> str:
    """From /authorize's redirect to Google to the consent page URL (following a forced consent round)."""
    for _ in range(2):
        assert response.status_code == 302, response.text
        callback = google.sign_in(response.headers["location"], sub, email, **sign_in)
        response = client.get(callback, follow_redirects=False)
        if response.status_code == 303:
            assert response.headers["location"].startswith("/oauth/consent?r=")
            return response.headers["location"]
    raise AssertionError(f"no consent page: {response.status_code} {response.text[:300]}")


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def form_action(html: str) -> str:
    return re.search(r'<form method="post" action="([^"]+)"', html).group(1).replace("&amp;", "&")


def consent(client, consent_url: str, agent_name: str = "Allen", action: str = "allow") -> httpx.Response:
    page = client.get(consent_url)
    assert page.status_code == 200, page.text
    assert form_action(page.text) == consent_url  # the browser posts the form where the page says
    form = {"csrf": csrf_of(page.text), "agent_name": agent_name, "action": action}
    return client.post(form_action(page.text), data=form,
                       follow_redirects=False)


def connect(client, google: FakeGoogle, client_id: str, redirect_uri: str = LOOPBACK, agent: str = "Allen",
            sub: str = DENIS_SUB, email: str = DENIS_EMAIL, secret: str | None = None, origin: str = PUBLIC) -> dict:
    """The whole OAuth flow for one agent; returns the token response. origin: the one `client` talks to (the
    public URL or an alias), which is the issuer and the resource's base."""
    verifier, challenge = pkce()
    consent_url = to_consent(client, google, authorize(client, client_id, challenge, redirect_uri,
                                                       resource=f"{origin}/mcp"), sub, email)
    response = consent(client, consent_url, agent)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(redirect_uri.split("?")[0])
    params = query(location)
    assert params["state"] == "st-1" and params["iss"] == origin
    form = {"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier, "resource": f"{origin}/mcp"}
    if secret:
        form["client_secret"] = secret
    tokens = client.post("/token", data=form)
    assert tokens.status_code == 200, tokens.text
    return tokens.json()


def mcp(client, token: str, tool: str, arguments: dict | None = None):
    """One tools/call as a 2025-06-18 client would send it (stateless server: no session needed)."""
    response = client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                                            "Accept": "application/json, text/event-stream",
                                            "MCP-Protocol-Version": "2025-06-18"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": tool, "arguments": arguments or {}}})
    return response


def tool_result(response) -> dict:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def tool_error(response) -> str:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError"), result
    return result["content"][0]["text"]


def id_token(google: FakeGoogle, sub: str = DENIS_SUB, email: str = DENIS_EMAIL, **kwargs) -> dict:
    return {"Authorization": f"Bearer {google.id_token(sub, email, IOS_CLIENT_ID, **kwargs)}"}
