"""Hosted bridge: the operator's view (SPEC "Operator stats"): who signed up and uses the service, from what the
server already keeps (users, agents, id counters), and a ping on every new external sign-up."""

from datetime import datetime, timezone

import pytest

from server.hosted.config import ConfigError, HostedSettings
from server.hosted.operator import OperatorPing
from server.hosted.stats import admin_stats, internal_check

from .hosted_support import DENIS_EMAIL, DENIS_SUB, OLGA_EMAIL, OLGA_SUB, build, id_token, settings_for

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
ENV = {"ASKS_GOOGLE_WEB_CLIENT": '{"client_id": "w", "client_secret": "s"}', "ASKS_GOOGLE_APP_CLIENT_IDS": "ios",
       "ASKS_ENCRYPTION_KEY": "k"}


def user(sub, email, created, linked=True, asks=0, tasks=0):
    counters = {k: v for k, v in (("ask", asks), ("task", tasks)) if v}
    return {"sub": sub, "email": email, "created_at": created, "counters": counters,
            "google_refresh": "sealed" if linked else None}


def agent(aid, sub, seen=None, client="Claude Code", key=False, revoked=None, webhook=None):
    return {"id": aid, "user": sub, "name": aid, "slug": aid, "client_name": client,
            "clients": [{"client_id": "c", "client_name": client, "connected_at": "2026-11-01T00:00:00.000Z"}]
            if client else [], "key_prefix": "qtk_ab" if key else None, "last_seen": seen, "revoked_at": revoked,
            "webhook_url": webhook, "created_at": "2026-11-01T00:00:00.000Z"}


USERS = [
    user("1", "we@allover.art", "2026-10-20T09:00:00.000Z", asks=1031, tasks=1011),
    user("2", "ann@gmail.com", "2026-11-03T08:00:00.000Z", asks=2, tasks=1),
    user("3", "bob@corp.example", "2026-10-30T08:00:00.000Z", linked=False),
    user("4", "old@gmail.com", "2026-09-01T08:00:00.000Z"),
]
AGENTS = [
    agent("ag_1", "1", seen="2026-11-03T11:00:00.000Z"),
    agent("ag_2", "1", seen="2026-10-01T11:00:00.000Z", client=None, key=True),
    agent("ag_3", "2", seen="2026-11-03T10:00:00.000Z", client="claude.ai", webhook="https://hooks.example/x"),
    agent("ag_4", "2", seen="2026-11-03T10:30:00.000Z", revoked="2026-11-03T11:00:00.000Z"),
    agent("ag_5", "3", seen="2026-10-29T10:00:00.000Z", client="Claude Code"),
    agent("ag_6", "9", seen="2026-11-03T10:00:00.000Z"),  # its user deleted the account meanwhile
]
INTERNAL = internal_check(("we@allover.art",), ())


def test_internal_check_takes_emails_and_domains_case_insensitively():
    check = internal_check(("We@Allover.art",), ("@allover.art", "review@example.com"))
    assert check("we@allover.art") and check("ALLEN@allover.art") and check("Review@example.com")
    assert not check("ann@gmail.com") and not check("x@notallover.art") and not check(None) and not check("")


def test_stats_count_users_agents_and_activity_for_all_and_external():
    stats = admin_stats(USERS, AGENTS, NOW, INTERNAL)
    assert stats["generated_at"] == "2026-11-03T12:00:00.000Z"
    every, external = stats["all"], stats["external"]
    assert (every["users"], external["users"]) == (4, 3)
    assert external["new"] == {"1d": 1, "7d": 2, "30d": 2}
    assert every["new"] == {"1d": 1, "7d": 2, "30d": 3}
    # active = a live agent seen in the window; the revoked one and the orphan don't count
    assert every["active"] == {"1d": 2, "7d": 3, "30d": 3}
    assert external["active"] == {"1d": 1, "7d": 2, "30d": 2}
    assert (every["drive_linked"], external["drive_linked"]) == (3, 2)
    assert (every["agents"], every["revoked_agents"], every["webhooks"], every["keys"]) == (4, 1, 1, 1)
    assert every["agents_active"] == {"1d": 2, "7d": 3, "30d": 3}
    assert every["clients"] == {"Claude Code": 2, "claude.ai": 1, "Agent key": 1}
    assert external["clients"] == {"claude.ai": 1, "Claude Code": 1}
    assert (every["asks"], every["tasks"], external["asks"], external["tasks"]) == (1033, 1012, 2, 1)


def test_stats_rows_newest_first_with_no_secrets():
    rows = admin_stats(USERS, AGENTS, NOW, INTERNAL)["accounts"]
    assert [r["email"] for r in rows] == ["ann@gmail.com", "bob@corp.example", "we@allover.art", "old@gmail.com"]
    ann = rows[0]
    assert ann == {"email": "ann@gmail.com", "internal": False, "created_at": "2026-11-03T08:00:00.000Z",
                   "drive_linked": True, "agents": 1, "asks": 2, "tasks": 1, "last_seen": "2026-11-03T10:00:00.000Z",
                   "clients": ["claude.ai"]}
    assert rows[2]["internal"] is True and rows[2]["clients"] == ["Agent key", "Claude Code"]
    assert rows[3]["agents"] == 0 and rows[3]["last_seen"] is None and rows[3]["clients"] == []
    assert "sealed" not in repr(rows) and "google_refresh" not in repr(rows)


def test_stats_survive_odd_records():
    odd_users = [{"sub": "x"}, {"sub": "y", "email": "y@gmail.com", "created_at": "not a date", "counters": None}]
    odd_agents = [{"id": "a", "user": "y", "last_seen": "garbage"}, {"id": "b"}]
    stats = admin_stats(odd_users, odd_agents, NOW, INTERNAL)
    assert stats["all"]["users"] == 2 and stats["all"]["new"]["30d"] == 0 and stats["all"]["active"]["30d"] == 0
    assert stats["all"]["clients"] == {"Unknown": 1}


def test_settings_read_admins_internal_and_the_operator_ping():
    plain = HostedSettings.from_env(ENV)
    assert plain.admin_emails == () and plain.internal_emails == () and plain.operator_chat is None
    full = HostedSettings.from_env({**ENV, "ASKS_ADMIN_EMAILS": " We@Allover.art, ",
                                    "ASKS_INTERNAL_EMAILS": "@allover.art,review@example.com",
                                    "ASKS_OPERATOR_TELEGRAM_CHAT": "7398621",
                                    "ASKS_OPERATOR_TELEGRAM_TOKEN": "123:abc\n"})
    assert full.admin_emails == ("we@allover.art",)
    assert full.internal_emails == ("@allover.art", "review@example.com")
    assert (full.operator_chat, full.operator_token) == ("7398621", "123:abc")
    with pytest.raises(ConfigError):
        HostedSettings.from_env({**ENV, "ASKS_ADMIN_EMAILS": "not-an-email"})
    with pytest.raises(ConfigError):
        HostedSettings.from_env({**ENV, "ASKS_OPERATOR_TELEGRAM_CHAT": "7398621"})  # a chat without a token


def test_operator_ping_on_new_external_users_only():
    sent = []
    ping = OperatorPing(lambda chat, text: sent.append((chat, text)), "7398621", INTERNAL,
                        "https://app.askhooman.test", run=lambda fn: fn())
    ping.new_user({"email": "ann@gmail.com"})
    ping.new_user({"email": "we@allover.art"})
    assert sent == [("7398621", "🆕 AskHooman: a new user signed up\nhttps://app.askhooman.test/#/admin")]
    OperatorPing(None, None, INTERNAL, "https://x", run=lambda fn: fn()).new_user({"email": "b@gmail.com"})  # off


def test_operator_ping_failure_is_only_logged(caplog):
    def broken(chat, text):
        raise RuntimeError("telegram down")
    OperatorPing(broken, "1", INTERNAL, "https://x", run=lambda fn: fn()).new_user({"email": "ann@gmail.com"})
    assert "operator ping failed" in caplog.text and "ann@" not in caplog.text


@pytest.fixture
def admin_hosted(google):
    from fastapi.testclient import TestClient
    from .hosted_support import PUBLIC
    sent = []
    settings = settings_for()
    settings = HostedSettings(**{**settings.__dict__, "admin_emails": (DENIS_EMAIL,), "internal_emails": ()})
    app, store = build(google, settings=settings,
                       operator=OperatorPing(lambda c, t: sent.append(t), "1", internal_check((DENIS_EMAIL,), ()),
                                             "https://app.test", run=lambda fn: fn()))
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.google, client.sent = store, google, sent
        yield client


def test_admin_stats_endpoint_is_for_admins_only(admin_hosted, google):
    assert admin_hosted.get("/api/admin/stats").status_code == 401
    olga = admin_hosted.get("/api/admin/stats", headers=id_token(google, sub=OLGA_SUB, email=OLGA_EMAIL))
    assert olga.status_code == 403
    unverified = admin_hosted.get("/api/admin/stats", headers=id_token(google, email_verified=False))
    assert unverified.status_code == 403
    ok = admin_hosted.get("/api/admin/stats", headers=id_token(google))
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    body = ok.json()
    assert body["all"]["users"] == 2 and body["external"]["users"] == 1  # Olga signed up by asking above
    assert {r["email"] for r in body["accounts"]} == {DENIS_EMAIL, OLGA_EMAIL}


def test_account_says_who_is_signed_in_and_whether_they_are_an_admin(admin_hosted, google):
    me = admin_hosted.get("/api/account", headers=id_token(google)).json()
    assert me == {"email": DENIS_EMAIL, "admin": True}
    olga = admin_hosted.get("/api/account", headers=id_token(google, sub=OLGA_SUB, email=OLGA_EMAIL)).json()
    assert olga == {"email": OLGA_EMAIL, "admin": False}


def test_first_app_sign_in_of_an_external_user_pings_the_operator_once(admin_hosted, google):
    for _ in range(2):
        admin_hosted.get("/api/account", headers=id_token(google, sub=OLGA_SUB, email=OLGA_EMAIL))
    admin_hosted.get("/api/account", headers=id_token(google, sub=DENIS_SUB))  # internal: no ping
    assert admin_hosted.sent == ["🆕 AskHooman: a new user signed up\nhttps://app.test/#/admin"]


def test_an_agent_connecting_a_new_external_user_pings_the_operator(admin_hosted, google):
    from .hosted_support import connect, register
    client_id = register(admin_hosted)["client_id"]
    connect(admin_hosted, google, client_id, sub=OLGA_SUB, email=OLGA_EMAIL)
    connect(admin_hosted, google, client_id, sub=OLGA_SUB, email=OLGA_EMAIL, agent="Hamilton")
    assert admin_hosted.sent == ["🆕 AskHooman: a new user signed up\nhttps://app.test/#/admin"]
