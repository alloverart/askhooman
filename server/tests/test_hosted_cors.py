"""Hosted bridge: CORS for the web app (ASKS_CORS_ORIGINS), on the App API only."""

import pytest
from fastapi.testclient import TestClient

from server.hosted.config import ConfigError, HostedSettings

from .hosted_support import KEY, PUBLIC, build, connect, id_token, register

WEB = "https://app.qtracker.test"  # the web app
DEV = "http://localhost:5173"  # its development server

# the App API with the method a browser asks for in its preflight
APP_ROUTES = (("/api/agents", "GET"), ("/api/agents/ag_x", "PATCH"), ("/api/agents/ag_x", "DELETE"),
              ("/api/agents/keys", "POST"), ("/api/connect-info", "GET"), ("/api/events", "POST"),
              ("/api/drive/token", "POST"), ("/api/me", "DELETE"), ("/api/account", "GET"),
              ("/api/admin/stats", "GET"))


@pytest.fixture
def web(google):
    """The bridge with the web app's origins in ASKS_CORS_ORIGINS."""
    app, store = build(google, cors_origins=(WEB, DEV))
    with TestClient(app, base_url=PUBLIC) as client:
        client.store = store
        yield client


def preflight(client, path: str, method: str, origin: str = WEB):
    return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                         "Access-Control-Request-Headers": "authorization,content-type"})


def cors(response) -> dict:
    return {k: v for k, v in response.headers.items() if k.lower().startswith("access-control-")}


def test_preflight_from_the_web_app(web):
    """Answered before the token check (a preflight carries none) for every App API route, DELETE /api/me included."""
    for origin in (WEB, DEV):
        for path, method in APP_ROUTES:
            response = preflight(web, path, method, origin)
            assert response.status_code == 204, (origin, path, method, response.text)
            assert cors(response) == {  # no Allow-Credentials: the app sends its ID token as a bearer, no cookies
                "access-control-allow-origin": origin,  # the exact origin, never *
                "access-control-allow-methods": "GET, POST, PATCH, DELETE",
                "access-control-allow-headers": "Authorization, Content-Type",
                "access-control-max-age": "600",
            }
            assert response.headers["vary"] == "Origin" and response.content == b""


def test_responses_to_the_web_app_carry_cors_errors_included(web, google, monkeypatch):
    """The browser can read every App API answer, the errors too: 401 from RequestGuard, 404, 409, 422, 429, 503."""
    origin = {"Origin": WEB}
    me = {**origin, **id_token(google)}

    def status(response) -> int:
        assert cors(response) == {"access-control-allow-origin": WEB}, response.headers
        assert "Origin" in response.headers["vary"]
        return response.status_code

    assert status(web.get("/api/agents", headers=me)) == 200
    created = web.post("/api/agents/keys", json={"name": "Research Bot"}, headers=me)
    assert status(created) == 201 and created.json()["key"].startswith("qtk_")
    agent_id = created.json()["agent"]["id"]
    assert status(web.get("/api/connect-info", headers=me)) == 200
    assert status(web.get("/api/agents", headers=origin)) == 401
    assert status(web.get("/api/agents", headers={**origin, "Authorization": "Bearer not-a-jwt"})) == 401
    assert status(web.delete("/api/me", headers=origin)) == 401
    assert status(web.patch("/api/agents/ag_nope", json={"name": "X"}, headers=me)) == 404
    assert status(web.post("/api/agents/keys", json={"name": "Research Bot"}, headers=me)) == 409
    assert status(web.patch(f"/api/agents/{agent_id}", json={"slug": "x"}, headers=me)) == 422
    assert status(web.post("/api/events", json={"ask_id": 1, "kind": "edited"}, headers=me)) == 422
    assert status(web.post("/api/events", json={"ask_id": 1, "kind": "closed"}, headers=me)) == 503  # Drive not linked
    no_grant = web.post("/api/drive/token", headers=me)  # the app then falls back to Google's window
    assert status(no_grant) == 409 and no_grant.json()["error"] == "not_linked"
    connect(web, google, register(web)["client_id"])
    token = web.post("/api/drive/token", headers=me)
    assert status(token) == 200 and token.json()["access_token"] and token.headers["cache-control"] == "no-store"
    with monkeypatch.context() as limited:
        limited.setattr(web.app.state.limiter, "allow", lambda *args: False)
        assert status(web.get("/api/agents", headers=me)) == 429
    assert status(web.delete(f"/api/agents/{agent_id}", headers=me)) == 200
    assert status(web.delete("/api/me", headers=me)) == 204
    assert status(web.delete("/api/me", headers=me)) == 404


def test_other_origins_get_no_cors(web, google):
    for origin in ("https://evil.example", "null", f"{WEB}.evil.example", "http://app.qtracker.test",
                   "https://app.qtracker.test:8443", "http://localhost:3000", "HTTPS://APP.QTRACKER.TEST"):
        response = preflight(web, "/api/agents", "GET", origin)
        assert response.status_code == 401 and not cors(response), origin  # refused by the token check
        response = web.get("/api/agents", headers={"Origin": origin, **id_token(google)})
        assert response.status_code == 200 and not cors(response) and "vary" not in response.headers, origin
    native = web.get("/api/agents", headers=id_token(google))  # the iOS app sends no Origin
    assert native.status_code == 200 and not cors(native)


def test_no_cors_outside_the_app_api(web, google):
    """The agents' REST (GET /api/me included), /mcp and the OAuth endpoints stay closed, to the web app too."""
    for path, method in (("/api/asks", "GET"), ("/api/asks", "POST"), ("/api/asks/1/answer", "POST"),
                         ("/api/tasks", "GET"), ("/api/me", "GET"), ("/api/agents", "PUT"), ("/mcp", "POST"),
                         ("/token", "POST"), ("/register", "POST"), ("/revoke", "POST"), ("/authorize", "GET"),
                         ("/.well-known/oauth-authorization-server", "GET"), ("/healthz", "GET")):
        response = preflight(web, path, method)
        assert response.status_code in (401, 405) and not cors(response), (path, method, response.status_code)
    tokens = connect(web, google, register(web)["client_id"])
    agent = {"Origin": WEB, "Authorization": f"Bearer {tokens['access_token']}"}
    for path in ("/api/asks", "/api/tasks", "/api/me"):
        response = web.get(path, headers=agent)
        assert response.status_code == 200 and not cors(response), path
    for response in (web.get("/api/asks", headers={"Origin": WEB}),
                     web.get("/api/me", headers={"Origin": WEB, **id_token(google)}),  # GET /api/me: agents only
                     web.post("/mcp", json={}, headers={"Origin": WEB}),
                     web.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
                              headers={"Origin": WEB}),
                     web.get("/.well-known/oauth-protected-resource/mcp", headers={"Origin": WEB})):
        assert not cors(response), (response.request.url, response.status_code)


def test_cors_is_closed_by_default(hosted, google):
    """ASKS_CORS_ORIGINS empty (the default): no Access-Control-* anywhere, the App API included."""
    for path, method in APP_ROUTES:
        response = preflight(hosted, path, method)
        assert response.status_code == 401 and not cors(response), (path, method)
    response = hosted.get("/api/agents", headers={"Origin": WEB, **id_token(google)})
    assert response.status_code == 200 and not cors(response) and "vary" not in response.headers
    assert not cors(hosted.get("/api/agents", headers={"Origin": WEB}))


def test_cors_origins_setting():
    env = {"ASKS_GOOGLE_WEB_CLIENT": '{"client_id": "a", "client_secret": "b"}',
           "ASKS_GOOGLE_APP_CLIENT_IDS": "ios.apps", "ASKS_ENCRYPTION_KEY": KEY}
    assert HostedSettings.from_env(env).cors_origins == ()
    assert HostedSettings.from_env({**env, "ASKS_CORS_ORIGINS": " , "}).cors_origins == ()
    # canonical as a browser sends Origin (lower case, no default port, no trailing slash), deduplicated
    given = "HTTPS://App.Example.com:443/, http://localhost:5173, https://app.example.com, https://b.example.com:8443"
    assert HostedSettings.from_env({**env, "ASKS_CORS_ORIGINS": given}).cors_origins == (
        "https://app.example.com", "http://localhost:5173", "https://b.example.com:8443")
    for bad in ("*", "null", "https://*.example.com", "app.example.com", "http://app.example.com",
                "http://127.0.0.1:5173", "http://localhost", "https://app.example.com/web",
                "https://app.example.com/?x=1", "https://user@app.example.com", "https://[::1]:8443",
                "https://app.example.com:99999", "https://app.example.com, *"):
        with pytest.raises(ConfigError, match="ASKS_CORS_ORIGINS"):
            HostedSettings.from_env({**env, "ASKS_CORS_ORIGINS": bad})
