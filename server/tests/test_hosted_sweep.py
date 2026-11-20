"""Hosted bridge: the hourly sweep of answer files (SPEC 7.6): POST /internal/sweep with a Cloud Scheduler OIDC token,
the four rules with their exact ages, the ask written under the guards, the claims of closed asks, slices with a
cursor, and a user whose Drive grant is gone."""

import dataclasses
import json
import logging
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from server.hosted import sweep as sweep_module
from server.hosted.files import SECRET_ORPHAN, SECRET_TTL, FILE_ORPHAN, claim_key
from server.hosted.sweep import NORMAL_TTL, sweep

from .fake_drive import app_action
from .hosted_support import DENIS_SUB, OLGA_EMAIL, OLGA_SUB, PUBLIC, build, mcp, settings_for, tool_result
from .test_hosted_files import answered_by_files, item
from .test_hosted_voice import agent

SA = "bridge@askhooman-test.iam.gserviceaccount.com"
HOUR, DAY = 3600, 86400
URL = "/internal/sweep"


@pytest.fixture
def hosted(google):
    app, store = build(google, settings=dataclasses.replace(settings_for(), sweep_service_account=SA))
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.google = store, google
        yield client


def oidc(google, email=SA, aud=PUBLIC, **kwargs) -> dict:
    return {"Authorization": f"Bearer {google.id_token('1234567890', email, aud, **kwargs)}"}


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


class Sweeper:
    """One user's agent and drive; asks are answered with files whose names carry an upload time `age` seconds before
    `self.now`, which is moved to a chosen distance after the answer with `at(seconds_after_answer)`."""

    def __init__(self, hosted, google, sub=DENIS_SUB, email="owner@example.com"):
        self.hosted, self.google, self.sub = hosted, google, sub
        self.token = agent(hosted, google, sub=sub, email=email)
        self.drive = google.drive(sub)
        self.drive._clock = datetime.now(timezone.utc)  # Drive's own times follow the sweep's clock, as in production
        self.now = time.time()
        self.asks: dict[int, float] = {}  # ask id -> answered epoch

    def ask(self) -> int:
        return tool_result(mcp(self.hosted, self.token, "ask_create", {"title": "Ship?"}))["id"]

    def put(self, ask_id: int, n: int, uploaded: float | None = None, secret=False, created: float | None = None,
            **props) -> str:
        """A file of the answer; the upload time in its name and Drive's createdTime are `uploaded` unless `created`
        says Drive saw it at another time."""
        moment = self.now if uploaded is None else uploaded
        app_properties = {"ask_id": str(ask_id), "kind": "file", "file": str(n), "secret": "1" if secret else "0",
                          **props}
        return self.drive.add(f"afile-{ask_id}-{n}-{int(moment * 1000)}", b"Q3 report " * 10, app_properties,
                              mime="application/octet-stream", created=moment if created is None else created)

    def answered(self, ask_id: int, *specs) -> list[dict]:
        """Answer ask `ask_id` with files; each spec is (n, secret) and gets a Drive file uploaded 1 minute before
        the answer. The sweep's clock is set to the answer's time."""
        items = []
        for n, secret in specs:
            file_id = self.put(ask_id, n, uploaded=time.time() - 60, secret=secret)
            items.append(item(n, file_id=file_id, secret=secret, bytes=100, uploaded_at="2026-11-19T09:59:00.000Z"))
        written = answered_by_files(self.drive, ask_id, items)
        at = epoch(written[0]["answered_at"])
        self.asks[ask_id] = at
        for i in written:
            i["uploaded_at"] = i["answered_at"]  # the apps stamp the upload before; min() is the same instant here
        self._restamp(ask_id, written)
        return written

    def _restamp(self, ask_id: int, written: list[dict]) -> None:
        file = self.drive.file(f"ask-{ask_id}.json")
        doc = json.loads(file["content"])
        doc["answer_files"] = written
        self.drive.write(file["id"], doc, {})

    def at(self, ask_id: int, seconds: float) -> float:
        self.now = self.asks[ask_id] + seconds
        return self.now

    def doc(self, ask_id: int) -> dict:
        return self.drive.doc(ask_id)

    def sweep(self, now=None, cursor=None, limit=50):
        app = self.hosted.app.state
        return sweep(app.store, app.pool, self.now if now is None else now, cursor, limit)

    def exists(self, file_id: str) -> bool:
        return file_id in self.drive.files

    def hold(self, ask_id: int, file_id: str, state: str, n=1, until=None):
        key = claim_key(self.sub, ask_id, file_id)
        doc = {"user": self.sub, "ask_id": ask_id, "state": state}
        if state == "held":
            doc.update(until=until, owner="o1")
        self.hosted.store.put("fileclaims", key, doc)
        return key


@pytest.fixture
def world(hosted, google):
    return Sweeper(hosted, google)


# ---------- the route: OIDC ----------

def test_oidc_token_of_the_service_account_is_accepted(hosted, google):
    response = hosted.post(URL, headers=oidc(google))
    assert response.status_code == 200
    assert response.json() == {"next_cursor": None, "stats": {}}  # no users yet


@pytest.mark.parametrize("headers", [
    "wrong email", "wrong audience", "not verified", "no token", "not a jwt", "app client token", "basic",
])
def test_every_other_caller_is_401_with_an_empty_body(hosted, google, headers):
    sent = {
        "wrong email": oidc(google, email="someone@example.com"),
        "wrong audience": oidc(google, aud="https://other.example"),
        "not verified": oidc(google, email_verified=False),
        "no token": {},
        "not a jwt": {"Authorization": "Bearer qta_" + "a" * 43},
        "app client token": {"Authorization": f"Bearer {google.id_token('1', SA, 'ios-client')}"},
        "basic": {"Authorization": "Basic Zm9vOmJhcg=="},
    }[headers]
    response = hosted.post(URL, headers=sent)
    assert response.status_code == 401
    assert response.content == b""


def test_a_forged_token_is_401(hosted, google):
    from cryptography.hazmat.primitives.asymmetric import rsa
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = hosted.post(URL, headers=oidc(google, key=forged))
    assert response.status_code == 401 and response.content == b""


def test_service_account_not_configured_refuses_everyone(google):
    app, _ = build(google)
    with TestClient(app, base_url=PUBLIC) as client:
        response = client.post(URL, headers=oidc(google))
    assert response.status_code == 401 and response.content == b""


def test_get_is_not_the_route(hosted, google):
    assert hosted.get(URL, headers=oidc(google)).status_code in (404, 405)


def test_failed_checks_count_against_the_bearer_limit(hosted, google):
    for _ in range(60):
        assert hosted.post(URL, headers={}).status_code == 401
    # the address is now slowed down: even the right token is turned away until the window passes
    assert hosted.post(URL, headers=oidc(google)).status_code == 401


def test_route_is_not_in_openapi_and_has_no_cors(google):
    app, _ = build(google, settings=dataclasses.replace(
        settings_for(cors_origins=("https://app.example.com",)), sweep_service_account=SA))
    assert not any("/internal" in path for path in app.openapi()["paths"])
    with TestClient(app, base_url=PUBLIC) as client:
        for response in (client.post(URL, headers={**oidc(google), "Origin": "https://app.example.com"}),
                         client.post(URL, headers={"Origin": "https://app.example.com"}),
                         client.options(URL, headers={"Origin": "https://app.example.com",
                                                      "Access-Control-Request-Method": "POST"})):
            assert "access-control-allow-origin" not in response.headers


# ---------- the four rules ----------

def test_secret_file_is_deleted_24h_after_the_answer_not_before(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    world.at(ask, SECRET_TTL - 60)
    assert world.sweep()[1].get("deleted_expired") is None
    assert world.exists(one["file_id"])
    world.at(ask, SECRET_TTL)
    _, stats = world.sweep()
    assert stats["deleted_expired"] == 1
    assert not world.exists(one["file_id"])
    gone = world.doc(ask)["answer_files"][0]
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "expired")
    assert gone["deleted_at"] is not None


def test_secret_age_counts_from_the_earlier_of_upload_and_answer(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    doc = world.doc(ask)
    doc["answer_files"][0]["uploaded_at"] = "2026-01-01T00:00:00.000Z"
    world.drive.write(world.drive.file(f"ask-{ask}.json")["id"], doc, {})
    world.drive.files[one["file_id"]]["createdTime"] = "2026-01-01T00:00:00.000Z"  # Drive saw it then, too
    world.at(ask, 60)  # the answer is a minute old, the upload months
    assert world.sweep()[1]["deleted_expired"] == 1


def test_secret_with_a_held_unexpired_claim_is_kept_past_24h(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    world.at(ask, SECRET_TTL + 5 * HOUR)
    world.hold(ask, one["file_id"], "held", until=world.now + 100)
    assert "deleted_expired" not in world.sweep()[1]
    assert world.exists(one["file_id"])
    world.hold(ask, one["file_id"], "held", until=world.now - 1)  # the lease ran out: nobody is sending
    assert world.sweep()[1]["deleted_expired"] == 1


def test_secret_with_a_done_claim_is_deleted_at_once_and_recorded_as_sent(world):
    """Task 6's leftover: the send went out, the Drive delete failed; the claim is done and the file still there."""
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    world.at(ask, 120)  # nowhere near 24 h
    world.hold(ask, one["file_id"], "done")
    _, stats = world.sweep()
    assert stats["deleted_sent"] == 1
    assert not world.exists(one["file_id"])
    gone = world.doc(ask)["answer_files"][0]
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "sent")
    assert gone["downloaded_at"] is not None


def test_files_of_a_closed_ask_are_deleted_at_any_age(world):
    ask = world.ask()
    ones = world.answered(ask, (1, False), (2, True))
    app_action(world.drive, ask, "closed")  # the app closes, its Drive delete failed or never ran
    world.at(ask, 30)
    _, stats = world.sweep()
    assert stats["deleted_closed"] == 2
    assert not any(world.exists(i["file_id"]) for i in ones)
    assert {(f["file_id"], f["deleted_reason"]) for f in world.doc(ask)["answer_files"]} == {(None, "closed")}


def test_claims_of_closed_asks_are_deleted_others_kept(world):
    closed, open_ = world.ask(), world.ask()
    [c] = world.answered(closed, (1, True))
    [o] = world.answered(open_, (1, True))
    world.hold(closed, c["file_id"], "done")
    kept = world.hold(open_, o["file_id"], "held", until=time.time() + 600)
    app_action(world.drive, closed, "closed")
    world.sweep(now=time.time())
    claims = world.hosted.store.all("fileclaims")
    assert set(claims) == {kept}


def test_closed_ask_with_nulled_item_still_loses_the_file(world):
    """The bridge's close nulls file_id in the record before the Drive delete; if that failed the name finds it."""
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    world.drive.fail_next("DELETE", "/drive/v3/files", 500)
    tool_result(mcp(world.hosted, world.token, "ask_close", {"id": ask, "resolution": "done"}))
    assert world.exists(one["file_id"]) and world.doc(ask)["answer_files"][0]["file_id"] is None
    _, stats = world.sweep(now=time.time())
    assert stats["deleted_closed"] == 1 and not world.exists(one["file_id"])


@pytest.mark.parametrize("secret, limit", [(False, FILE_ORPHAN), (True, SECRET_ORPHAN)])
def test_orphans_are_deleted_by_the_time_in_the_name(world, secret, limit):
    young = world.put(7, 1, uploaded=world.now - limit + 60, secret=secret)
    old = world.put(7, 2, uploaded=world.now - limit - 1, secret=secret)
    exactly = world.put(7, 3, uploaded=world.now - limit, secret=secret)
    _, stats = world.sweep()
    assert stats["deleted_orphan"] == 2
    assert world.exists(young) and not world.exists(old) and not world.exists(exactly)


def test_orphan_of_a_replaced_answer_goes_by_age_not_with_the_answer(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    again = world.put(ask, 2, uploaded=world.now)
    answered_by_files(world.drive, ask, [item(2, file_id=again, bytes=100)])  # the new answer names file 2 only
    world.now = time.time() + 2 * HOUR
    assert "deleted_orphan" not in world.sweep()[1] and world.exists(one["file_id"])
    world.now = time.time() + FILE_ORPHAN + 100
    assert world.sweep()[1]["deleted_orphan"] == 1
    assert not world.exists(one["file_id"]) and world.exists(again)


def test_normal_file_is_deleted_30_days_after_the_answer(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    world.at(ask, NORMAL_TTL - 60)
    assert world.sweep()[1].get("deleted_expired") is None and world.exists(one["file_id"])
    world.at(ask, NORMAL_TTL)
    assert world.sweep()[1]["deleted_expired"] == 1
    gone = world.doc(ask)["answer_files"][0]
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "expired")


def test_a_normal_file_a_day_after_the_answer_stays(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    world.at(ask, 2 * DAY)
    assert world.sweep()[1] == {"users": 1} and world.exists(one["file_id"])


def test_the_constants_are_the_spec_ages():
    assert (SECRET_TTL, SECRET_ORPHAN, FILE_ORPHAN, NORMAL_TTL) == (24 * HOUR, HOUR, 24 * HOUR, 30 * DAY)


# ---------- never a Drive delete without the guard ----------

def test_files_without_our_marks_are_never_deleted(world):
    old = world.now - 400 * DAY
    foreign = [
        world.drive.add(f"afile-9-1-{int(old * 1000)}", b"x", {}),  # no kind, no ask_id
        world.drive.add(f"afile-9-2-{int(old * 1000)}", b"x", {"kind": "voice", "ask_id": "9", "file": "2"}),
        world.drive.add(f"afile-9-3-{int(old * 1000)}", b"x", {"kind": "file", "ask_id": "8", "file": "3"}),  # other ask
        world.drive.add(f"afile-9-4-{int(old * 1000)}", b"x", {"kind": "file", "ask_id": "9", "file": "5"}),  # other n
        world.drive.add("afile-notes", b"x", {"kind": "file", "ask_id": "9", "file": "1"}),  # not our name
    ]
    assert world.sweep()[1] == {"users": 1}
    assert all(world.exists(f) for f in foreign)


def test_the_item_naming_a_file_of_another_ask_deletes_nothing(world):
    """A user can put any file id into their own ask file: the Drive properties decide (records._delete_answer_file)."""
    ask, other = world.ask(), world.ask()
    world.answered(ask, (1, False))
    world.at(ask, NORMAL_TTL + 1)
    theirs = {"file_id": world.put(other, 1, uploaded=world.now)}  # a young file of the other ask
    doc = world.doc(ask)
    doc["answer_files"][0]["file_id"] = theirs["file_id"]
    world.drive.write(world.drive.file(f"ask-{ask}.json")["id"], doc, {})
    world.sweep()
    assert world.exists(theirs["file_id"])


# ---------- the record: guards and healing ----------

def test_the_ask_is_not_written_for_a_changed_answer(world):
    """A re-answer came first: its item may reuse the id but has another answered_at: the late write is dropped."""
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    stale = {**world.doc(ask), "id": ask}
    time.sleep(0.01)  # another millisecond: another answered_at
    answered_by_files(world.drive, ask, [item(1, file_id=one["file_id"], bytes=100)])  # a new answer, same file id
    app = world.hosted.app.state
    assert sweep_module._mark_deleted(app.pool, world.sub, stale, one["file_id"], "expired", "2026-11-19T10:00:00.000Z") \
        is False
    assert world.doc(ask)["answer_files"][0]["file_id"] == one["file_id"]


def test_the_ask_is_not_written_for_another_file(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    app = world.hosted.app.state
    ask_doc = {**world.doc(ask), "id": ask}
    assert sweep_module._mark_deleted(app.pool, world.sub, ask_doc, "other-file", "expired", "2026-11-19T10:00:00.000Z") \
        is False
    assert sweep_module._mark_deleted(app.pool, world.sub, ask_doc, one["file_id"], "expired",
                                      "2026-11-19T10:00:00.000Z") is True
    assert sweep_module._mark_deleted(app.pool, world.sub, ask_doc, one["file_id"], "expired",
                                      "2026-11-19T10:00:01.000Z") is False  # once


def test_item_whose_drive_file_is_gone_is_healed(world):
    """Task 6's leftover: the download route found the file not there (410) but the ask still names it."""
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    del world.drive.files[one["file_id"]]
    world.put(77, 1)  # a young file of the user's: with no afile- file at all the asks are not even listed
    world.at(ask, 600)
    _, stats = world.sweep()
    assert stats["healed"] == 1
    gone = world.doc(ask)["answer_files"][0]
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "expired")
    assert gone["deleted_at"] is not None
    assert world.sweep()[1] == {"users": 1}  # nothing left to heal


def test_healing_names_the_reason_it_knows(world):
    sent, closed = world.ask(), world.ask()
    [a] = world.answered(sent, (1, True))
    [b] = world.answered(closed, (1, False))
    world.hold(sent, a["file_id"], "done")
    app_action(world.drive, closed, "closed")
    del world.drive.files[a["file_id"]], world.drive.files[b["file_id"]]
    world.sweep()
    assert world.doc(sent)["answer_files"][0]["deleted_reason"] == "sent"
    assert world.doc(closed)["answer_files"][0]["deleted_reason"] == "closed"


def test_an_item_whose_file_exists_under_another_name_is_not_healed(world):
    ask = world.ask()
    other = world.drive.add("something-else", b"x", {})
    answered_by_files(world.drive, ask, [item(1, file_id=other, bytes=1)])
    world.sweep(now=time.time() + 400 * DAY)
    assert world.doc(ask)["answer_files"][0]["file_id"] == other and world.exists(other)


# ---------- slices, the cursor, a user whose grant is gone ----------

def three_users(hosted, google):
    mine = Sweeper(hosted, google)
    second = Sweeper(hosted, google, sub=OLGA_SUB, email=OLGA_EMAIL)
    third = Sweeper(hosted, google, sub="3003", email="third@example.com")
    return mine, second, third


def stale_orphan(who) -> str:
    return who.put(5, 1, uploaded=time.time() - 3 * DAY)


def test_limit_users_per_call_and_the_cursor_resumes(hosted, google):
    users = three_users(hosted, google)
    files = {u.sub: stale_orphan(u) for u in users}  # subs in order: 1001, 2002, 3003
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time(), None, 2)
    assert cursor == "2002" and stats["users"] == 2
    assert not users[0].exists(files["1001"]) and not users[1].exists(files["2002"]) and users[2].exists(files["3003"])
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time(), cursor, 2)
    assert cursor is None and stats["users"] == 1
    assert not users[2].exists(files["3003"])


def test_an_exact_multiple_of_limit_ends_with_no_cursor(hosted, google):
    three_users(hosted, google)
    cursor, _ = sweep(hosted.store, hosted.app.state.pool, time.time(), None, 3)
    assert cursor is None


def test_a_user_without_a_grant_is_counted_not_swept(hosted, google):
    mine = Sweeper(hosted, google)
    hosted.store.update("users", DENIS_SUB, {"google_refresh": None})
    stale_orphan(mine)
    _, stats = sweep(hosted.store, hosted.app.state.pool, time.time(), None, 10)
    assert stats == {"unlinked": 1}


def test_revoked_grant_user_skipped_and_cursor_advances(hosted, google, caplog):
    first, second, third = three_users(hosted, google)
    mine = stale_orphan(first)
    theirs = stale_orphan(second)
    last = stale_orphan(third)
    for token, sub in list(google.refresh_tokens.items()):
        if sub == DENIS_SUB:
            google.revoked_refresh.add(token)  # Denis revoked AskHooman in Google's settings
    caplog.set_level(logging.INFO)
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time(), None, 2)
    assert cursor == "2002"  # past both users of the slice, the failed one included
    assert stats == {"users_failed": 1, "users": 1, "deleted_orphan": 1}
    assert first.exists(mine) and not second.exists(theirs)  # the revoked user's file is the apps' sweep's
    assert "user 1001 skipped" in caplog.text
    assert not hosted.app.state.accounts.drive_linked(DENIS_SUB)
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time(), cursor, 2)  # and the run goes on
    assert cursor is None and stats == {"users": 1, "deleted_orphan": 1} and not third.exists(last)


def test_a_drive_error_skips_the_user_and_the_run_goes_on(hosted, google):
    first, second, _ = three_users(hosted, google)
    stale_orphan(first)
    theirs = stale_orphan(second)
    first.drive.fail_next("GET", "/drive/v3/files", 500)
    _, stats = sweep(hosted.store, hosted.app.state.pool, time.time(), None, 10)
    assert stats["users_failed"] == 1 and stats["users"] == 2
    assert not second.exists(theirs)


def test_a_failing_file_does_not_stop_the_user_s_other_files(world):
    a, b = world.put(5, 1, uploaded=world.now - 3 * DAY), world.put(5, 2, uploaded=world.now - 3 * DAY)
    world.drive.fail_next("DELETE", "/drive/v3/files", 500)
    _, stats = world.sweep()
    assert stats["failed"] == 1 and stats["deleted_orphan"] == 1
    assert world.exists(a) != world.exists(b)
    assert world.sweep()[1] == {"users": 1, "deleted_orphan": 1}  # the next hour finishes it


# ---------- the route runs the sweep ----------

def test_the_route_sweeps_and_says_counts_only(hosted, google, caplog):
    world = Sweeper(hosted, google)
    file_id = stale_orphan(world)
    caplog.set_level(logging.INFO)
    response = hosted.post(URL, headers=oidc(google))
    assert response.status_code == 200
    assert response.json() == {"next_cursor": None, "stats": {"users": 1, "deleted_orphan": 1}}
    assert not world.exists(file_id)
    assert "afile-" not in caplog.text and "Q3 report" not in caplog.text


def test_the_route_walks_all_slices_and_hands_back_the_cursor_when_the_budget_ends(hosted, google, monkeypatch):
    users = three_users(hosted, google)
    for who in users:
        stale_orphan(who)
    response = hosted.post(f"{URL}?limit=1", headers=oidc(google))
    assert response.json() == {"next_cursor": None, "stats": {"users": 3, "deleted_orphan": 3}}
    for who in users:
        stale_orphan(who)
    from server.hosted import app as app_module
    monkeypatch.setattr(app_module, "SWEEP_BUDGET", 0.0)
    response = hosted.post(f"{URL}?limit=1", headers=oidc(google))
    assert response.json()["next_cursor"] == "1001" and response.json()["stats"]["users"] == 1
    monkeypatch.setattr(app_module, "SWEEP_BUDGET", 600.0)
    response = hosted.post(f"{URL}?limit=5&cursor=1001", headers=oidc(google))
    assert response.json() == {"next_cursor": None, "stats": {"users": 2, "deleted_orphan": 2}}


def test_the_service_account_setting_is_read_from_the_environment():
    from server.hosted.config import ConfigError, HostedSettings
    env = {"ASKS_GOOGLE_WEB_CLIENT": '{"client_id": "c", "client_secret": "s"}', "ASKS_GOOGLE_APP_CLIENT_IDS": "i",
           "ASKS_ENCRYPTION_KEY": "k" * 8}
    assert HostedSettings.from_env(env).sweep_service_account is None
    assert HostedSettings.from_env({**env, "ASKS_SWEEP_SERVICE_ACCOUNT": f" {SA.upper()} "}).sweep_service_account == SA
    with pytest.raises(ConfigError):
        HostedSettings.from_env({**env, "ASKS_SWEEP_SERVICE_ACCOUNT": "not-an-email"})


# ---------- fix round 1: the stored cursor, the deadline, one users read ----------

def test_bare_runs_resume_from_the_stored_cursor_and_wrap(hosted, google, monkeypatch):
    from server.hosted import app as app_module
    users = three_users(hosted, google)
    files = {u.sub: stale_orphan(u) for u in users}
    monkeypatch.setattr(app_module, "SWEEP_BUDGET", 0.0)  # one slice per run
    run = lambda: hosted.post(f"{URL}?limit=1", headers=oidc(google)).json()  # noqa: E731 (a bare POST: no cursor)
    assert run()["next_cursor"] == "1001"
    assert hosted.store.get("sweep", "cursor") == {"cursor": "1001"}
    assert not users[0].exists(files["1001"]) and users[1].exists(files["2002"])
    assert run()["next_cursor"] == "2002"
    assert not users[1].exists(files["2002"]) and users[2].exists(files["3003"])
    assert run()["next_cursor"] is None  # the last user: wrapped
    assert hosted.store.get("sweep", "cursor") == {"cursor": None}
    again = stale_orphan(users[0])
    assert run()["next_cursor"] == "1001" and not users[0].exists(again)  # and starts over at the first user


def test_an_explicit_cursor_wins_over_the_stored_one(hosted, google):
    users = three_users(hosted, google)
    files = {u.sub: stale_orphan(u) for u in users}
    hosted.store.put("sweep", "cursor", {"cursor": "2002"})
    assert hosted.post(f"{URL}?cursor=1001&limit=1", headers=oidc(google)).status_code == 200
    assert not users[1].exists(files["2002"])  # started after 1001, not 2002


def test_the_deadline_stops_between_files_of_one_user(world, monkeypatch):
    clock = {"t": 0.0}
    real = sweep_module.HostedAsks._delete_answer_file

    def slow(self, *args):
        clock["t"] += 10
        return real(self, *args)
    monkeypatch.setattr(sweep_module.HostedAsks, "_delete_answer_file", slow)
    ids = [world.put(5, n, uploaded=world.now - 3 * DAY) for n in (1, 2, 3)]
    app = world.hosted.app.state
    cursor, stats = sweep(app.store, app.pool, world.now, None, 10, deadline=15, clock=lambda: clock["t"])
    assert cursor is None and stats.get("deleted_orphan") == 2  # stopped at the unfinished user: resume at it
    assert [world.exists(i) for i in ids] == [False, False, True]
    clock["t"] = 0.0
    assert sweep(app.store, app.pool, world.now, cursor, 10, deadline=500, clock=lambda: clock["t"])[0] is None
    assert not any(world.exists(i) for i in ids)


def test_the_deadline_stops_between_users_and_resumes_at_the_unfinished_one(hosted, google, monkeypatch):
    clock = {"t": 0.0}
    real = sweep_module.HostedAsks._delete_answer_file

    def slow(self, *args):
        clock["t"] += 10
        return real(self, *args)
    monkeypatch.setattr(sweep_module.HostedAsks, "_delete_answer_file", slow)
    users = three_users(hosted, google)
    files = {u.sub: stale_orphan(u) for u in users}
    pool = hosted.app.state.pool
    cursor, stats = sweep(hosted.store, pool, time.time(), None, 10, deadline=15, clock=lambda: clock["t"])
    assert cursor == "2002" and stats["users"] == 2  # the third was not begun
    assert users[2].exists(files["3003"])
    cursor, _ = sweep(hosted.store, pool, time.time(), cursor, 10, deadline=1000, clock=lambda: clock["t"])
    assert cursor is None and not users[2].exists(files["3003"])


def test_a_run_reads_the_user_ids_once(hosted, google, monkeypatch):
    three_users(hosted, google)
    reads = []
    real = hosted.store.all
    monkeypatch.setattr(hosted.store, "all", lambda name: (reads.append(name), real(name))[1])
    assert hosted.post(f"{URL}?limit=1", headers=oidc(google)).status_code == 200
    assert reads.count("users") == 1


# ---------- fix round 1: more cases the review asked for ----------

def test_an_open_ask_file_in_age_is_never_deleted(world):
    ask = world.ask()  # a real ask, still open
    normal = world.put(ask, 1, uploaded=world.now - 23 * HOUR)
    secret = world.put(ask, 2, uploaded=world.now - 50 * 60, secret=True)
    assert world.sweep()[1] == {"users": 1}
    assert world.exists(normal) and world.exists(secret)
    world.now += 2 * HOUR  # the normal one is past its day as an orphan; the secret draft of an OPEN ask is not (I-3)
    assert world.sweep()[1]["deleted_orphan"] == 1 and world.exists(secret)
    world.now = world.now - 2 * HOUR + SECRET_TTL - 50 * 60 + CLOCK_SLACK  # createdTime + 24 h + slack: its ceiling
    assert world.sweep()[1]["deleted_expired"] == 1 and not world.exists(secret)


def test_an_old_file_with_an_unparseable_name_survives(world):
    old = world.now - 400 * DAY
    names = [f"afile-1-1-{int(old * 1000)}x", "afile-1-1", "afile--1-1-5", f"afile-1-1-{int(old * 1000)}-2", "afile-"]
    ids = [world.drive.add(n, b"x", {"kind": "file", "ask_id": "1", "file": "1", "secret": "0"}) for n in names]
    assert world.sweep()[1] == {"users": 1} and all(world.exists(i) for i in ids)


def test_a_drive_error_while_healing_does_not_heal_the_record(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    del world.drive.files[one["file_id"]]
    world.put(77, 1)  # (see test_item_whose_drive_file_is_gone_is_healed)
    world.drive.fail_next("GET", f"/drive/v3/files/{one['file_id']}", 503)
    _, stats = world.sweep()
    assert stats["failed"] == 1 and "healed" not in stats
    assert world.doc(ask)["answer_files"][0]["file_id"] == one["file_id"]  # not written: the file may be there
    assert world.sweep()[1]["healed"] == 1  # the next hour, with Drive answering


# ---------- final review: C1 (an ask that can't be read is never «no ask») and I3 (what a run reads) ----------

def stale_answered(world, *specs):
    """An answered ask with files, then the clock a day and an hour past the answer: every file would be an orphan
    (secret 1 h, normal 24 h) if the sweep believed its ask were not there."""
    ask = world.ask()
    ones = world.answered(ask, *specs)
    world.at(ask, DAY + 120)  # past the orphan ages (24 h from createdTime = 24 h - 60 s here), before the ceilings
    return ask, ones


def ask_file(world, ask) -> dict:
    return world.drive.file(f"ask-{ask}.json")


def break_ask_file(world, ask, how: str) -> None:
    file = ask_file(world, ask)
    world.hosted.app.state.pool._cache.clear()
    if how == "drive 500":
        world.drive.fail_next("GET", f"/drive/v3/files/{file['id']}", 500)
    elif how == "corrupt":
        file["content"], file["modifiedTime"] = b"not json at all", world.drive._tick()
    elif how == "oversize":
        file["content"], file["modifiedTime"] = b" " * (300 * 1024), world.drive._tick()
    elif how == "unknown status":
        doc = json.loads(file["content"])
        doc["status"] = "bewildered"
        world.drive.write(file["id"], doc, {})
    elif how == "gone between list and read":
        world.drive.fail_next("GET", f"/drive/v3/files/{file['id']}", 404)


@pytest.mark.parametrize("how", ["drive 500", "corrupt", "oversize", "unknown status", "gone between list and read"])
def test_an_ask_that_cannot_be_read_keeps_its_files(world, how):
    ask, ones = stale_answered(world, (1, False), (2, True))
    break_ask_file(world, ask, how)
    _, stats = world.sweep()
    assert not any(key.startswith("deleted_") for key in stats), stats
    assert all(world.exists(one["file_id"]) for one in ones)


def test_an_unreadable_ask_does_not_shield_the_files_of_an_absent_one(world):
    ask, ones = stale_answered(world, (1, False))
    gone = world.put(99, 1, uploaded=world.now - 3 * DAY)  # no ask #99 at all: a real orphan
    break_ask_file(world, ask, "corrupt")
    _, stats = world.sweep()
    assert stats["deleted_orphan"] == 1 and not world.exists(gone) and world.exists(ones[0]["file_id"])


def test_the_next_run_with_a_readable_ask_goes_by_its_rules(world):
    ask, [one] = stale_answered(world, (1, False))
    break_ask_file(world, ask, "drive 500")
    assert world.sweep()[1] == {"users": 1} and world.exists(one["file_id"])
    _, stats = world.sweep()  # Drive answers again: the ask names the file, a normal file of 25 h is fine
    assert stats == {"users": 1} and world.exists(one["file_id"])
    world.at(ask, NORMAL_TTL)
    assert world.sweep()[1]["deleted_expired"] == 1


def test_a_truncated_listing_deletes_no_orphan(world):
    for n in (5, 6, 7):  # task files fill the listing first: the ask file is past the cut
        world.drive.add(f"task-{n}.json", {"id": n}, {})
    ask, ones = stale_answered(world, (1, False), (2, True))
    stranger = world.put(99, 1, uploaded=world.now - 3 * DAY)
    world.hosted.app.state.pool.client(world.sub).max_files = 3
    _, stats = world.sweep()
    assert not any(key.startswith("deleted_") for key in stats), stats
    assert world.exists(stranger) and all(world.exists(one["file_id"]) for one in ones)
    world.hosted.app.state.pool.client(world.sub).max_files = 5000
    _, stats = world.sweep()  # complete again: the stranger is an orphan, the ask's files follow their ask
    assert stats["deleted_orphan"] == 1 and not world.exists(stranger)


def test_the_files_of_a_listed_open_ask_are_orphans_by_age_even_when_the_listing_is_truncated(world):
    ask = world.ask()  # open: its file is named by nothing yet
    old = world.put(ask, 1, uploaded=world.now - 3 * DAY)
    world.hosted.app.state.pool.client(world.sub).max_files = 1  # ask-1.json is the first (only) one read
    assert world.sweep()[1]["deleted_orphan"] == 1 and not world.exists(old)


def ask_reads(world) -> list[str]:
    """The ids of the ask files Drive was asked for the content of."""
    ids = {f["id"]: f["name"] for f in world.drive.files.values() if f["name"].startswith("ask-")}
    return [ids[path.rsplit("/", 1)[1]] for _, path, params in world.drive.calls("GET", "/drive/v3/files/")
            if params.get("alt") == "media" and path.rsplit("/", 1)[1] in ids]


def test_a_run_reads_only_the_asks_whose_files_are_there(world):
    named, answered_empty = world.ask(), world.ask()
    keep = world.answered(named, (1, False))
    world.answered(answered_empty, (1, False))
    answered_empty_file = world.doc(answered_empty)["answer_files"][0]["file_id"]
    del world.drive.files[answered_empty_file]  # its only file is gone: nothing there names that ask
    world.hosted.app.state.pool._cache.clear()
    world.now += 10 * DAY  # long past the window in which a leftover is looked for
    before = len(ask_reads(world))
    assert world.sweep()[1] == {"users": 1}
    reads = ask_reads(world)[before:]
    assert reads == [f"ask-{named}.json"] and world.exists(keep[0]["file_id"])


def test_a_user_with_no_files_reads_no_ask(world):
    for _ in range(3):
        world.answered(world.ask(), (1, False))
    for file_id in [i for i, f in world.drive.files.items() if f["name"].startswith("afile-")]:
        del world.drive.files[file_id]
    world.hosted.app.state.pool._cache.clear()
    world.now += 10 * DAY
    before = len(ask_reads(world))
    world.sweep()
    assert ask_reads(world)[before:] == []


def test_the_deadline_is_checked_while_the_asks_are_read(world, monkeypatch):
    asks = [world.ask() for _ in range(4)]
    for number in asks:
        world.answered(number, (1, False))
    world.hosted.app.state.pool._cache.clear()
    clock = {"t": 0.0}
    drive = world.hosted.app.state.pool.client(world.sub)
    real = drive.download

    def slow(file_id):
        clock["t"] += 10
        return real(file_id)
    monkeypatch.setattr(drive, "download", slow)
    app = world.hosted.app.state
    cursor, _ = sweep(app.store, app.pool, world.now + 3 * DAY, None, 10, deadline=15, clock=lambda: clock["t"])
    assert cursor is None and clock["t"] <= 30  # stopped at the unfinished user after two or three reads, not all four


# ---------- final review: I1 (a secret that was sent is sent, whatever the claim key says) ----------

def mark_sent(world, ask, **over):
    doc = world.doc(ask)
    doc["answer_files"][0].update(downloaded_at="2026-11-19T09:00:00.000Z", **over)
    world.drive.write(world.drive.file(f"ask-{ask}.json")["id"], doc, {})


def test_a_secret_item_with_downloaded_at_and_a_live_file_is_deleted_as_sent(world):
    """The send went out, the delete failed, a re-answer kept the item: a new claim key, no claim document at all."""
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    mark_sent(world, ask)
    world.at(ask, 120)  # nowhere near 24 h
    _, stats = world.sweep()
    assert stats["deleted_sent"] == 1 and not world.exists(one["file_id"])
    gone = world.doc(ask)["answer_files"][0]
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "sent")
    assert gone["downloaded_at"] == "2026-11-19T09:00:00.000Z"


def test_a_sent_secret_with_a_held_claim_is_left_to_the_send_in_flight(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    mark_sent(world, ask)
    world.hold(ask, one["file_id"], "held", until=world.at(ask, 120) + 100)
    assert "deleted_sent" not in world.sweep()[1] and world.exists(one["file_id"])


def test_a_normal_file_with_downloaded_at_is_not_a_sent_file(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    mark_sent(world, ask)  # a normal file is downloaded many times: that proves nothing
    world.at(ask, 120)
    assert world.sweep()[1] == {"users": 1} and world.exists(one["file_id"])


# ---------- final review: I2 (lifetimes are bounded by Drive's createdTime, not by a client's clock) ----------

SLACK = 600


def skewed(world, ask, seconds: float, created_ago: float = 60) -> dict:
    """An answered ask with one file of each kind whose client stamps (the ask's answered_at, the item's answered_at
    and uploaded_at) are `seconds` off from the real ones, while Drive made the file `created_ago` seconds before
    now. Returns {secret, normal} -> {file_id, created}."""
    real = world.now
    made = {}
    ones = world.answered(ask, (1, True), (2, False))
    doc = world.doc(ask)
    shift = lambda stamp: _iso_shift(stamp, seconds)  # noqa: E731
    doc["answered_at"] = shift(doc["answered_at"])
    for entry in doc["answer_files"]:
        entry["answered_at"], entry["uploaded_at"] = doc["answered_at"], doc["answered_at"]
    world.drive.write(world.drive.file(f"ask-{ask}.json")["id"], doc, {})
    for one in ones:
        world.drive.files[one["file_id"]]["createdTime"] = _iso_shift(_iso_of(real - created_ago), 0)
        made["secret" if one["secret"] else "normal"] = {"file_id": one["file_id"], "created": real - created_ago}
    return made


def _iso_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_shift(stamp: str, seconds: float) -> str:
    return _iso_of(epoch(stamp) + seconds)


def test_a_clock_two_days_behind_does_not_delete_a_secret_early(world):
    ask = world.ask()
    made = skewed(world, ask, -2 * DAY)
    created = made["secret"]["created"]
    for moment in (world.now, created + SECRET_TTL - 60):
        assert "deleted_expired" not in world.sweep(now=moment)[1]
        assert world.exists(made["secret"]["file_id"])
    assert world.sweep(now=created + SECRET_TTL)[1]["deleted_expired"] == 1  # Drive's time is the floor
    assert not world.exists(made["secret"]["file_id"])


def test_a_clock_three_days_ahead_does_not_keep_a_secret_for_four_days(world):
    ask = world.ask()
    made = skewed(world, ask, 3 * DAY)
    created = made["secret"]["created"]
    assert "deleted_expired" not in world.sweep(now=created + SECRET_TTL + SLACK - 1)[1]
    assert world.exists(made["secret"]["file_id"])
    assert world.sweep(now=created + SECRET_TTL + SLACK)[1]["deleted_expired"] == 1
    assert not world.exists(made["secret"]["file_id"])


def test_a_normal_file_lives_30_days_from_drive_s_createdTime_at_most(world):
    ask = world.ask()
    made = skewed(world, ask, 40 * DAY)  # the client says it answered in the far future
    created = made["normal"]["created"]
    world.sweep(now=created + NORMAL_TTL + SLACK - 1)  # (the secret file is long gone by then)
    assert world.exists(made["normal"]["file_id"])
    world.sweep(now=created + NORMAL_TTL + SLACK)
    assert not world.exists(made["normal"]["file_id"])


def test_a_clock_behind_does_not_delete_a_normal_file_early(world):
    ask = world.ask()
    made = skewed(world, ask, -40 * DAY)
    created = made["normal"]["created"]
    world.sweep(now=created + NORMAL_TTL - 60)
    assert world.exists(made["normal"]["file_id"])
    world.sweep(now=created + NORMAL_TTL)
    assert not world.exists(made["normal"]["file_id"])


def test_a_small_clock_difference_inside_the_slack_changes_nothing(world):
    ask = world.ask()
    world.answered(ask, (1, True))
    # answered_at + 24 h, as before
    assert "deleted_expired" not in world.sweep(now=world.asks[ask] + SECRET_TTL - 1)[1]
    assert world.sweep(now=world.asks[ask] + SECRET_TTL)[1]["deleted_expired"] == 1


@pytest.mark.parametrize("secret, limit", [(False, FILE_ORPHAN), (True, SECRET_ORPHAN)])
def test_an_orphan_is_aged_by_drive_s_createdTime_not_by_the_name(world, secret, limit):
    young_in_drive = world.put(7, 1, uploaded=world.now - 5 * DAY, created=world.now - limit + 60, secret=secret)
    # the name says now
    old_in_drive = world.put(7, 2, uploaded=world.now, created=world.now - limit - 1, secret=secret)
    _, stats = world.sweep()
    assert stats["deleted_orphan"] == 1
    assert world.exists(young_in_drive) and not world.exists(old_in_drive)


def test_an_orphan_without_a_createdTime_is_kept(world):
    file_id = world.put(7, 1, uploaded=world.now - 5 * DAY)
    del world.drive.files[file_id]["createdTime"]
    assert world.sweep()[1] == {"users": 1} and world.exists(file_id)


# ---------- final review: I5 (an unset service account is loud; the last run is kept and shown) ----------

def test_startup_warns_when_the_sweep_service_account_is_not_set(google, caplog):
    caplog.set_level(logging.WARNING)
    app, _ = build(google)  # sweep_service_account None
    with TestClient(app, base_url=PUBLIC):
        pass
    assert "ASKS_SWEEP_SERVICE_ACCOUNT is not set" in caplog.text
    assert "no answer file is ever swept" in caplog.text and "Upgrading an existing deployment" in caplog.text


def test_startup_does_not_warn_when_it_is_set(google, caplog):
    caplog.set_level(logging.WARNING)
    app, _ = build(google, settings=dataclasses.replace(settings_for(), sweep_service_account=SA))
    with TestClient(app, base_url=PUBLIC):
        pass
    assert "ASKS_SWEEP_SERVICE_ACCOUNT is not set" not in caplog.text


def test_a_run_keeps_its_time_and_counts(hosted, google):
    world = Sweeper(hosted, google)
    stale_orphan(world)
    before = time.time()
    assert hosted.store.get("sweep", "last_run") is None
    assert hosted.post(URL, headers=oidc(google)).status_code == 200
    last = hosted.store.get("sweep", "last_run")
    assert last["stats"] == {"users": 1, "deleted_orphan": 1} and last["more"] is False
    assert before - 1 <= epoch(last["at"]) <= time.time() + 1
    assert set(last) == {"at", "stats", "more", "more_in_a_row"}  # counts and a time: nothing of a file or a user


def test_a_refused_call_leaves_the_last_run_alone(hosted, google):
    hosted.store.put("sweep", "last_run", {"at": "2026-11-19T09:00:00.000Z", "stats": {"users": 1}, "more": False})
    assert hosted.post(URL, headers={}).status_code == 401
    assert hosted.store.get("sweep", "last_run")["at"] == "2026-11-19T09:00:00.000Z"


def test_the_operator_sees_the_last_run_in_the_admin_stats(google):
    from server.hosted.config import HostedSettings
    from .hosted_support import id_token
    settings = settings_for()
    settings = HostedSettings(**{**settings.__dict__, "admin_emails": ("owner@example.com",),
                                 "sweep_service_account": SA})
    app, store = build(google, settings=settings)
    with TestClient(app, base_url=PUBLIC) as client:
        mine = id_token(google, email="owner@example.com")
        assert client.get("/api/admin/stats", headers=mine).json()["sweep"] is None  # never ran
        assert client.post(URL, headers=oidc(google)).status_code == 200
        shown = client.get("/api/admin/stats", headers=mine).json()["sweep"]
    assert shown == {**store.get("sweep", "last_run"), "behind": False} and shown["more"] is False


# ---------- final review: M4 (a done claim has a life of its own: 90 days) ----------

def expired_done(world, ask, file_id, ago=60):
    key = world.hold(ask, file_id, "done")
    world.hosted.store.update("fileclaims", key, {"expires_at": time.time() - ago})
    return key


def test_an_expired_done_claim_is_deleted_by_the_sweep_and_an_unexpired_one_is_kept(world):
    first, second = world.ask(), world.ask()
    [a] = world.answered(first, (1, True))
    [b] = world.answered(second, (1, True))
    for ask in (first, second):
        mark_sent(world, ask)  # sent, the delete failed: the sweep finishes it this run
    world.at(first, 120)
    stale = expired_done(world, first, a["file_id"])
    fresh = world.hold(second, b["file_id"], "done")
    world.hosted.store.update("fileclaims", fresh, {"expires_at": time.time() + 86400})
    _, stats = world.sweep()
    assert stats["deleted_sent"] == 2 and stats["claims_deleted"] == 1
    assert world.hosted.store.get("fileclaims", stale) is None
    assert world.hosted.store.get("fileclaims", fresh) is not None


def test_an_expired_done_claim_of_an_answered_ask_with_no_live_file_goes(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    mark_sent(world, ask)
    world.at(ask, 120)
    key = expired_done(world, ask, one["file_id"])
    world.sweep()  # the file goes as «sent»: nothing live is left behind the claim
    assert not world.exists(one["file_id"])
    world.sweep()
    assert world.hosted.store.get("fileclaims", key) is None


def test_an_expired_done_claim_stays_while_its_file_is_still_there(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    world.at(ask, 120)
    key = expired_done(world, ask, one["file_id"])
    world.drive.fail_next("DELETE", f"/drive/v3/files/{one['file_id']}", 500)  # the sweep can't delete it this hour
    _, stats = world.sweep()
    assert stats["failed"] == 1 and world.exists(one["file_id"])
    assert world.hosted.store.get("fileclaims", key) is not None  # the claim is what refuses a second send
    assert "claims_deleted" not in stats


def test_a_done_claim_without_expires_at_and_a_held_one_are_left_alone(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    world.at(ask, 120)
    held = world.hold(ask, one["file_id"], "held", until=world.now + 600)
    world.hosted.store.update("fileclaims", held, {"expires_at": time.time() - 60})
    world.sweep()
    assert world.hosted.store.get("fileclaims", held) is not None


# ---------- fix round 2: I-A (the truncation is the listing's, not the shared client's) ----------

def test_a_concurrent_listing_on_the_same_client_does_not_make_a_cut_listing_look_complete(world, monkeypatch):
    for n in (5, 6, 7):
        world.drive.add(f"task-{n}.json", {"id": n}, {})
    ask, ones = stale_answered(world, (1, False), (2, True))
    stranger = world.put(99, 1, uploaded=world.now - 3 * DAY)
    drive = world.hosted.app.state.pool.client(world.sub)
    drive.max_files = 3
    real = drive.list_files

    def with_a_request_in_between(query=None):
        found = real(query)  # the sweep's listing, cut short
        real("name = 'nothing' and trashed = false")  # another request of the same user lists meanwhile: complete
        return found
    monkeypatch.setattr(drive, "list_files", with_a_request_in_between)
    _, stats = world.sweep()
    assert not any(key.startswith("deleted_") for key in stats), stats
    assert world.exists(stranger) and all(world.exists(one["file_id"]) for one in ones)


def test_a_listing_that_fills_the_cap_exactly_counts_as_cut(world):
    """Drive may hold one more file than the cap lets us see: a listing of exactly `max_files` is not trusted, here
    for deciding that a lapsed done claim has nothing behind it."""
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    mark_sent(world, ask)
    world.at(ask, 120)
    key = expired_done(world, ask, one["file_id"])
    world.hosted.app.state.pool.client(world.sub).max_files = 1  # one afile file there, the cap is one
    world.sweep()
    assert not world.exists(one["file_id"]) and world.hosted.store.get("fileclaims", key) is not None
    world.hosted.app.state.pool.client(world.sub).max_files = 5000
    world.sweep()
    assert world.hosted.store.get("fileclaims", key) is None


# ---------- fix round 2: I-C (a ceiling that needs no ask), I-B (one slow user can't hold the cursor), M5 ----------

from server.hosted.files import CLOCK_SLACK, lifetime_end  # noqa: E402


def created_at_of(world, file_id) -> float:
    return epoch(world.drive.files[file_id]["createdTime"])


@pytest.mark.parametrize("how", ["corrupt", "oversize", "unknown status", "drive 500"])
def test_the_files_of_an_ask_that_stays_unreadable_go_at_their_ceiling_not_before(world, how):
    ask = world.ask()
    secret, normal = world.answered(ask, (1, True), (2, False))
    created = {n: created_at_of(world, one["file_id"]) for n, one in ((1, secret), (2, normal))}
    break_ask_file(world, ask, how)

    def run(moment):
        world.hosted.app.state.pool._cache.clear()
        if how == "drive 500":
            world.drive.fail_next("GET", f"/drive/v3/files/{ask_file(world, ask)['id']}", 500)
        return world.sweep(now=moment)[1]
    assert not any(k.startswith("deleted_") for k in run(created[1] + SECRET_TTL + CLOCK_SLACK - 1))
    assert world.exists(secret["file_id"]) and world.exists(normal["file_id"])
    assert run(created[1] + SECRET_TTL + CLOCK_SLACK)["deleted_expired"] == 1  # the secret one, by its Drive marks
    assert not world.exists(secret["file_id"]) and world.exists(normal["file_id"])
    assert not any(k.startswith("deleted_") for k in run(created[2] + NORMAL_TTL + CLOCK_SLACK - 1))
    assert run(created[2] + NORMAL_TTL + CLOCK_SLACK)["deleted_expired"] == 1
    assert not world.exists(normal["file_id"])


def test_the_ceiling_waits_for_a_held_claim(world):
    ask = world.ask()
    [secret] = world.answered(ask, (1, True))
    break_ask_file(world, ask, "corrupt")
    ceiling = created_at_of(world, secret["file_id"]) + SECRET_TTL + CLOCK_SLACK
    world.hold(ask, secret["file_id"], "held", until=ceiling + 100)
    assert "deleted_expired" not in world.sweep(now=ceiling + 10)[1] and world.exists(secret["file_id"])
    assert world.sweep(now=ceiling + 101)[1]["deleted_expired"] == 1  # the lease ran out


def test_an_item_with_a_garbage_stamp_does_not_keep_its_file_for_ever_or_stop_the_others(world, caplog):
    ask = world.ask()
    bad, good = world.answered(ask, (1, True), (2, True))
    doc = world.doc(ask)
    doc["answer_files"][0]["uploaded_at"] = "not a time at all"
    world.drive.write(ask_file(world, ask)["id"], doc, {})
    world.hosted.app.state.pool._cache.clear()
    caplog.set_level(logging.WARNING)
    created = created_at_of(world, bad["file_id"])
    # the good one is due by its stamps, the bad one is not
    _, stats = world.sweep(now=world.asks[ask] + SECRET_TTL + 30)
    assert not world.exists(good["file_id"]) and world.exists(bad["file_id"])
    assert stats["deleted_expired"] == 1
    assert "stamps of item 1 of ask #" in caplog.text and "not a time at all" not in caplog.text
    assert world.sweep(now=created + SECRET_TTL + CLOCK_SLACK)[1]["deleted_expired"] == 1
    assert not world.exists(bad["file_id"])


def test_a_normal_item_flagged_secret_in_drive_keeps_its_30_days_when_its_ask_is_readable(world):
    ask = world.ask()
    file_id = world.put(ask, 1, uploaded=world.now - 60, secret=True)  # Drive's marks say secret, the item says not
    answered_by_files(world.drive, ask, [item(1, file_id=file_id, bytes=100, secret=False)])
    created = created_at_of(world, file_id)
    assert "deleted_expired" not in world.sweep(now=created + SECRET_TTL + 5 * HOUR)[1] and world.exists(file_id)


@pytest.mark.parametrize("ttl", [SECRET_TTL, NORMAL_TTL])
def test_the_ceiling_is_never_earlier_than_any_lifetime_the_rules_compute(ttl):
    ceiling = lambda created: created + ttl + CLOCK_SLACK  # noqa: E731
    created = 1_000_000.0
    for offset in (-400 * DAY, -3 * DAY, -CLOCK_SLACK - 1, -CLOCK_SLACK, -1, 0, 1, CLOCK_SLACK, 3 * DAY, 400 * DAY):
        assert lifetime_end(created + offset, created, ttl) <= ceiling(created), offset


class Cut:
    """A clock a slow Drive moves: each ask file read of the user takes `step` seconds."""

    def __init__(self, world, monkeypatch, step):
        self.t = 0.0
        drive = world.hosted.app.state.pool.client(world.sub)
        real = drive.download

        def slow(file_id):
            self.t += step
            return real(file_id)
        monkeypatch.setattr(drive, "download", slow)

    def __call__(self):
        return self.t


def test_a_slow_user_is_cut_and_the_next_user_is_swept_in_the_same_run(hosted, google, monkeypatch):
    first = Sweeper(hosted, google)
    second = Sweeper(hosted, google, sub=OLGA_SUB, email=OLGA_EMAIL)
    for _ in range(6):
        first.answered(first.ask(), (1, False))
    first.hosted.app.state.pool._cache.clear()
    theirs = stale_orphan(second)
    clock = Cut(first, monkeypatch, 50)
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time() + 3 * DAY, None, 10, deadline=120,
                          clock=clock, user_seconds=100)
    assert stats["user_cut"] == 1 and stats["users"] == 2  # the first user counts as done for this run
    assert cursor is None and not second.exists(theirs)  # and the second one was reached: the budget was not eaten


def test_a_user_who_hits_the_read_cap_is_cut_and_the_cursor_moves_past(hosted, google, monkeypatch):
    first = Sweeper(hosted, google)
    second = Sweeper(hosted, google, sub=OLGA_SUB, email=OLGA_EMAIL)
    for _ in range(5):
        first.answered(first.ask(), (1, False))
    hosted.app.state.pool._cache.clear()
    theirs = stale_orphan(second)
    monkeypatch.setattr(sweep_module, "USER_READS", 2)
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time() + 3 * DAY, None, 1)
    assert cursor == "1001" and stats["user_cut"] == 1  # past the user that was cut, the next call starts at the second
    cursor, stats = sweep(hosted.store, hosted.app.state.pool, time.time() + 3 * DAY, cursor, 1)
    assert cursor is None and not second.exists(theirs)


def test_a_user_with_no_files_and_no_claims_is_not_listed_for_asks(world):
    before = len([c for c in world.drive.calls("GET", "/drive/v3/files") if "contains 'ask-'" in c[2].get("q", "")])
    world.sweep()
    assert len([c for c in world.drive.calls("GET", "/drive/v3/files") if "contains 'ask-'" in c[2].get("q", "")]) == before


def test_a_user_with_only_a_claim_is_still_looked_at(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    del world.drive.files[one["file_id"]]
    world.hold(ask, one["file_id"], "done")
    before = len([c for c in world.drive.calls("GET", "/drive/v3/files") if "contains 'ask-'" in c[2].get("q", "")])
    world.sweep()
    assert len([c for c in world.drive.calls("GET", "/drive/v3/files") if "contains 'ask-'" in c[2].get("q", "")]) == before + 1


# ---------- fix round 3: I-1 (tail starvation), I-3 (secret drafts), M-1 (done claim), M-6 ----------

def closed_by_the_app(world, ask):
    app_action(world.drive, ask, "closed")
    world.hosted.app.state.pool._cache.clear()


def test_a_closed_ask_at_the_tail_is_swept_although_the_reads_ran_out_before_it(world, monkeypatch):
    live = [world.ask() for _ in range(3)]
    for ask in live:
        world.answered(ask, (1, False))
    last = world.ask()
    [tail] = world.answered(last, (1, False))
    closed_by_the_app(world, last)
    monkeypatch.setattr(sweep_module, "USER_READS", 2)
    _, stats = world.sweep(now=world.now + HOUR)
    assert stats["user_cut"] == 1  # two reads, a third was refused: the live asks behind the cap wait
    assert stats["deleted_closed"] == 1 and not world.exists(tail["file_id"])


def test_the_files_nearest_to_expiry_get_the_reads_first(world, monkeypatch):
    normals = [world.ask() for _ in range(4)]
    for ask in normals:
        world.answered(ask, (1, False))  # listed first, due in 30 days
    ask = world.ask()
    [secret] = world.answered(ask, (1, True))  # listed last, due now
    monkeypatch.setattr(sweep_module, "USER_READS", 2)
    _, stats = world.sweep(now=world.at(ask, SECRET_TTL + 60))
    assert stats["deleted_expired"] == 1 and not world.exists(secret["file_id"])


def test_a_ceiling_needs_no_read_at_all(world, monkeypatch):
    for _ in range(3):
        world.answered(world.ask(), (1, False))
    ask = world.ask()
    [old] = world.answered(ask, (1, False))
    world.drive.files[old["file_id"]]["createdTime"] = _iso_of(world.now - 31 * DAY)
    monkeypatch.setattr(sweep_module, "USER_READS", 0)  # not one ask file may be read
    world.sweep(now=world.now + 5 * 60)
    assert not world.exists(old["file_id"])


def test_a_done_claim_deletes_as_sent_before_any_read(world):
    ask = world.ask()
    [one] = world.answered(ask, (1, True))
    world.hold(ask, one["file_id"], "done")
    break_ask_file(world, ask, "corrupt")  # the ask can't be read at all
    _, stats = world.sweep(now=world.at(ask, 120))
    assert stats["deleted_sent"] == 1 and not world.exists(one["file_id"])


def test_a_secret_draft_of_an_open_ask_lives_to_its_24_h_ceiling(world):
    ask = world.ask()  # open: a draft, nothing sent yet
    secret = world.put(ask, 1, uploaded=world.now, secret=True)
    absent = world.put(99, 1, uploaded=world.now, secret=True)  # no such ask: a real orphan, 1 h
    assert world.sweep(now=world.now + 2 * HOUR)[1]["deleted_orphan"] == 1
    assert world.exists(secret) and not world.exists(absent)
    assert "deleted_expired" not in world.sweep(now=world.now + SECRET_TTL + CLOCK_SLACK - 1)[1]
    assert world.sweep(now=world.now + SECRET_TTL + CLOCK_SLACK)[1]["deleted_expired"] == 1
    assert not world.exists(secret)


@pytest.mark.parametrize("error", [OverflowError, OSError, ZeroDivisionError])
def test_any_error_reading_a_stamp_leaves_the_file_to_its_ceiling_and_the_rest_going(world, monkeypatch, error):
    ask = world.ask()
    bad, good = world.answered(ask, (1, True), (2, True))
    real = sweep_module._epoch

    def broken(stamp):
        if stamp == "poisoned":
            raise error("no such time")
        return real(stamp)
    monkeypatch.setattr(sweep_module, "_epoch", broken)
    doc = world.doc(ask)
    doc["answer_files"][0]["uploaded_at"] = "poisoned"
    world.drive.write(ask_file(world, ask)["id"], doc, {})
    world.hosted.app.state.pool._cache.clear()
    created = created_at_of(world, bad["file_id"])
    _, stats = world.sweep(now=world.asks[ask] + SECRET_TTL + 30)
    assert not world.exists(good["file_id"]) and world.exists(bad["file_id"]) and "failed" not in stats
    assert world.sweep(now=created + SECRET_TTL + CLOCK_SLACK)[1]["deleted_expired"] == 1


# ---------- fix round 3: I-4 (a sweep that can't keep up says so) ----------

def test_two_runs_in_a_row_with_work_left_say_the_sweep_is_falling_behind(google, monkeypatch, caplog):
    from server.hosted import app as app_module
    from server.hosted.config import HostedSettings
    from .hosted_support import id_token
    settings = HostedSettings(**{**settings_for().__dict__, "admin_emails": ("owner@example.com",),
                                 "sweep_service_account": SA})
    app, store = build(google, settings=settings)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.google = store, google
        users = three_users(client, google)
        for who in users:
            stale_orphan(who)
        mine = id_token(google, email="owner@example.com")
        monkeypatch.setattr(app_module, "SWEEP_BUDGET", 0.0)  # one slice per run: always work left
        caplog.set_level(logging.WARNING)
        shown = lambda: client.get("/api/admin/stats", headers=mine).json()["sweep"]  # noqa: E731
        assert client.post(URL, headers=oidc(google)).json()["next_cursor"] == "1001"  # the scheduler's bare POST
        assert "sweep falling behind" not in caplog.text
        assert shown()["more_in_a_row"] == 1 and shown()["behind"] is False
        # a run somebody starts by hand (an explicit cursor, or a limit) is not the scheduler: the count is not theirs
        assert client.post(f"{URL}?cursor=1001&limit=1", headers=oidc(google)).json()["next_cursor"] == "2002"
        assert shown()["more_in_a_row"] == 1 and "sweep falling behind" not in caplog.text
        assert client.post(URL, headers=oidc(google)).json()["next_cursor"] == "2002"
        assert "sweep falling behind" in caplog.text
        assert shown()["more_in_a_row"] == 2 and shown()["behind"] is True
        assert client.post(f"{URL}?limit=1", headers=oidc(google)).json()["next_cursor"] is None  # finishes, by hand
        assert shown()["more_in_a_row"] == 2 and shown()["behind"] is True  # a hand-made run does not reset it either
        monkeypatch.setattr(app_module, "SWEEP_BUDGET", 600.0)  # it catches up
        assert client.post(URL, headers=oidc(google)).json()["next_cursor"] is None  # the scheduler's run finishes
        assert shown()["more_in_a_row"] == 0 and shown()["behind"] is False and shown()["more"] is False


# ---------- fix round 4 ----------

def test_a_secret_file_the_answered_ask_does_not_name_is_a_draft_not_an_orphan_after_an_hour(world):
    """A re-answer draft: the app uploads on attach, before Send, so the answered ask doesn't name the file yet."""
    ask = world.ask()
    world.answered(ask, (1, False))
    draft = world.put(ask, 2, uploaded=world.at(ask, 0), secret=True)
    assert "deleted_orphan" not in world.sweep(now=world.now + 2 * HOUR)[1] and world.exists(draft)
    assert "deleted_expired" not in world.sweep(now=world.now + SECRET_TTL + CLOCK_SLACK - 1)[1]
    assert world.sweep(now=world.now + SECRET_TTL + 11 * 60)[1]["deleted_expired"] == 1
    assert not world.exists(draft)


def test_the_hour_for_an_unnamed_secret_file_is_for_an_absent_ask_only(world):
    absent = world.put(99, 1, uploaded=world.now, secret=True)
    ask = world.ask()
    open_ask = world.put(ask, 1, uploaded=world.now, secret=True)
    assert world.sweep(now=world.now + 2 * HOUR)[1]["deleted_orphan"] == 1
    assert not world.exists(absent) and world.exists(open_ask)


def test_claims_of_asks_closed_by_their_marks_go_before_the_reads_so_a_cut_user_still_gets_it(world, monkeypatch):
    live = [world.ask() for _ in range(3)]
    for ask in live:
        world.answered(ask, (1, False))
    closed = world.ask()
    [one] = world.answered(closed, (1, True))
    key = world.hold(closed, one["file_id"], "done")
    closed_by_the_app(world, closed)
    kept = world.hold(live[0], world.doc(live[0])["answer_files"][0]["file_id"], "held", until=world.now + 3600)
    monkeypatch.setattr(sweep_module, "USER_READS", 1)
    _, stats = world.sweep(now=world.now + 60)
    assert stats["user_cut"] == 1 and stats["claims_deleted"] >= 1
    assert world.hosted.store.get("fileclaims", key) is None and world.hosted.store.get("fileclaims", kept) is not None


def lagging_listing(world, ask, monkeypatch, meta_says: str):
    """The listing's appProperties say `closed` (an index that has not caught up) while the file's own metadata says
    `meta_says`."""
    file = ask_file(world, ask)
    file["appProperties"]["status"] = "closed"
    drive = world.hosted.app.state.pool.client(world.sub)
    real = drive.get_meta

    def meta(file_id):
        found = real(file_id)
        if file_id == file["id"] and found is not None:
            found = {**found, "appProperties": {**found["appProperties"], "status": meta_says}}
        return found
    monkeypatch.setattr(drive, "get_meta", meta)
    world.hosted.app.state.pool._cache.clear()


def test_a_closed_mark_in_the_listing_alone_does_not_delete_a_file(world, monkeypatch):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))  # answered, young: nothing deletes it
    lagging_listing(world, ask, monkeypatch, "answered")
    assert world.sweep(now=world.now + 60)[1] == {"users": 1} and world.exists(one["file_id"])


def test_a_closed_mark_the_files_own_metadata_confirms_deletes_without_a_read(world, monkeypatch):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    lagging_listing(world, ask, monkeypatch, "closed")
    _, stats = world.sweep(now=world.now + 60)
    assert stats["deleted_closed"] == 1 and not world.exists(one["file_id"])


def test_a_failed_confirmation_defers_to_the_read(world, monkeypatch):
    ask = world.ask()
    [one] = world.answered(ask, (1, False))
    closed_by_the_app(world, ask)
    world.drive.fail_next("GET", f"/drive/v3/files/{ask_file(world, ask)['id']}", 503)  # the confirming get_meta
    _, stats = world.sweep(now=world.now + 60)
    assert stats["deleted_closed"] == 1 and not world.exists(one["file_id"])  # the read says closed: same result


def test_the_falling_behind_count_survives_a_failed_read_of_the_last_run(google, monkeypatch):
    from server.hosted import app as app_module
    from server.hosted.config import HostedSettings
    settings = HostedSettings(**{**settings_for().__dict__, "sweep_service_account": SA})
    app, store = build(google, settings=settings)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.google = store, google
        store.put("sweep", "last_run", {"at": "2026-11-19T09:00:00.000Z", "stats": {}, "more": True, "more_in_a_row": 1})
        real = store.get
        monkeypatch.setattr(store, "get", lambda c, k: (_ for _ in ()).throw(RuntimeError("store down"))
                            if (c, k) == ("sweep", "last_run") else real(c, k))
        assert client.post(URL, headers=oidc(google)).json()["next_cursor"] is None  # finished: the count is reset
        monkeypatch.setattr(store, "get", real)
    assert store.get("sweep", "last_run")["more_in_a_row"] == 0
