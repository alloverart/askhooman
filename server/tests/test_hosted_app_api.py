"""Hosted bridge: the App API (Google ID tokens), agent keys and webhooks."""

import json
import time

from cryptography.hazmat.primitives.asymmetric import rsa

from server.hosted.webhooks import verify

from .fake_drive import app_action
from .fake_google import WEB_CLIENT_ID
from .hosted_support import (
    DENIS_SUB,
    OLGA_SUB,
    PUBLIC,
    RESOURCE,
    connect,
    id_token,
    mcp,
    register,
    tool_result,
)


def test_id_token_is_verified(hosted, google):
    assert hosted.get("/api/agents").status_code == 401
    ok = hosted.get("/api/agents", headers=id_token(google))
    assert ok.status_code == 200 and ok.json() == {"agents": []}
    wrong_aud = {"Authorization": f"Bearer {google.id_token(DENIS_SUB, 'd@x', WEB_CLIENT_ID)}"}
    expired = id_token(google, expires_in=-3600)
    foreign_key = id_token(google, key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    unknown_kid = id_token(google, kid="nope")
    wrong_iss = id_token(google, iss="https://evil.example")
    for headers in (wrong_aud, expired, foreign_key, unknown_kid, wrong_iss, {"Authorization": "Bearer not-a-jwt"}):
        response = hosted.get("/api/agents", headers=headers)
        assert response.status_code == 401, response.text
    # an agent's access token is not an app credential, and an ID token is not an agent credential
    tokens = connect(hosted, google, register(hosted)["client_id"])
    assert hosted.get("/api/agents", headers={"Authorization": f"Bearer {tokens['access_token']}"}).status_code == 401
    assert hosted.get("/api/asks", headers=id_token(google)).status_code == 401


def test_agents_list_rename_revoke(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"], agent="Sonya Allen")
    agents = hosted.get("/api/agents", headers=id_token(google)).json()["agents"]
    assert [(a["name"], a["slug"], a["client_name"]) for a in agents] == [("Sonya Allen", "sonya-allen", "Claude Code")]
    assert agents[0]["last_seen"] and agents[0]["revoked_at"] is None
    agent_id = agents[0]["id"]
    assert hosted.get("/api/agents", headers=id_token(google, sub=OLGA_SUB)).json() == {"agents": []}
    assert hosted.patch(f"/api/agents/{agent_id}", json={"name": "x"},
                        headers=id_token(google, sub=OLGA_SUB)).status_code == 404  # not hers
    renamed = hosted.patch(f"/api/agents/{agent_id}", json={"name": "Allen"}, headers=id_token(google))
    assert renamed.json()["agent"]["name"] == "Allen" and renamed.json()["agent"]["slug"] == "sonya-allen"
    assert hosted.patch(f"/api/agents/{agent_id}", json={"slug": "x"}, headers=id_token(google)).status_code == 422
    revoked = hosted.delete(f"/api/agents/{agent_id}", headers=id_token(google))
    assert revoked.status_code == 200 and revoked.json()["agent"]["revoked_at"]
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 401  # all its tokens die
    refresh = hosted.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                          "client_id": agents[0]["client_id"]})
    assert refresh.json()["error"] == "invalid_grant"
    assert hosted.get("/api/agents", headers=id_token(google)).json() == {"agents": []}


def test_agent_key(hosted, google):
    created = hosted.post("/api/agents/keys", json={"name": "Hamilton"}, headers=id_token(google))
    assert created.status_code == 201
    body = created.json()
    key = body["key"]
    assert key.startswith("qtk_") and body["agent"]["slug"] == "hamilton" and body["agent"]["has_key"]
    assert body["drive_linked"] is False and body["link_url"].endswith("/oauth/link")
    assert key not in json.dumps(hosted.store.all("keys")) + json.dumps(hosted.store.all("agents"))
    listed = hosted.get("/api/agents", headers=id_token(google)).json()["agents"]
    assert "key" not in json.dumps(listed).replace("key_prefix", "").replace("has_key", "")
    # not linked to Drive yet: the agent learns where its user must allow access
    text = mcp(hosted, key, "asks_mine").json()["result"]["content"][0]["text"]
    assert "/oauth/link" in text
    # the user links Drive in the browser (Google sign-in only), then the key works like an OAuth agent
    start = hosted.get("/oauth/link", follow_redirects=False)
    assert "prompt=consent" in start.headers["location"]
    linked = hosted.get(google.sign_in(start.headers["location"], DENIS_SUB, "owner@example.com"))
    assert linked.status_code == 200 and "Google Drive linked" in linked.text
    assert hosted.get("/api/connect-info", headers=id_token(google)).json()["drive_linked"] is True
    ask = tool_result(mcp(hosted, key, "ask_create", {"title": "Via key?"}))
    assert ask["agent"] == "hamilton" and google.drive(DENIS_SUB).doc(1)["agent"] == "hamilton"
    rest = hosted.get("/api/me", headers={"Authorization": f"Bearer {key}"})
    assert rest.json() == {"role": "agent", "agent": "hamilton"}
    hosted.delete(f"/api/agents/{body['agent']['id']}", headers=id_token(google))
    assert mcp(hosted, key, "asks_mine").status_code == 401


def test_connect_info(hosted, google):
    info = hosted.get("/api/connect-info", headers=id_token(google)).json()
    assert info["server_url"] == RESOURCE
    ids = [c["id"] for c in info["clients"]]
    assert "claude" in ids and "claude-code" in ids and "agent-key" in ids
    code = next(c for c in info["clients"] if c["id"] == "claude-code")
    assert code["command"] == f"claude mcp add --transport http qtracker {RESOURCE}"


def test_events_call_the_agents_webhook_signed_and_retried(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"], agent="Allen")
    agent_id = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["id"]
    bad = hosted.patch(f"/api/agents/{agent_id}", json={"webhook_url": "http://10.0.0.1/hook"},
                       headers=id_token(google))
    assert bad.status_code == 422
    set_hook = hosted.patch(f"/api/agents/{agent_id}", json={"webhook_url": "https://hooks.example/qt?k=1"},
                            headers=id_token(google))
    secret = set_hook.json()["webhook_secret"]
    assert secret.startswith("whsec_") and set_hook.json()["agent"]["webhook_secret_set"] is True
    assert secret not in json.dumps(hosted.store.all("agents"))  # encrypted at rest
    again = hosted.patch(f"/api/agents/{agent_id}", json={"name": "Allen"}, headers=id_token(google)).json()
    assert "webhook_secret" not in again  # shown once

    ask = tool_result(mcp(hosted, tokens["access_token"], "ask_create", {"title": "Ship?"}))
    app_action(google.drive(DENIS_SUB), ask["id"], "answered", answer="Yes, ship")
    google.webhook_statuses.extend([500, 200])  # first delivery fails, the retry succeeds
    sent = hosted.post("/api/events", json={"ask_id": ask["id"], "kind": "answered"}, headers=id_token(google))
    assert sent.status_code == 202 and sent.json() == {"queued": 1}
    assert hosted.app.state.webhooks.flush(5)
    assert len(google.webhook_calls) == 2
    call = google.webhook_calls[-1]
    assert str(call.url) == "https://hooks.example/qt?k=1"
    assert verify(secret, call.content, call.headers["qtracker-signature"])
    assert not verify("whsec_wrong", call.content, call.headers["qtracker-signature"])
    assert not verify(secret, call.content, call.headers["qtracker-signature"], now=time.time() + 3600)
    payload = json.loads(call.content)
    assert payload["type"] == "ask.answered" and payload["answer"] == "Yes, ship"
    assert (payload["ask"]["id"], payload["ask"]["status"]) == (ask["id"], "answered")
    assert "events" not in payload["ask"]
    assert payload["event"]["id"] == 2 and call.headers["qtracker-event"] == "ask.answered"
    assert call.headers["qtracker-delivery"] == payload["delivery_id"]
    # an event that doesn't match the file is refused; another user's ask is not found
    wrong = hosted.post("/api/events", json={"ask_id": ask["id"], "kind": "closed"}, headers=id_token(google))
    assert wrong.status_code == 409
    missing = hosted.post("/api/events", json={"ask_id": 99, "kind": "closed"}, headers=id_token(google))
    assert missing.status_code == 404
    assert hosted.post("/api/events", json={"ask_id": 1, "kind": "edited"}, headers=id_token(google)).status_code == 422


def test_webhook_gives_up_on_4xx(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"])
    agent_id = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["id"]
    hosted.patch(f"/api/agents/{agent_id}", json={"webhook_url": "https://hooks.example/x"}, headers=id_token(google))
    ask = tool_result(mcp(hosted, tokens["access_token"], "ask_create", {"title": "Q?"}))
    app_action(google.drive(DENIS_SUB), ask["id"], "closed")
    google.webhook_statuses.extend([410])
    hosted.post("/api/events", json={"ask_id": ask["id"], "kind": "closed"}, headers=id_token(google))
    assert hosted.app.state.webhooks.flush(5)
    assert len(google.webhook_calls) == 1
    assert json.loads(google.webhook_calls[0].content)["resolution"] == "Not relevant (Denis)"


# ---------- the public site: landing page and the icon kit ----------

def test_landing_page_and_icons(hosted):
    landing = hosted.get("/")
    assert landing.status_code == 200 and "Questions from your AI agents" in landing.text
    assert f'property="og:image" content="{PUBLIC}/og-image-1200x630.png"' in landing.text
    assert "noindex" not in landing.text and 'href="/favicon.ico"' in landing.text
    for path, kind in (("/favicon.ico", "image/x-icon"), ("/favicon.svg", "image/svg+xml"),
                       ("/apple-touch-icon.png", "image/png"), ("/site.webmanifest", "application/manifest+json"),
                       ("/og-image-1200x630.png", "image/png"), ("/icon-maskable-512x512.png", "image/png")):
        response = hosted.get(path)
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind) and response.content
        assert "default-src 'none'" in response.headers["content-security-policy"]
    error = hosted.get("/oauth/consent")
    assert "noindex" in error.text and "/favicon.svg" in error.text  # every page carries the icons, only / is indexed
