"""Hosted bridge: the App API (Google ID tokens), agent keys and webhooks."""

import json
import logging
import time

from cryptography.hazmat.primitives.asymmetric import rsa

from server.hosted.webhooks import verify

from .fake_drive import app_action
from .fake_google import DRIVE_SCOPE, GRANT_SCOPES, WEB_CLIENT_ID
from .hosted_support import (
    DENIS_EMAIL,
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
    assert code["command"] == f"claude mcp add --scope user --transport http askhooman {RESOURCE}"
    assert "restart" in code["steps"], "an open Claude Code sees a new server only after a restart"


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
    assert verify(secret, call.content, call.headers["askhooman-signature"])
    assert not verify("whsec_wrong", call.content, call.headers["askhooman-signature"])
    assert not verify(secret, call.content, call.headers["askhooman-signature"], now=time.time() + 3600)
    # receivers written before the rename read the Qtracker-* names: the same values go out under them too
    for name in ("signature", "event", "delivery"):
        assert call.headers[f"qtracker-{name}"] == call.headers[f"askhooman-{name}"]
    payload = json.loads(call.content)
    assert payload["type"] == "ask.answered" and payload["answer"] == "Yes, ship"
    assert (payload["ask"]["id"], payload["ask"]["status"]) == (ask["id"], "answered")
    assert "events" not in payload["ask"]
    assert payload["event"]["id"] == 2 and call.headers["askhooman-event"] == "ask.answered"
    assert call.headers["askhooman-delivery"] == payload["delivery_id"]
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


# ---------- the web app's Drive token (POST /api/drive/token) ----------

def link_drive(client, google, sub: str = DENIS_SUB, email: str = DENIS_EMAIL) -> None:
    """The user allows Drive at /oauth/link (Google sign-in only): the bridge stores a new grant."""
    start = client.get("/oauth/link", follow_redirects=False)
    linked = client.get(google.sign_in(start.headers["location"], sub, email))
    assert linked.status_code == 200 and "Google Drive linked" in linked.text


def web_refreshes(google) -> list[dict]:
    """Google refreshes for the web app's token (the bridge's own Drive calls ask for no particular scope)."""
    return [r for r in google.refreshes if r["scope"] is not None]


def drive_token(client, google, sub: str = DENIS_SUB):
    response = client.post("/api/drive/token", headers=id_token(google, sub=sub))
    if response.status_code != 401:  # (RequestGuard's 401 is not this route's)
        assert response.headers["cache-control"] == "no-store" and response.headers["pragma"] == "no-cache"
    return response


def test_drive_token_for_the_web_app(hosted, google, caplog):
    """A Drive token made with the user's own grant at the bridge, asked for drive.appdata only, and handed out
    again while it has a quarter of an hour left; only the App API's ID token gets one."""
    assert hosted.post("/api/drive/token").status_code == 401
    none = drive_token(hosted, google)  # no grant at the bridge yet: the app falls back to Google's window
    assert none.status_code == 409 and none.json()["error"] == "not_linked"
    assert f"{PUBLIC}/oauth/link" in none.json()["detail"] and google.refreshes == []

    tokens = connect(hosted, google, register(hosted)["client_id"])  # connecting an agent stores the grant
    agent = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert hosted.post("/api/drive/token", headers=agent).status_code == 401  # an agent's token is not the app's
    cache = hosted.app.state.drive_tokens
    start = time.monotonic()
    cache.clock = lambda: start
    with caplog.at_level(logging.DEBUG):
        first = drive_token(hosted, google)
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"access_token", "expires_in", "scope"} and body["scope"] == DRIVE_SCOPE
    # the fake Google counts its 3600 s on the real clock: a second may tick over before the reply
    life = body["expires_in"]
    assert life in (3598, 3599) and google.access_tokens[body["access_token"]] == DENIS_SUB
    assert google.refreshes == [{"sub": DENIS_SUB, "scope": DRIVE_SCOPE}]  # drive.appdata only
    assert body["access_token"] not in caplog.text + json.dumps(hosted.store.all("users"))
    # the token reads the user's appDataFolder, where the agents' asks are
    ask = tool_result(mcp(hosted, tokens["access_token"], "ask_create", {"title": "Seen from the web?"}))
    files = hosted.app.state.pool.http.get(
        "https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {body['access_token']}"},
        params={"spaces": "appDataFolder", "q": "name contains 'ask-' and trashed = false",
                "fields": "nextPageToken,files(id,name,appProperties,modifiedTime)"})
    assert files.status_code == 200 and f"ask-{ask['id']}.json" in files.text

    cache.clock = lambda: start + 600  # still 49 minutes left: the same token, Google isn't asked
    again = drive_token(hosted, google).json()
    assert again["access_token"] == body["access_token"] and again["expires_in"] == life - 600
    assert len(web_refreshes(google)) == 1
    cache.clock = lambda: start + 3599 - 899  # less than 15 minutes left: a new one
    fresh = drive_token(hosted, google).json()
    assert fresh["access_token"] != body["access_token"] and fresh["expires_in"] in (3598, 3599)
    assert len(web_refreshes(google)) == 2
    # another user never gets Denis's token
    olga = drive_token(hosted, google, sub=OLGA_SUB)
    assert olga.status_code == 409 and olga.json()["error"] == "not_linked" and len(web_refreshes(google)) == 2


def test_drive_token_after_the_grant_was_revoked(hosted, google):
    """Google says invalid_grant: 409, the dead grant is forgotten (connect-info says so) and Google isn't asked
    again; linking Drive again heals it, and a token of the old grant is never handed out after a new link."""
    connect(hosted, google, register(hosted)["client_id"])
    old = drive_token(hosted, google).json()["access_token"]
    for refresh, sub in google.refresh_tokens.items():
        if sub == DENIS_SUB:
            google.revoked_refresh.add(refresh)
    hosted.app.state.drive_tokens.forget(DENIS_SUB)  # another instance, without the cached token
    revoked = drive_token(hosted, google)
    assert revoked.status_code == 409 and revoked.json()["error"] == "invalid_grant", revoked.text
    assert hosted.get("/api/connect-info", headers=id_token(google)).json()["drive_linked"] is False
    asked = len(google.refreshes)
    assert drive_token(hosted, google).json()["error"] == "not_linked" and len(google.refreshes) == asked

    link_drive(hosted, google)
    healed = drive_token(hosted, google)
    assert healed.status_code == 200 and healed.json()["access_token"] != old
    # a new link (a new grant): the cached token of the previous one isn't reused
    link_drive(hosted, google)
    relinked = drive_token(hosted, google).json()["access_token"]
    assert relinked != healed.json()["access_token"] and len(google.refreshes) == asked + 2


def test_drive_token_when_google_fails_or_is_hammered(hosted, google):
    """Google unreachable or failing: 503 (try again later), the grant is kept; refreshes are limited per user, so
    a client looping on the endpoint can't make the bridge hammer Google."""
    connect(hosted, google, register(hosted)["client_id"])
    google.refresh_status = 503
    failed = drive_token(hosted, google)
    assert failed.status_code == 503 and failed.headers["retry-after"] == "30"
    assert hosted.get("/api/connect-info", headers=id_token(google)).json()["drive_linked"] is True
    for _ in range(9):
        assert drive_token(hosted, google).status_code == 503
    limited = drive_token(hosted, google)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "600"
    assert len(google.refreshes) == 10  # the 11th call didn't reach Google
    assert drive_token(hosted, google, sub=OLGA_SUB).status_code == 409  # per user


def test_drive_token_scope_when_google_wont_narrow(hosted, google):
    """If Google refused a refresh for fewer scopes, the token carries the whole grant (what the web app's own
    token has too); the response says which."""
    connect(hosted, google, register(hosted)["client_id"])
    google.narrows = False
    body = drive_token(hosted, google).json()
    assert body["scope"].split() == list(GRANT_SCOPES)
    assert [r["scope"] for r in google.refreshes] == [DRIVE_SCOPE, None]


def test_drive_token_forgotten_with_the_account(hosted, google):
    connect(hosted, google, register(hosted)["client_id"])
    assert drive_token(hosted, google).status_code == 200
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    gone = drive_token(hosted, google)  # a new, empty account: nothing cached is handed out
    assert gone.status_code == 409 and gone.json()["error"] == "not_linked"


# ---------- the public site: landing page and the icon kit ----------

def test_landing_page_and_icons(hosted):
    landing = hosted.get("/")
    assert landing.status_code == 200 and "Questions from your AI agents" in landing.text
    assert f'property="og:image" content="{PUBLIC}/og-image-1200x630.png"' in landing.text
    assert "noindex" not in landing.text and 'href="/favicon.ico"' in landing.text
    # the connect steps are the app's (connect-info), with the public URL
    assert f"claude mcp add --scope user --transport http askhooman {RESOURCE}" in landing.text and f">{RESOURCE}<" in landing.text
    assert "https://github.com/alloverart/askhooman" in landing.text and 'href="https://app.askhooman.com"' in landing.text
    for path, words in (("/privacy", ("appDataFolder", "drive.appdata", "Delete hidden app data", "support@askhooman.com",
                                      "encrypted", "one-way hashes", "within 30")),
                        ("/support", ("support@askhooman.com", "security@askhooman.com", "/issues"))):
        page = hosted.get(path)
        assert page.status_code == 200 and "noindex" not in page.text, path
        assert all(w in page.text for w in words), path
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    for link in ('href="/privacy"', 'href="/support"'):  # in every public page's footer
        assert link in landing.text
    llms = hosted.get("/llms.txt")  # for agents reading the site
    assert llms.status_code == 200 and llms.headers["content-type"].startswith("text/plain")
    assert f"askhooman {RESOURCE}" in llms.text and "asks_mine" in llms.text and f"{PUBLIC}/privacy" in llms.text and "https://app.askhooman.com" in llms.text
    for path, kind in (("/favicon.ico", "image/x-icon"), ("/favicon.svg", "image/svg+xml"),
                       ("/apple-touch-icon.png", "image/png"), ("/site.webmanifest", "application/manifest+json"),
                       ("/og-image-1200x630.png", "image/png"), ("/icon-maskable-512x512.png", "image/png"),
                       ("/ds/tokens.css", "text/css"), ("/ds/components.css", "text/css"),
                       ("/ds/fonts/inter-latin-wght-normal.woff2", "font/woff2")):
        response = hosted.get(path)
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind) and response.content
        assert "default-src 'none'" in response.headers["content-security-policy"]
    # the design system's stylesheets and Inter come from our own origin, nothing else
    assert 'href="/ds/tokens.css?v=' in landing.text and 'class="ds"' in landing.text
    # the public pages are light whatever the system's theme (Denis 02.11.2026)
    assert '<html lang="en" data-theme="light">' in landing.text and "prefers-color-scheme: dark" not in landing.text
    # every page names its files with their version, so a changed icon or stylesheet reaches browsers at once
    from server.hosted.site import VERSIONS
    assert f'src="/icon-512x512.png?v={VERSIONS["icon-512x512.png"]}"' in landing.text
    assert len(VERSIONS["icon-512x512.png"]) == 10
    versioned = hosted.get(f"/icon-512x512.png?v={VERSIONS['icon-512x512.png']}")
    assert versioned.status_code == 200 and versioned.headers["content-type"] == "image/png"
    assert "font-src 'self'" in landing.headers["content-security-policy"]
    error = hosted.get("/oauth/consent")
    assert "noindex" in error.text and "/favicon.svg" in error.text  # every page carries the icons, only / is indexed


def test_house_rules_for_agents(hosted):
    """House rules for agents (Denis 05.11.2026): a page of their own, linked from «Dear agent» and /llms.txt; the short
    version is in the server instructions every agent gets."""
    page = hosted.get("/agents")
    assert page.status_code == 200 and "noindex" not in page.text
    assert f'<link rel="canonical" href="{PUBLIC}/agents">' in page.text
    text = " ".join(page.text.split())  # as rendered
    assert "House rules for agents" in text
    for words in ("Check in first.", "Act on every answer, not only the newest:", "Answered elsewhere? Close it here.",
                  "Decisions go in asks, not in chat.", "Keep your tasks true.",
                  "A task with <code>reply_pending</code> waits for you:", "Never make them answer twice."):
        assert words in text, words
    assert 'href="/agents"' in hosted.get("/").text
    assert f"{PUBLIC}/agents" in hosted.get("/llms.txt").text
    from server.mcp_tools import HOSTED_INSTRUCTIONS, INSTRUCTIONS
    for instructions in (INSTRUCTIONS, HOSTED_INSTRUCTIONS):
        assert "ask_close it yourself" in " ".join(instructions.split())

def test_store_pages(hosted):
    """What App Store review and Google's OAuth verification look for: what the app does, how to connect, the privacy
    policy (Limited Use for drive.appdata, deletion), support contacts, all linked from every page."""
    pages = {path: hosted.get(path) for path in ("/", "/privacy", "/support")}
    for path, page in pages.items():
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html"), path
        assert "<script" not in page.text and "default-src 'none'" in page.headers["content-security-policy"], path
        assert f'<link rel="canonical" href="{PUBLIC}{path}">' in page.text, path
        for link in ('href="/privacy"', 'href="/support"', 'href="https://github.com/alloverart/askhooman"'):
            assert link in page.text, (path, link)
    landing, privacy, support = (" ".join(pages[p].text.split()) for p in ("/", "/privacy", "/support"))  # as rendered
    assert f"<code class=\"url\">{RESOURCE}</code>" in landing
    assert "Add custom connector" in landing and f"claude mcp add --scope user --transport http askhooman {RESOURCE}" in landing
    for words in ("In review at the App Store", "In review at Google Play", "Open the web app",
                  '<a href="https://app.askhooman.com">app.askhooman.com</a>',
                  "Apache-2.0", "drive.appdata",
                  "We keep only your account email, an encrypted Google token and your agents' names and keys"):
        assert words in landing, words
    for words in ("Effective 1 November 2026", "Allover Studio, LLC", "mailto:privacy@askhooman.com", "qtracker.test",
                  "will adhere to the <a href=\"https://developers.google.com/terms/api-services-user-data-policy\">"
                  "Google API Services User Data Policy</a>, including the Limited Use requirements",
                  "drive.appdata", "appDataFolder", "never copied to our database", "Google Cloud",
                  "Settings → Delete account", "revokes", "stay in your Google Drive", "Delete hidden app data",
                  "Third-party apps", "Children", "Changes", "no ads", "never sell"):
        assert words in privacy, words
    for words in ("mailto:support@askhooman.com", "mailto:privacy@askhooman.com", "mailto:security@askhooman.com",
                  f"claude mcp add --scope user --transport http askhooman {RESOURCE}", "Add custom connector",
                  f'href="{PUBLIC}/oauth/link"', "Settings → Delete account", "Delete hidden app data",
                  'href="/privacy"'):
        assert words in support, words
    # the old domain serves the same pages, pointing at the public URL
    alias = hosted.get("/privacy", headers={"Host": "old.qtracker.test"})
    assert alias.status_code == 200 and f'<link rel="canonical" href="{PUBLIC}/privacy">' in alias.text


# ---------- SPEC v5 step 2: the wake switch and the agent's wake-up fields ----------

def new_agent(hosted, google, name: str = "Allen") -> dict:
    made = hosted.post("/api/agents/keys", json={"name": name}, headers=id_token(google))
    assert made.status_code == 201
    return {**made.json()["agent"], "key": made.json()["key"]}


def test_agent_shape_has_wake_fields(hosted, google):
    agent = new_agent(hosted, google)
    listed = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]
    for shape in (agent, listed):
        assert shape["wake"] is True
        assert shape["webhook_set_by"] is None and shape["last_delivery"] is None and shape["last_listen"] is None


def test_patch_wake_off_and_on(hosted, google):
    agent = new_agent(hosted, google)
    off = hosted.patch(f"/api/agents/{agent['id']}", json={"wake": False}, headers=id_token(google))
    assert off.status_code == 200 and off.json()["agent"]["wake"] is False
    assert hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["wake"] is False
    on = hosted.patch(f"/api/agents/{agent['id']}", json={"wake": True}, headers=id_token(google))
    assert on.json()["agent"]["wake"] is True
    bad = hosted.patch(f"/api/agents/{agent['id']}", json={"wake": "no"}, headers=id_token(google))
    assert bad.status_code == 422


def test_webhook_set_from_app_is_set_by_user(hosted, google):
    agent = new_agent(hosted, google)
    set_hook = hosted.patch(f"/api/agents/{agent['id']}", json={"webhook_url": "https://hooks.example/a"},
                            headers=id_token(google))
    assert set_hook.json()["agent"]["webhook_set_by"] == "user"
    cleared = hosted.patch(f"/api/agents/{agent['id']}", json={"webhook_url": None}, headers=id_token(google))
    assert cleared.json()["agent"]["webhook_url"] is None and cleared.json()["agent"]["webhook_set_by"] is None


def test_revoke_and_delete_account_drop_the_feed(hosted, google):
    feed = hosted.app.state.feed
    first, second = new_agent(hosted, google, "Allen"), new_agent(hosted, google, "Ogilvy")
    for agent in (first, second):
        feed.publish(agent["id"], {"type": "ask.answered", "ask_id": 1, "event": {"id": 2, "at": "x"}})
        assert hosted.store.get("feeds", agent["id"]) is not None
    assert hosted.delete(f"/api/agents/{first['id']}", headers=id_token(google)).status_code == 200
    assert hosted.store.get("feeds", first["id"]) is None
    assert hosted.store.get("feeds", second["id"]) is not None
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert hosted.store.get("feeds", second["id"]) is None


def test_last_delivery_on_the_agent(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"], agent="Allen")
    agent_id = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["id"]
    hosted.patch(f"/api/agents/{agent_id}", json={"webhook_url": "https://hooks.example/qt"}, headers=id_token(google))
    ask = tool_result(mcp(hosted, tokens["access_token"], "ask_create", {"title": "Ship?"}))
    app_action(google.drive(DENIS_SUB), ask["id"], "answered", answer="Yes")
    google.webhook_statuses.extend([404])
    hosted.post("/api/events", json={"ask_id": ask["id"], "kind": "answered"}, headers=id_token(google))
    assert hosted.app.state.webhooks.flush(5)
    last = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["last_delivery"]
    assert last["ok"] is False and last["status"] == 404 and last["error"] == "HTTP 404" and last["at"]
    google.webhook_statuses.extend([200])
    hosted.post("/api/events", json={"ask_id": ask["id"], "kind": "answered"}, headers=id_token(google))
    assert hosted.app.state.webhooks.flush(5)
    last = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["last_delivery"]
    assert (last["ok"], last["status"], last["error"]) == (True, 200, None)
