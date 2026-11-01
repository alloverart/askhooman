"""Hosted bridge: the authorization server and the resource server's bearer check (SPEC v4, MCP auth 2026-07-28)."""

import base64
import re

import pytest
from fastapi.testclient import TestClient

from server.hosted.crypto import digest

from .hosted_support import (
    DENIS_EMAIL,
    DENIS_SUB,
    LOOPBACK,
    PUBLIC,
    RESOURCE,
    authorize,
    build,
    connect,
    consent,
    csrf_of,
    mcp,
    pkce,
    query,
    register,
    to_consent,
    tool_result,
)

CIMD_URL = "https://client.example/oauth/metadata.json"
CIMD_REDIRECT = "http://localhost:8765/callback"


def cimd_document(**overrides) -> dict:
    return {"client_id": CIMD_URL, "client_name": "Example Agent", "redirect_uris": [CIMD_REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", **overrides}


# ---------- discovery ----------

def test_metadata(hosted):
    resource = hosted.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json() == {"resource": RESOURCE, "authorization_servers": [PUBLIC],
                               "scopes_supported": ["qtracker"], "bearer_methods_supported": ["header"],
                               "resource_name": "AskHooman"}
    assert hosted.get("/.well-known/oauth-protected-resource").json() == resource.json()
    meta = hosted.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == PUBLIC  # exactly, no trailing slash (RFC 8414 / 9207 compare strings)
    assert meta["authorization_endpoint"] == f"{PUBLIC}/authorize" and meta["token_endpoint"] == f"{PUBLIC}/token"
    assert meta["registration_endpoint"] == f"{PUBLIC}/register" and meta["revocation_endpoint"] == f"{PUBLIC}/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["client_id_metadata_document_supported"] is True
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    # CORS stays closed
    assert "access-control-allow-origin" not in {k.lower() for k in resource.headers}
    assert hosted.options("/token", headers={"Origin": "https://evil.example",
                                             "Access-Control-Request-Method": "POST"}).status_code == 405


OLD = "https://old-name.test"  # the domain before a rename, kept as an alias


def test_an_alias_keeps_the_connections_made_on_it(google):
    """After a rename the old domain stays an alias (ASKS_PUBLIC_URL_ALIASES): requests that arrive on it get its own
    metadata, issuer and token audience, so agents connected there keep working. A token is good only on the origin
    it was issued for, and other hosts are answered as the public URL."""
    google.redirect_uris.add(f"{OLD}/oauth/google/callback")
    app, _ = build(google, aliases=(OLD,))
    with TestClient(app, base_url=PUBLIC) as new:  # runs the app's lifespan once; the others share it
        old, other = TestClient(app, base_url=OLD), TestClient(app, base_url="https://elsewhere.test")
        meta = old.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == f"{OLD}/mcp" and meta["authorization_servers"] == [OLD]
        assert old.get("/.well-known/oauth-authorization-server").json()["token_endpoint"] == f"{OLD}/token"
        assert new.get("/.well-known/oauth-authorization-server").json()["issuer"] == PUBLIC
        assert other.get("/.well-known/oauth-authorization-server").json()["issuer"] == PUBLIC
        assert f'resource_metadata="{OLD}/.well-known' in old.post("/mcp", json={}).headers["www-authenticate"]
        # the old domain only issues tokens for itself
        assert "unknown resource" in authorize(old, register(old)["client_id"], pkce()[1]).text
        legacy = register(old)
        tokens = connect(old, google, legacy["client_id"], origin=OLD)  # Google returned to the old domain
        assert google.auth_requests[-1]["redirect_uri"] == f"{OLD}/oauth/google/callback"
        ask = tool_result(mcp(old, tokens["access_token"], "ask_create", {"title": "Still connected?"}))
        assert ask["agent"] == "allen"
        assert mcp(new, tokens["access_token"], "asks_mine").status_code == 401  # audience: the old domain
        refreshed = old.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                             "client_id": legacy["client_id"]})
        assert refreshed.status_code == 200, refreshed.text
        mine = tool_result(mcp(old, refreshed.json()["access_token"], "asks_mine"))
        assert [a["id"] for a in mine["asks"]] == [ask["id"]]
        # a new connection on the public URL works there, and only there
        fresh = connect(new, google, register(new)["client_id"], agent="Ogilvy")
        assert tool_result(mcp(new, fresh["access_token"], "asks_mine"))["asks"] == []
        assert mcp(old, fresh["access_token"], "asks_mine").status_code == 401


def test_401_points_to_resource_metadata(hosted):
    response = hosted.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="qtracker"' in challenge and "error=" not in challenge
    bad = mcp(hosted, "qta_not-a-real-token", "asks_mine")
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]
    assert "resource_metadata=" in bad.headers["www-authenticate"]
    rest = hosted.get("/api/asks", headers={"Authorization": "Bearer qta_nope"})
    assert rest.status_code == 401 and "resource_metadata=" in rest.headers["www-authenticate"]


# ---------- happy paths ----------

def test_dcr_flow_then_mcp_creates_ask_in_users_drive(hosted, google):
    client = register(hosted)
    assert client["client_id"].startswith("qtc_") and "client_secret" not in client
    tokens = connect(hosted, google, client["client_id"], agent="Allen")
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["scope"] == "qtracker"
    assert tokens["access_token"].startswith("qta_") and tokens["refresh_token"].startswith("qtr_")

    ask = tool_result(mcp(hosted, tokens["access_token"], "ask_create",
                          {"title": "Ship the pricing page?", "options": ["Yes", "Wait"]}))
    assert (ask["id"], ask["agent"], ask["status"]) == (1, "allen", "open")
    doc = google.drive(DENIS_SUB).doc(1)  # the user's own Drive appDataFolder
    assert (doc["agent"], doc["title"], doc["writer"], doc["rev"], doc["schema"]) == (
        "allen", "Ship the pricing page?", "bridge", 1, 1)
    assert [e["kind"] for e in doc["events"]] == ["created"]
    assert google.drive(DENIS_SUB).file("ask-1.json")["appProperties"] == {
        "ask_id": "1", "status": "open", "agent": "allen", "rev": "1"}
    mine = tool_result(mcp(hosted, tokens["access_token"], "asks_mine"))
    assert [a["id"] for a in mine["asks"]] == [1] and mine["counts"] == {"open": 1, "answered": 0, "closed": 0}
    # the agents' REST works with the same token
    me = hosted.get("/api/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json() == {"role": "agent", "agent": "allen"}
    # Google's refresh token is stored encrypted, never in clear
    user = hosted.store.get("users", DENIS_SUB)
    assert user["email"] == DENIS_EMAIL and user["google_refresh"].startswith("v1.k1.")
    assert not any(t in str(hosted.store.all("users")) for t in google.refresh_tokens)
    # only digests of our tokens are stored
    stored = str(hosted.store.all("tokens"))
    assert tokens["access_token"] not in stored and tokens["refresh_token"] not in stored
    assert digest(tokens["access_token"]) in hosted.store.all("tokens")


def test_cimd_client_flow(hosted, google):
    google.publish(CIMD_URL, cimd_document(), headers={"Cache-Control": "max-age=600"})
    verifier, challenge = pkce()
    first = authorize(hosted, CIMD_URL, challenge, CIMD_REDIRECT.replace("8765", "51111"))  # loopback: any port
    consent_url = to_consent(hosted, google, first)
    page = hosted.get(consent_url).text
    assert "Example Agent" in page and "client.example" in page  # the metadata host is shown
    assert "runs on your own computer" in page and "localhost" in page  # localhost-only redirect warning
    response = hosted.post(consent_url, data={"csrf": csrf_of(page), "agent_name": "Example", "action": "allow"},
                           follow_redirects=False)
    params = query(response.headers["location"])
    assert response.headers["location"].startswith("http://localhost:51111/callback?")
    assert params["iss"] == PUBLIC and params["state"] == "st-1"
    tokens = hosted.post("/token", data={"grant_type": "authorization_code", "code": params["code"],
                                         "redirect_uri": "http://localhost:51111/callback", "client_id": CIMD_URL,
                                         "code_verifier": verifier, "resource": RESOURCE})
    assert tokens.status_code == 200, tokens.text
    ask = tool_result(mcp(hosted, tokens.json()["access_token"], "task_create", {"title": "Index the docs"}))
    assert ask["agent"] == "example"
    agent = hosted.store.all("agents")
    assert [a["client_id"] for a in agent.values()] == [CIMD_URL]


def test_a_shared_cimd_client_never_prefills_or_takes_over_an_agent(hosted, google):
    """Every Claude Code is one CIMD client_id: connecting the second agent from it must not prefill the first
    one's name, and typing that name needs "Connect as existing agent" (a regression once prefilled the previous
    agent's name)."""
    google.publish(CIMD_URL, cimd_document(), headers={"Cache-Control": "max-age=600"})
    connect(hosted, google, CIMD_URL, CIMD_REDIRECT, agent="Allen")
    consent_url = to_consent(hosted, google, authorize(hosted, CIMD_URL, pkce()[1], CIMD_REDIRECT))
    page = hosted.get(consent_url).text
    assert "Already connected: Allen" in page and 'value="Allen"' not in page
    assert 'value="" maxlength="40" required' in page and 'placeholder="My favorite agent"' in page
    typed = hosted.post(consent_url, data={"csrf": csrf_of(page), "agent_name": "Allen", "action": "allow"},
                        follow_redirects=False)
    assert typed.status_code == 200 and "Connect as existing agent Allen" in typed.text
    ogilvy = connect(hosted, google, CIMD_URL, CIMD_REDIRECT, agent="Ogilvy")
    ask = tool_result(mcp(hosted, ogilvy["access_token"], "ask_create", {"title": "Thanksgiving prints?"}))
    assert ask["agent"] == "ogilvy"


def test_reconnecting_with_the_same_name_is_the_same_agent(hosted, google):
    client = register(hosted)
    first = connect(hosted, google, client["client_id"], agent="Allen")
    ask = tool_result(mcp(hosted, first["access_token"], "ask_create", {"title": "Mine?"}))
    consent_url = to_consent(hosted, google, authorize(hosted, client["client_id"], pkce()[1]))
    page = hosted.get(consent_url).text
    assert "Already connected: Allen" in page and 'value="Allen"' in page  # the same client: prefilled as before
    second = connect(hosted, google, client["client_id"], agent="allen")
    closed = tool_result(mcp(hosted, second["access_token"], "ask_close", {"id": ask["id"], "resolution": "done"}))
    assert closed["closed_by"] == "allen"
    assert len([a for a in hosted.store.all("agents").values() if not a["revoked_at"]]) == 1


# ---------- authorization request errors ----------

def test_unregistered_redirect_is_never_followed(hosted):
    client = register(hosted)
    for uri in ("https://evil.example/cb", "http://127.0.0.1:33418/other", "http://127.0.0.2:33418/callback"):
        response = authorize(hosted, client["client_id"], pkce()[1], uri)
        assert response.status_code == 400 and "location" not in response.headers
        assert "not registered" in response.text
    unknown = authorize(hosted, "qtc_unknown", pkce()[1])
    assert unknown.status_code == 400 and "location" not in unknown.headers


@pytest.mark.parametrize("change, problem", [
    ({"resource": None}, "resource parameter is required"),
    ({"resource": "https://other.example/mcp"}, "unknown resource"),
    ({"code_challenge_method": "plain"}, "S256"),
    ({"code_challenge": "short"}, "code_challenge"),
    ({"response_type": "token"}, "response_type"),
])
def test_bad_authorization_requests_show_a_page_and_never_redirect(hosted, change, problem):
    """Redirect URIs of DCR/CIMD clients are untrusted: before the user acted, problems stay on our page."""
    client = register(hosted)
    args = {"challenge": pkce()[1]}
    extra = {}
    for key, value in change.items():
        if key == "resource":
            args["resource"] = value
        elif key == "code_challenge":
            args["challenge"] = value
        else:
            extra[key] = value
    response = authorize(hosted, client["client_id"], **args, **extra)
    assert response.status_code == 400 and "location" not in response.headers
    assert problem in response.text and "invalid sign-in request" in response.text


def test_resource_is_canonicalised(hosted, google):
    client = register(hosted)
    response = authorize(hosted, client["client_id"], pkce()[1], resource="HTTPS://QTRACKER.TEST:443/mcp/")
    assert response.status_code == 302 and response.headers["location"].startswith("https://accounts.google.com/")


def back_link(html: str) -> str:
    return re.search(r'<a href="([^"]+)" rel="noreferrer">Return to', html).group(1).replace("&amp;", "&")


def test_google_cancel_and_missing_drive_scope(hosted, google):
    client = register(hosted)
    response = authorize(hosted, client["client_id"], pkce()[1])
    callback = google.sign_in(response.headers["location"], DENIS_SUB, DENIS_EMAIL, deny=True)
    denied = hosted.get(callback, follow_redirects=False)
    # our page with a link back (the user decides), not an automatic redirect to the client's URI
    assert denied.status_code == 400 and "location" not in denied.headers and "cancelled" in denied.text
    params = query(back_link(denied.text))
    assert back_link(denied.text).startswith(LOOPBACK + "?")
    assert params["error"] == "access_denied" and params["state"] == "st-1" and params["iss"] == PUBLIC
    # Drive box unticked: Google is asked once more with its consent screen, then the page explains
    response = authorize(hosted, client["client_id"], pkce()[1])
    callback = google.sign_in(response.headers["location"], DENIS_SUB, DENIS_EMAIL, grant_drive=False)
    again = hosted.get(callback, follow_redirects=False)
    assert again.status_code == 302 and "prompt=consent" in again.headers["location"]
    assert "login_hint=owner%40example.com" in again.headers["location"]
    callback = google.sign_in(again.headers["location"], DENIS_SUB, DENIS_EMAIL, grant_drive=False)
    final = hosted.get(callback, follow_redirects=False)
    assert final.status_code == 400 and "location" not in final.headers and "Drive" in final.text
    params = query(back_link(final.text))
    assert params["error"] == "access_denied" and params["iss"] == PUBLIC


def test_no_refresh_token_from_google_forces_consent_once(hosted, google):
    client = register(hosted)
    response = authorize(hosted, client["client_id"], pkce()[1])
    callback = google.sign_in(response.headers["location"], DENIS_SUB, DENIS_EMAIL, give_refresh=False)
    again = hosted.get(callback, follow_redirects=False)
    assert again.status_code == 302 and "prompt=consent" in again.headers["location"]
    callback = google.sign_in(again.headers["location"], DENIS_SUB, DENIS_EMAIL)
    assert hosted.get(callback, follow_redirects=False).status_code == 303
    assert hosted.store.get("users", DENIS_SUB)["google_refresh"]


def test_state_is_bound_to_the_browser(hosted, google):
    client = register(hosted)
    response = authorize(hosted, client["client_id"], pkce()[1])
    callback = google.sign_in(response.headers["location"], DENIS_SUB, DENIS_EMAIL)
    hosted.cookies.clear()  # the callback arrives in a browser that didn't start the flow (login CSRF)
    stolen = hosted.get(callback, follow_redirects=False)
    assert stolen.status_code == 400 and "another browser" in stolen.text
    assert not hosted.store.all("users")


def test_consent_csrf_cancel_and_single_use(hosted, google):
    client = register(hosted)
    consent_url = to_consent(hosted, google, authorize(hosted, client["client_id"], pkce()[1]))
    page = hosted.get(consent_url)
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers[
        "content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    forged = hosted.post(consent_url, data={"csrf": "forged", "agent_name": "Evil", "action": "allow"},
                         follow_redirects=False)
    assert forged.status_code == 400 and "location" not in forged.headers
    empty = hosted.post(consent_url, data={"csrf": csrf_of(page.text), "agent_name": "  ", "action": "allow"})
    assert empty.status_code == 200 and "agent name is required" in empty.text
    cancel = consent(hosted, consent_url, action="cancel")
    params = query(cancel.headers["location"])
    assert params["error"] == "access_denied" and params["iss"] == PUBLIC and params["state"] == "st-1"
    again = hosted.post(consent_url, data={"csrf": csrf_of(page.text), "agent_name": "Allen", "action": "allow"},
                        follow_redirects=False)
    assert again.status_code == 400  # answered already
    assert not hosted.store.all("agents")


def test_consent_page_escapes_client_name(hosted, google):
    client = register(hosted, client_name='<script>alert(1)</script>"x')
    consent_url = to_consent(hosted, google, authorize(hosted, client["client_id"], pkce()[1]))
    page = hosted.get(consent_url).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "not verified" in page  # a self-registered name is not vouched for


# ---------- token endpoint ----------

def _code(hosted, google, client_id, redirect_uri=LOOPBACK):
    verifier, challenge = pkce()
    consent_url = to_consent(hosted, google, authorize(hosted, client_id, challenge, redirect_uri))
    return verifier, query(consent(hosted, consent_url).headers["location"])["code"]


def test_code_exchange_checks(hosted, google):
    client = register(hosted)
    cid = client["client_id"]
    verifier, code = _code(hosted, google, cid)
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": LOOPBACK, "client_id": cid,
            "code_verifier": verifier, "resource": RESOURCE}
    other = register(hosted)
    for change, error in [({"client_id": other["client_id"]}, "invalid_grant"),
                          ({"code_verifier": "x" * 43}, "invalid_grant")]:
        response = hosted.post("/token", data={**base, **change})
        assert response.status_code == 400 and response.json()["error"] == error
        assert response.headers["cache-control"] == "no-store"
    # the failed PKCE attempt burned the code: the right verifier can't use it any more
    assert hosted.post("/token", data=base).json()["error"] == "invalid_grant"

    verifier, code = _code(hosted, google, cid)
    wrong_resource = hosted.post("/token", data={**base, "code": code, "code_verifier": verifier,
                                                 "resource": "https://other.example/mcp"})
    assert wrong_resource.json()["error"] == "invalid_target"
    verifier, code = _code(hosted, google, cid)
    wrong_redirect = hosted.post("/token", data={**base, "code": code, "code_verifier": verifier,
                                                 "redirect_uri": "http://127.0.0.1:9/callback"})
    assert wrong_redirect.json()["error"] == "invalid_grant"
    json_body = hosted.post("/token", json=base)
    assert json_body.status_code == 400 and json_body.json()["error"] == "invalid_request"
    assert hosted.post("/token", data={**base, "grant_type": "password"}).json()["error"] == "unsupported_grant_type"


def test_code_replay_revokes_its_tokens(hosted, google):
    client = register(hosted)
    verifier, code = _code(hosted, google, client["client_id"])
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": LOOPBACK,
            "client_id": client["client_id"], "code_verifier": verifier, "resource": RESOURCE}
    tokens = hosted.post("/token", data=form).json()
    assert tool_result(mcp(hosted, tokens["access_token"], "asks_mine"))["asks"] == []
    replay = hosted.post("/token", data=form)
    assert replay.json()["error"] == "invalid_grant"
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 401


def test_refresh_rotation_and_reuse_detection(hosted, google):
    client = register(hosted)
    tokens = connect(hosted, google, client["client_id"])
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"], "resource": RESOURCE}
    second = hosted.post("/token", data=form).json()
    assert second["refresh_token"] != tokens["refresh_token"] and second["access_token"] != tokens["access_token"]
    assert tool_result(mcp(hosted, second["access_token"], "tasks_mine"))["tasks"] == []
    third = hosted.post("/token", data={**form, "refresh_token": second["refresh_token"]}).json()
    assert "access_token" in third
    # an old refresh token comes back (stolen?): the whole family dies, including the newest tokens
    reuse = hosted.post("/token", data=form)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert mcp(hosted, third["access_token"], "asks_mine").status_code == 401
    newest = hosted.post("/token", data={**form, "refresh_token": third["refresh_token"]})
    assert newest.json()["error"] == "invalid_grant"
    # widening the scope or another resource is refused
    fresh = connect(hosted, google, client["client_id"])
    wider = hosted.post("/token", data={**form, "refresh_token": fresh["refresh_token"], "scope": "qtracker admin"})
    assert wider.json()["error"] == "invalid_scope"


def test_revoke(hosted, google):
    client = register(hosted)
    tokens = connect(hosted, google, client["client_id"])
    assert hosted.post("/revoke", data={"token": tokens["refresh_token"], "client_id": "qtc_other"}).status_code == 401
    response = hosted.post("/revoke", data={"token": tokens["refresh_token"], "client_id": client["client_id"]})
    assert response.status_code == 200
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 401
    assert hosted.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                       "client_id": client["client_id"]}).json()["error"] == "invalid_grant"
    unknown = hosted.post("/revoke", data={"token": "qta_unknown", "client_id": client["client_id"]})
    assert unknown.status_code == 200  # RFC 7009: unknown tokens are not an error


def test_token_for_another_audience_is_rejected(hosted, google):
    client = register(hosted)
    tokens = connect(hosted, google, client["client_id"])
    record = hosted.store.get("tokens", digest(tokens["access_token"]))
    hosted.store.put("tokens", digest(tokens["access_token"]), {**record, "resource": "https://other.example/mcp"})
    response = mcp(hosted, tokens["access_token"], "asks_mine")
    assert response.status_code == 401 and 'error="invalid_token"' in response.headers["www-authenticate"]
    # an expired one too
    hosted.store.put("tokens", digest(tokens["access_token"]), {**record, "expires_at": 1})
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 401


def test_confidential_client_secret(hosted, google):
    client = register(hosted, token_endpoint_auth_method="client_secret_basic")
    secret = client["client_secret"]
    assert client["client_secret_expires_at"] == 0
    assert secret not in str(hosted.store.all("clients"))  # stored as a digest
    verifier, code = _code(hosted, google, client["client_id"])
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": LOOPBACK,
            "code_verifier": verifier, "resource": RESOURCE}
    bad = hosted.post("/token", data=form, auth=(client["client_id"], "wrong"))
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_client"
    assert bad.headers["www-authenticate"].startswith("Basic")
    basic = base64.b64encode(f"{client['client_id']}:{secret}".encode()).decode()
    good = hosted.post("/token", data=form, headers={"Authorization": f"Basic {basic}"})
    assert good.status_code == 200, good.text


# ---------- registration ----------

@pytest.mark.parametrize("body, error", [
    ({"redirect_uris": ["http://example.com/cb"]}, "invalid_redirect_uri"),  # http only on loopback
    ({"redirect_uris": ["cursor://anysphere/callback"]}, "invalid_redirect_uri"),  # no custom schemes
    ({"redirect_uris": ["https://app.example/cb#frag"]}, "invalid_redirect_uri"),
    ({"redirect_uris": []}, "invalid_redirect_uri"),
    ({"redirect_uris": ["https://app.example/cb"], "grant_types": ["client_credentials"]}, "invalid_client_metadata"),
    ({"redirect_uris": ["https://app.example/cb"], "token_endpoint_auth_method": "private_key_jwt"},
     "invalid_client_metadata"),
])
def test_registration_rules(hosted, body, error):
    response = hosted.post("/register", json={"client_name": "x", **body})
    assert response.status_code == 400 and response.json()["error"] == error


def test_registration_rate_limit(hosted):
    statuses = [hosted.post("/register", json={"redirect_uris": [LOOPBACK]}).status_code for _ in range(12)]
    assert statuses[:10] == [201] * 10 and statuses[10:] == [429, 429]
