"""Hosted bridge: SSRF-safe fetching, client metadata documents, redirect URI rules, secrets at rest, logs."""

import logging
import socket

import httpcore
import httpx
import pytest

from server.hosted.clients import (
    CimdResolver,
    ClientError,
    check_cimd_id,
    check_redirect_uri,
    parse_cimd,
    redirect_matches,
)
from server.hosted.config import ConfigError, HostedSettings, public_url
from server.hosted.crypto import CryptoError, Keyring
from server.hosted.ratelimit import RateLimiter, client_ip
from server.hosted.safe_http import PublicOnlyBackend, UnsafeURL, check_url, is_public_address

from .hosted_support import (
    KEY,
    connect,
    id_token,
    mcp,
    register,
    tool_result,
)

CIMD_URL = "https://client.example/oauth/metadata.json"


# ---------- SSRF ----------

@pytest.mark.parametrize("address, public", [
    ("8.8.8.8", True), ("2606:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("::1", False),
    ("fc00::1", False), ("fe80::1", False), ("::ffff:10.0.0.1", False), ("224.0.0.1", False), ("not-ip", False),
])
def test_public_addresses(address, public):
    assert is_public_address(address) is public


@pytest.mark.parametrize("url", [
    "http://example.com/x", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://[::1]/x", "https://localhost/x",
    "https://user:pw@example.com/x", "https://metadata.google.internal/x", "https://printer.local/x",
    "ftp://example.com/x", "https://", "https://example.com:0/x", "https://example.com:8443/x",
])
def test_unsafe_urls(url):
    with pytest.raises(UnsafeURL):
        check_url(url)


def test_connect_time_check_blocks_private_resolution():
    """DNS that answers with an internal address is refused at connect time (no rebinding window)."""
    backend = PublicOnlyBackend(resolve=lambda host, port, type: [(socket.AF_INET, type, 6, "", ("10.0.0.7", port))])
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("innocent.example", 443)
    backend = PublicOnlyBackend(resolve=lambda host, port, type: [
        (socket.AF_INET, type, 6, "", ("93.184.216.34", port)), (socket.AF_INET, type, 6, "", ("127.0.0.1", port))])
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("mixed.example", 443)


# ---------- client metadata documents ----------

def doc(**overrides) -> dict:
    return {"client_id": CIMD_URL, "client_name": "Example", "redirect_uris": ["http://127.0.0.1:1234/cb"],
            **overrides}


@pytest.mark.parametrize("client_id", [
    "https://client.example", "https://client.example/", "https://client.example/a/../b.json",
    "https://client.example/./b.json", "https://client.example/x.json#frag", "http://client.example/x.json",
    "https://10.0.0.1/x.json",
])
def test_cimd_id_rules(client_id):
    with pytest.raises(ClientError):
        check_cimd_id(client_id)


@pytest.mark.parametrize("overrides", [
    {"client_id": "https://client.example/other.json"},
    {"client_name": ""},
    {"redirect_uris": []},
    {"redirect_uris": ["http://evil.example/cb"]},
    {"client_secret": "s"},
    {"token_endpoint_auth_method": "client_secret_post"},
    {"token_endpoint_auth_method": "private_key_jwt"},
    {"grant_types": ["implicit"]},
])
def test_cimd_document_rules(overrides):
    import json
    with pytest.raises(ClientError):
        parse_cimd(CIMD_URL, json.dumps(doc(**overrides)).encode())


def test_cimd_fetch_limits_and_cache(google):
    now = [0.0]
    resolver = CimdResolver(httpx.Client(transport=google.public_transport()), clock=lambda: now[0])

    def later():  # a failure is answered from memory for 60 s
        now[0] += 61

    google.publish(CIMD_URL, b'{"client_id": "' + CIMD_URL.encode() + b'", "pad": "' + b"x" * 6000 + b'"}')
    with pytest.raises(ClientError, match="larger"):
        resolver.resolve(CIMD_URL)
    google.publish(CIMD_URL, doc())
    with pytest.raises(ClientError, match="larger"):  # the failure is cached: no second fetch for 60 s
        resolver.resolve(CIMD_URL)
    later()
    google.publish(CIMD_URL, doc(), headers={"Content-Type": "text/html"})
    with pytest.raises(ClientError, match="JSON"):
        resolver.resolve(CIMD_URL)
    later()
    google.publish(CIMD_URL, doc(), status=302, headers={"Location": "http://10.0.0.1/"})
    with pytest.raises(ClientError, match="HTTP 302"):  # redirects are not followed
        resolver.resolve(CIMD_URL)
    later()
    google.publish(CIMD_URL, doc(), headers={"Cache-Control": "max-age=600"})
    assert resolver.resolve(CIMD_URL)["client_name"] == "Example"
    google.publish(CIMD_URL, doc(client_name="Changed"))
    assert resolver.resolve(CIMD_URL)["client_name"] == "Example"  # cached
    now[0] += 10**6
    assert resolver.resolve(CIMD_URL)["client_name"] == "Changed"


# ---------- redirect URIs ----------

def test_redirect_rules():
    assert check_redirect_uri("https://claude.ai/api/mcp/auth_callback")
    assert check_redirect_uri("http://localhost:6274/oauth/callback")
    assert check_redirect_uri("http://[::1]/cb")
    for bad in ("http://example.com/cb", "myapp://cb", "https://x.example/cb#f", "javascript:alert(1)", "https:///cb"):
        with pytest.raises(ClientError):
            check_redirect_uri(bad)
    assert redirect_matches("http://127.0.0.1:1/cb", "http://127.0.0.1:55555/cb")
    assert redirect_matches("http://[::1]/cb", "http://[::1]:8080/cb")
    assert not redirect_matches("http://127.0.0.1:1/cb", "http://127.0.0.1:2/other")
    assert not redirect_matches("http://127.0.0.1:1/cb", "http://localhost:1/cb")
    assert not redirect_matches("https://app.example/cb", "https://app.example:444/cb")
    assert not redirect_matches("https://app.example/cb", "https://app.example/cb?x=1")


# ---------- secrets ----------

def test_keyring():
    keyring = Keyring(KEY)
    sealed = keyring.encrypt("1//refresh", "google-refresh:1001")
    assert "1//refresh" not in sealed and keyring.decrypt(sealed, "google-refresh:1001") == "1//refresh"
    with pytest.raises(CryptoError):
        keyring.decrypt(sealed, "google-refresh:2002")  # bound to its owner
    rotated = Keyring("k2:" + "a" * 43 + "=," + "k1:" + KEY)
    assert rotated.decrypt(sealed, "google-refresh:1001") == "1//refresh"
    assert rotated.encrypt("x", "c").startswith("v1.k2.")
    with pytest.raises(CryptoError):
        Keyring("c2hvcnQ=")


def test_settings_from_env(tmp_dir):
    client = tmp_dir / "web.json"
    client.write_text('{"web": {"client_id": "id.apps", "client_secret": "s"}}')
    env = {"ASKS_PUBLIC_URL": "https://qtracker.allover.art/", "ASKS_GOOGLE_WEB_CLIENT": str(client),
           "ASKS_GOOGLE_APP_CLIENT_IDS": "ios.apps", "ASKS_ENCRYPTION_KEY": KEY}
    settings = HostedSettings.from_env(env)
    assert settings.public_url == "https://qtracker.allover.art" and settings.web_client_id == "id.apps"
    assert settings.resource == "https://qtracker.allover.art/mcp" and settings.store == "firestore"
    inline = HostedSettings.from_env({**env, "ASKS_GOOGLE_WEB_CLIENT": '{"client_id": "a", "client_secret": "b"}'})
    assert inline.web_client_secret == "b"
    for bad in ("http://qtracker.allover.art", "https://x.example/path", ""):
        with pytest.raises(ConfigError):
            public_url(bad)
    with pytest.raises(ConfigError):
        HostedSettings.from_env({**env, "ASKS_GOOGLE_APP_CLIENT_IDS": ""})


def test_rate_limiter_and_client_ip():
    now = [0.0]
    limiter = RateLimiter({"x": (2, 60.0)}, clock=lambda: now[0])
    assert [limiter.allow("x", "a") for _ in range(3)] == [True, True, False]
    assert limiter.allow("x", "b")
    now[0] = 61.0
    assert limiter.allow("x", "a")
    assert client_ip({"x-forwarded-for": "6.6.6.6, 1.2.3.4"}, "10.0.0.1", True) == "1.2.3.4"
    assert client_ip({"x-forwarded-for": "6.6.6.6"}, "10.0.0.1", False) == "10.0.0.1"


def test_no_secrets_in_server_logs(hosted, google, caplog):
    caplog.set_level(logging.DEBUG)
    client = register(hosted, token_endpoint_auth_method="client_secret_post")
    tokens = connect(hosted, google, client["client_id"], secret=client["client_secret"])
    tool_result(mcp(hosted, tokens["access_token"], "ask_create", {"title": "Logged?"}))
    key = hosted.post("/api/agents/keys", json={"name": "K"}, headers=id_token(google)).json()["key"]
    hosted.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                "client_id": client["client_id"], "client_secret": client["client_secret"]})
    hosted.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                "client_id": client["client_id"], "client_secret": client["client_secret"]})
    server_lines = [r.getMessage() for r in caplog.records if r.name.startswith("asks")]
    assert server_lines  # the server did log
    secrets = [tokens["access_token"], tokens["refresh_token"], client["client_secret"], key,
               *google.refresh_tokens, *google.access_tokens]
    for line in server_lines:
        for secret in secrets:
            assert secret not in line, line


def test_store_outage_is_a_json_503(hosted, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(hosted.app.state.accounts, "identify", broken)
    response = mcp(hosted, "qta_whatever", "asks_mine")
    assert response.status_code == 503 and response.json() == {"detail": "temporarily unavailable, try again"}
    assert hosted.get("/api/asks", headers={"Authorization": "Bearer qta_x"}).status_code == 503


def test_agent_slugs():
    from server.hosted.accounts import _CYRILLIC, slugify
    assert len(_CYRILLIC) == 38
    assert slugify("Sonya Allen") == "sonya-allen"
    assert slugify("Соня Фейнман") == "sonya-feynman"
    assert slugify("Claude Code (work)") == "claude-code-work"
    assert slugify("x" * 50) == "x" * 32
    assert slugify("日本").startswith("agent-") and slugify("日本") != slugify("中文") == slugify("中文")
    assert slugify("Denis") == "denis-agent"  # "denis" is the human's actor name in the files
