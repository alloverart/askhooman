"""Drive mirror (SPEC.md "Drive storage"): the bridge against an in-memory Drive (tests/fake_drive.py)."""

import dataclasses
import json
import logging
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Settings
from server.drive import DriveClient, DriveError
from server.notify import answer_text, close_text, reopen_text

from .conftest import ALLEN, DENIS, auth
from .fake_drive import CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, FakeDrive, app_action, app_task_reply, make_client


@pytest.fixture
def fake() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def drive_files(tmp_dir):
    """The OAuth client file as Google Cloud console downloads it, and the refresh token file (SPEC "Drive storage")."""
    client_file = tmp_dir / "drive-client.json"
    client_file.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                                     "token_uri": "https://oauth2.googleapis.com/token"}}))
    token_file = tmp_dir / "drive-token.json"
    token_file.write_text(json.dumps({"refresh_token": REFRESH_TOKEN, "scope": "x", "obtained_at": "2026-10-31"}))
    return client_file, token_file


@pytest.fixture
def drive_client(fake, drive_files) -> DriveClient:
    return make_client(fake, *drive_files)


@pytest.fixture
def drive_settings(settings, drive_files) -> Settings:
    # A long poll: only the startup sync and write-triggered pushes run in the background; tests force
    # full syncs with sync() so what they check does not depend on timing.
    return dataclasses.replace(settings, drive=True, drive_client_file=drive_files[0],
                               drive_token_file=drive_files[1], drive_poll=3600)


@pytest.fixture
def dclient(drive_settings, notifier, drive_client):
    with TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as test_client:
        yield test_client


def sync(client) -> None:
    client.app.state.mirror.sync_once()


def create(client, token=ALLEN, **fields) -> dict:
    fields.setdefault("title", "Делаем?")
    response = client.post("/api/asks", json=fields, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


def get(client, ask_id) -> dict:
    return client.get(f"/api/asks/{ask_id}", headers=auth(DENIS)).json()


def assert_mirrored(client, fake, ask_id, rev=None) -> dict:
    """The file holds exactly what REST returns (ask + events), written by the bridge."""
    ask = get(client, ask_id)
    file = fake.file(f"ask-{ask_id}.json")
    doc = json.loads(file["content"])
    assert {k: v for k, v in doc.items() if k not in ("rev", "schema", "writer")} == ask
    assert (doc["schema"], doc["writer"]) == (1, "bridge")
    assert file["appProperties"] == {"ask_id": str(ask_id), "status": ask["status"], "agent": ask["agent"],
                                     "rev": str(doc["rev"])}
    assert (file["mimeType"], file["parents"]) == ("application/json", ["appDataFolder"])
    if rev is not None:
        assert doc["rev"] == rev
    return doc


def writes(fake) -> list:
    return fake.calls("POST", "/upload/") + fake.calls("PATCH")


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


# ---------- off by default ----------

def test_drive_off_changes_nothing(settings, notifier, drive_client, fake):
    assert Settings.from_env({}).drive is False
    app = create_app(settings, notifier=notifier, drive=drive_client)
    assert app.state.mirror is None and app.state.service.mirror is None
    with TestClient(app) as c:
        ask = create(c)
        assert c.post(f"/api/asks/{ask['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS)).status_code == 200
    assert fake.requests == []
    tables = {r[0] for r in sqlite3.connect(settings.db_path).execute("SELECT name FROM sqlite_master")}
    assert "drive_files" not in tables


def test_settings_from_env(tmp_dir):
    s = Settings.from_env({"ASKS_DRIVE": "1", "ASKS_DRIVE_CLIENT": "~/c.json", "ASKS_DRIVE_TOKEN": "/x/t.json",
                           "ASKS_DRIVE_POLL": "0.5"})
    assert s.drive and s.drive_client_file.name == "c.json" and str(s.drive_token_file) == "/x/t.json"
    assert s.drive_poll == 0.5
    assert Settings.from_env({"ASKS_DRIVE_POLL": "soon"}).drive_poll == 15.0
    assert Settings.from_env({"ASKS_DRIVE_POLL": "-1"}).drive_poll == 15.0


# ---------- bridge -> Drive ----------

def test_migration_pushes_every_existing_ask(settings, drive_settings, notifier, drive_client, fake):
    with TestClient(create_app(settings, notifier=notifier)) as c:  # v1: no mirror
        a, b, d = create(c, title="Первый?"), create(c, title="Второй?", options=["Да"]), create(c, title="Третий?")
        c.post(f"/api/asks/{b['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS))
        c.post(f"/api/asks/{d['id']}/close", json={"resolution": "не нужно"}, headers=auth(ALLEN))
    with TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        sync(c)
        for ask in (a, b, d):
            assert_mirrored(c, fake, ask["id"], rev=1)
        assert len(fake.files) == 3
        assert fake.doc(b["id"])["status"] == "answered" and fake.doc(d["id"])["resolution"] == "не нужно"
        # nothing left to do: a second full sync lists the folder and writes nothing
        fake.requests.clear()
        sync(c)
        assert [r[0:2] for r in fake.requests] == [("GET", "/drive/v3/files")]
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT ask_id, rev FROM drive_files ORDER BY ask_id").fetchall() == [
            (a["id"], 1), (b["id"], 1), (d["id"], 1)]


def test_agent_writes_are_mirrored(dclient, fake):
    ask = create(dclient, title="Шить M?", options=["Да", "Нет"], ref="TG 1")
    sync(dclient)
    assert_mirrored(dclient, fake, ask["id"], rev=1)
    dclient.patch(f"/api/asks/{ask['id']}", json={"title": "Шить L?", "priority": "urgent"}, headers=auth(ALLEN))
    sync(dclient)
    doc = assert_mirrored(dclient, fake, ask["id"], rev=2)
    assert (doc["title"], doc["priority"], doc["events"][-1]["kind"]) == ("Шить L?", "urgent", "edited")
    dclient.post(f"/api/asks/{ask['id']}/close", json={"resolution": "сшили"}, headers=auth(ALLEN))
    sync(dclient)
    doc = assert_mirrored(dclient, fake, ask["id"], rev=3)
    assert (doc["status"], doc["closed_by"]) == ("closed", "allen")
    assert len(fake.files) == 1


def test_writes_are_pushed_by_the_background_worker(drive_settings, notifier, drive_client, fake):
    """No manual sync: a REST write reaches Drive, and an app answer comes back, within a few polls."""
    settings = dataclasses.replace(drive_settings, drive_poll=0.05)
    with TestClient(create_app(settings, notifier=notifier, drive=drive_client)) as c:
        ask = create(c)
        wait_for(lambda: fake.named(f"ask-{ask['id']}.json"))
        wait_for(lambda: fake.doc(ask["id"])["writer"] == "bridge")
        app_action(fake, ask["id"], "answered", answer="Да")
        wait_for(lambda: get(c, ask["id"])["status"] == "answered")
        wait_for(lambda: fake.doc(ask["id"])["writer"] == "bridge")
        assert_mirrored(c, fake, ask["id"], rev=3)
        assert notifier.flush()


def test_push_checks_the_remote_rev_first(dclient, fake, sender, notifier):
    ask = create(dclient)
    sync(dclient)
    file_id = fake.file(f"ask-{ask['id']}.json")["id"]
    app_action(fake, ask["id"], "closed")  # rev 2 by the app; the bridge doesn't know yet
    fake.requests.clear()
    # the agent edits before the bridge's next poll: the push must read the app's close first
    assert dclient.patch(f"/api/asks/{ask['id']}", json={"ref": "TG 2"}, headers=auth(ALLEN)).status_code == 200
    dclient.app.state.mirror.push_pending()
    assert [(m, p, q.get("alt")) for m, p, q in fake.requests] == [
        ("GET", f"/drive/v3/files/{file_id}", None),  # metadata: appProperties.rev
        ("GET", f"/drive/v3/files/{file_id}", "media"),  # newer than seen: pull + replay
        ("PATCH", f"/upload/drive/v3/files/{file_id}", None),
    ]
    doc = assert_mirrored(dclient, fake, ask["id"], rev=3)  # remote rev + 1
    assert (doc["status"], doc["closed_by"], doc["ref"]) == ("closed", "denis", "TG 2")
    assert [e["kind"] for e in doc["events"]] == ["created", "edited", "closed"]
    assert notifier.flush() and len(sender.sent) == 1


# ---------- app -> bridge (replay) ----------

def test_app_answer_is_replayed_as_denis(dclient, fake, sender, notifier):
    ask = create(dclient, title="Шить M?")
    sync(dclient)
    app_action(fake, ask["id"], "answered", answer="Да, шей")
    assert fake.doc(ask["id"])["writer"] == "app"
    sync(dclient)
    replayed = get(dclient, ask["id"])
    assert (replayed["status"], replayed["answer"]) == ("answered", "Да, шей")
    assert [(e["kind"], e["actor"]) for e in replayed["events"]] == [("created", "allen"), ("answered", "denis")]
    assert replayed["events"][-1]["payload"] == {"answer": "Да, шей", "previous_answer": None}
    assert notifier.flush()
    assert sender.texts == [answer_text(replayed)]
    assert_mirrored(dclient, fake, ask["id"], rev=3)  # app rev 2 + 1: the file converged to the bridge
    # replayed once: further syncs change nothing and ping nobody
    fake.requests.clear()
    sync(dclient)
    sync(dclient)
    assert writes(fake) == [] and notifier.flush() and len(sender.sent) == 1


def test_app_close_of_open_ask_is_not_relevant(dclient, fake, sender, notifier):
    ask = create(dclient, title="Ярлык?")
    sync(dclient)
    app_action(fake, ask["id"], "closed")
    sync(dclient)
    closed = get(dclient, ask["id"])
    assert (closed["status"], closed["closed_by"], closed["resolution"]) == ("closed", "denis", "Not relevant (Denis)")
    assert closed["events"][-1]["payload"] == {"resolution": "Not relevant (Denis)", "previous_status": "open"}
    assert notifier.flush() and sender.texts == [close_text(closed)]
    assert_mirrored(dclient, fake, ask["id"], rev=3)


def test_app_reopen_is_replayed(dclient, fake, sender, notifier):
    ask = create(dclient, title="Шить M?")
    dclient.post(f"/api/asks/{ask['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS))  # via REST
    sync(dclient)
    rev = fake.doc(ask["id"])["rev"]
    app_action(fake, ask["id"], "reopened")  # «Undo» in the app
    sync(dclient)
    reopened = get(dclient, ask["id"])
    assert (reopened["status"], reopened["answer"], reopened["answered_at"]) == ("open", None, None)
    assert reopened["events"][-1]["payload"]["previous_status"] == "answered"
    assert reopened["events"][-1]["payload"]["answer"] == "Да"
    assert notifier.flush()
    assert sender.texts[-1] == reopen_text(reopened, "answered") and len(sender.sent) == 2
    assert_mirrored(dclient, fake, ask["id"], rev=rev + 2)


def test_invalid_replay_is_skipped_and_logged(dclient, fake, sender, notifier, caplog):
    ask = create(dclient)
    sync(dclient)
    fake.down = True  # the bridge can't reach Drive, the phone can
    assert dclient.post(f"/api/asks/{ask['id']}/close", json={"resolution": "сама решила"},
                        headers=auth(ALLEN)).status_code == 200
    with pytest.raises(DriveError):
        sync(dclient)
    fake.down = False
    app_action(fake, ask["id"], "answered", answer="Да")  # the app still saw it open
    with caplog.at_level(logging.INFO, logger="asks.drive"):
        sync(dclient)
    assert "skipped the human's answered of ask #1" in caplog.text and "is closed" in caplog.text
    after = get(dclient, ask["id"])
    assert (after["status"], after["closed_by"], after["answer"]) == ("closed", "allen", None)
    assert "answered" not in [e["kind"] for e in after["events"]]
    assert_mirrored(dclient, fake, ask["id"], rev=3)  # the app's answer is overwritten by the bridge's state
    assert notifier.flush() and sender.sent == []
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="asks.drive"):
        sync(dclient)
    assert "skipped" not in caplog.text  # tried once only


def test_two_writers_with_the_same_rev_converge(dclient, fake, sender, notifier):
    """The app wrote rev 2 from a stale copy while the bridge also wrote rev 2: modifiedTime gives it away."""
    ask = create(dclient, title="Старый?")
    sync(dclient)
    stale = fake.doc(ask["id"])
    dclient.patch(f"/api/asks/{ask['id']}", json={"title": "Новый?"}, headers=auth(ALLEN))
    sync(dclient)
    assert fake.doc(ask["id"])["rev"] == 2
    assert app_action(fake, ask["id"], "answered", answer="Да", base=stale)["rev"] == 2
    sync(dclient)
    after = get(dclient, ask["id"])
    assert (after["title"], after["status"], after["answer"]) == ("Новый?", "answered", "Да")
    assert_mirrored(dclient, fake, ask["id"], rev=3)
    assert notifier.flush() and len(sender.sent) == 1


def test_unknown_top_level_fields_are_kept(dclient, fake):
    ask = create(dclient)
    sync(dclient)
    app_action(fake, ask["id"], "answered", answer="Да", extra={"app_note": {"pinned": True}})
    sync(dclient)
    doc = fake.doc(ask["id"])
    assert (doc["writer"], doc["status"], doc["app_note"]) == ("bridge", "answered", {"pinned": True})
    dclient.post(f"/api/asks/{ask['id']}/close", json={"resolution": "ok"}, headers=auth(ALLEN))
    sync(dclient)
    assert fake.doc(ask["id"])["app_note"] == {"pinned": True}


# ---------- adoption, foreign and missing files ----------

def _existing_ask(settings, notifier) -> dict:
    with TestClient(create_app(settings, notifier=notifier)) as c:
        ask = create(c, title="Был до Drive?")
        return get(c, ask["id"])


def test_adopts_an_existing_file_instead_of_creating_one(settings, drive_settings, notifier, drive_client, fake,
                                                         sender):
    ask = _existing_ask(settings, notifier)
    fake.add("ask-1.json", {**ask, "rev": 4, "schema": 1, "writer": "bridge"},
             {"ask_id": "1", "status": "open", "agent": "allen", "rev": "4"})
    file_id = fake.file("ask-1.json")["id"]
    app_action(fake, 1, "answered", answer="Да")  # the human answered while the mapping was lost
    with TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        sync(c)
        assert get(c, 1)["status"] == "answered"
        assert_mirrored(c, fake, 1, rev=6)
    assert fake.calls("POST", "/upload/") == [] and fake.file("ask-1.json")["id"] == file_id
    assert notifier.flush() and len(sender.sent) == 1


def test_adopting_an_identical_file_writes_nothing(settings, drive_settings, notifier, drive_client, fake):
    ask = _existing_ask(settings, notifier)
    fake.add("ask-1.json", {**ask, "rev": 3, "schema": 1, "writer": "bridge"},
             {"ask_id": "1", "status": "open", "agent": "allen", "rev": "3"})
    with TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        sync(c)
        sync(c)
        assert_mirrored(c, fake, 1, rev=3)
    assert writes(fake) == []


def test_file_of_another_ask_is_overwritten_without_replay(settings, drive_settings, notifier, drive_client, fake,
                                                           sender, caplog):
    ask = _existing_ask(settings, notifier)
    other = {**ask, "title": "Чужой?", "created_at": "2026-01-01T00:00:00.000Z", "rev": 7, "schema": 1,
             "writer": "bridge"}
    fake.add("ask-1.json", other, {"ask_id": "1", "status": "open", "agent": "allen", "rev": "7"})
    app_action(fake, 1, "answered", answer="Да")
    with caplog.at_level(logging.WARNING, logger="asks.drive"), \
            TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        sync(c)
        assert get(c, 1)["status"] == "open"
        assert_mirrored(c, fake, 1, rev=9)
    assert "holds another ask" in caplog.text
    assert notifier.flush() and sender.sent == []


def test_unknown_and_orphan_files_are_ignored(dclient, fake, caplog):
    fake.page_size = 2  # forces several pages
    notes = fake.add("notes.txt", b"hello", {"x": "1"})
    odd = fake.add("ask-x.json", {"x": 1})
    orphan = fake.add("ask-99.json", {"id": 99}, {"rev": "5"})
    elsewhere = fake.add("ask-1.json", {"id": 1}, {"rev": "9"}, parents=("root",))  # not in appDataFolder
    before = {fid: dict(f) for fid, f in fake.files.items()}
    asks = [create(dclient, title=f"Вопрос {i}?") for i in range(3)]
    with caplog.at_level(logging.WARNING, logger="asks.drive"):
        sync(dclient)
        sync(dclient)
    for ask in asks:
        assert_mirrored(dclient, fake, ask["id"], rev=1)
    for fid in (notes, odd, orphan, elsewhere):
        assert fake.files[fid] == before[fid]
    assert caplog.text.count("ask-99.json has no ask #99") == 1
    assert any("pageToken" in q for _, _, q in fake.calls("GET", "/drive/v3/files"))
    assert all(m in ("GET", "POST", "PATCH") for m, _, _ in fake.requests)  # nothing is ever deleted


def test_a_file_that_disappeared_is_written_again(dclient, fake):
    ask = create(dclient)
    sync(dclient)
    with fake.lock:
        del fake.files[fake.file(f"ask-{ask['id']}.json")["id"]]
    dclient.patch(f"/api/asks/{ask['id']}", json={"ref": "TG 3"}, headers=auth(ALLEN))
    sync(dclient)
    assert assert_mirrored(dclient, fake, ask["id"])["ref"] == "TG 3"


# ---------- Drive trouble never reaches the API ----------

def test_drive_down_the_api_works_and_the_mirror_catches_up(drive_settings, notifier, drive_client, fake, caplog):
    fake.down = True
    with caplog.at_level(logging.WARNING, logger="asks.drive"), \
            TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        started = time.monotonic()
        ask = create(c, title="Пока Drive лежит?")
        assert c.patch(f"/api/asks/{ask['id']}", json={"ref": "TG 9"}, headers=auth(ALLEN)).status_code == 200
        assert c.post(f"/api/asks/{ask['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS)).status_code == 200
        assert time.monotonic() - started < 2
        wait_for(lambda: "Drive sync failed" in caplog.text)
        with pytest.raises(DriveError) as raised:
            sync(c)
        assert raised.value.transient
        fake.down = False
        sync(c)
        doc = assert_mirrored(c, fake, ask["id"], rev=1)
        assert (doc["status"], doc["ref"]) == ("answered", "TG 9")
    for secret in (REFRESH_TOKEN, CLIENT_SECRET, "ya29."):
        assert secret not in caplog.text


def test_missing_token_is_a_startup_warning_not_a_failure(drive_settings, notifier, drive_client, drive_files,
                                                          fake, caplog):
    drive_files[1].unlink()
    with caplog.at_level(logging.WARNING), \
            TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        assert create(c)["id"] == 1
        assert "Drive mirror is on but cannot sync" in caplog.text and "link the Google account" in caplog.text
        drive_files[1].write_text(json.dumps({"refresh_token": REFRESH_TOKEN}))  # linked later: no restart
        sync(c)
        assert_mirrored(c, fake, 1, rev=1)


# ---------- the Drive client itself ----------

def test_access_token_is_cached_and_a_401_refreshes_it_once(fake, drive_client):
    drive_client.list_files()
    drive_client.list_files()
    assert fake.token_calls == 1
    fake.valid_tokens.clear()  # Google revoked the access token early
    assert drive_client.list_files() == []
    assert fake.token_calls == 2
    assert [p for _, p, _ in fake.requests].count("/token") == 2


def test_revoked_refresh_token_is_reported_without_secrets(fake, drive_client):
    fake.refresh_error = (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(DriveError) as raised:
        drive_client.list_files()
    message = str(raised.value)
    assert not raised.value.transient and "invalid_grant" in message and "link the Google account again" in message
    assert REFRESH_TOKEN not in message and CLIENT_SECRET not in message


def test_client_file_formats(tmp_dir, fake, drive_files):
    flat = tmp_dir / "flat.json"
    flat.write_text(json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}))
    assert make_client(fake, flat, drive_files[1]).config_problem() is None
    broken = tmp_dir / "broken.json"
    broken.write_text(json.dumps({"installed": {"client_id": CLIENT_ID}}))
    assert "client_secret" in make_client(fake, broken, drive_files[1]).config_problem()
    assert "ASKS_DRIVE_CLIENT is not set" in make_client(fake, None, drive_files[1]).config_problem()


# ---------- tasks (v3): task-<id>.json, written only by the bridge ----------

def create_task(client, token=ALLEN, **fields) -> dict:
    fields.setdefault("title", "Get the supplier to confirm the fabric")
    response = client.post("/api/tasks", json=fields, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


def get_task(client, task_id) -> dict:
    return client.get(f"/api/tasks/{task_id}", headers=auth(DENIS)).json()


def assert_task_mirrored(client, fake, task_id, rev=None, kept=()) -> dict:
    """The file holds exactly what REST returns (task + events), plus rev/schema/kind/writer (and `kept`
    top-level fields someone else added), minus the derived reply / reply_pending, which are never stored."""
    task = get_task(client, task_id)
    file = fake.file(f"task-{task_id}.json")
    doc = json.loads(file["content"])
    assert "reply" not in doc and "reply_pending" not in doc
    task = {k: v for k, v in task.items() if k not in ("reply", "reply_pending")}
    assert {k: v for k, v in doc.items() if k not in ("rev", "schema", "kind", "writer", *kept)} == task
    assert (doc["schema"], doc["kind"], doc["writer"]) == (1, "task", "bridge")
    assert file["appProperties"] == {"task_id": str(task_id), "status": task["status"], "agent": task["agent"],
                                     "rev": str(doc["rev"])}
    assert (file["mimeType"], file["parents"]) == ("application/json", ["appDataFolder"])
    if rev is not None:
        assert doc["rev"] == rev
    return doc


def test_task_writes_are_mirrored_next_to_asks(dclient, fake, sender, notifier):
    ask = create(dclient, title="Шить M?")
    task = create_task(dclient, ref="TG 5591")
    assert ask["id"] == task["id"] == 1
    sync(dclient)
    assert_mirrored(dclient, fake, 1, rev=1)  # the ask file gets no "kind"
    assert_task_mirrored(dclient, fake, 1, rev=1)
    dclient.patch(f"/api/tasks/{task['id']}", json={"status": "waiting", "waiting_on": "supplier", "next_check": "2026-11-03"},
                  headers=auth(ALLEN))
    sync(dclient)
    doc = assert_task_mirrored(dclient, fake, 1, rev=2)
    assert (doc["status"], doc["waiting_on"], doc["events"][-1]["kind"]) == ("waiting", "supplier", "status")
    dclient.post(f"/api/tasks/{task['id']}/done", json={"resolution": "accepted"}, headers=auth(ALLEN))
    dclient.app.state.mirror.push_pending()
    assert assert_task_mirrored(dclient, fake, 1, rev=3)["status"] == "done"
    dclient.post(f"/api/tasks/{task['id']}/reopen", headers=auth(ALLEN))
    sync(dclient)
    assert assert_task_mirrored(dclient, fake, 1, rev=4)["finished_at"] is None
    assert_mirrored(dclient, fake, 1, rev=1)
    assert len(fake.files) == 2
    assert notifier.flush() and sender.sent == []
    with sqlite3.connect(dclient.app.state.settings.db_path) as conn:
        assert conn.execute("SELECT task_id, rev FROM drive_task_files").fetchall() == [(1, 4)]
        assert conn.execute("SELECT ask_id, rev FROM drive_files").fetchall() == [(1, 1)]


def test_task_writes_are_pushed_by_the_background_worker(drive_settings, notifier, drive_client, fake):
    settings = dataclasses.replace(drive_settings, drive_poll=0.05)
    with TestClient(create_app(settings, notifier=notifier, drive=drive_client)) as c:
        task = create_task(c)
        wait_for(lambda: fake.named(f"task-{task['id']}.json"))
        c.post(f"/api/tasks/{task['id']}/drop", json={"resolution": "moot"}, headers=auth(ALLEN))
        wait_for(lambda: fake.task_doc(task["id"])["status"] == "dropped")
        assert_task_mirrored(c, fake, task["id"], rev=2)


def test_v2_database_with_mapped_asks_gets_task_files(settings, drive_settings, notifier, drive_client, fake):
    """A v2 bridge database (drive_files filled, no task tables) after the v3 upgrade: the asks' mapping
    keeps working as is (no ask file is rewritten) and the tasks are pushed on the first sync."""
    with TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:  # v2 era
        a, b = create(c, title="Первый?"), create(c, title="Второй?")
        sync(c)
    with sqlite3.connect(settings.db_path) as conn:
        conn.executescript("DROP TABLE drive_task_files; DROP TABLE task_events; DROP TABLE tasks;")
        mapping = conn.execute("SELECT * FROM drive_files ORDER BY ask_id").fetchall()
    with TestClient(create_app(settings, notifier=notifier)) as c:  # tasks created while the mirror was off
        t1 = create_task(c, title="First task")
        t2 = create_task(c, title="Second task")
        c.post(f"/api/tasks/{t2['id']}/done", headers=auth(ALLEN))
    ask_files = {f["id"]: dict(f) for f in fake.files.values()}
    fake.requests.clear()
    with TestClient(create_app(drive_settings, notifier=notifier, drive=drive_client)) as c:
        sync(c)
        for task in (t1, t2):
            assert_task_mirrored(c, fake, task["id"], rev=1)
        for ask in (a, b):
            assert_mirrored(c, fake, ask["id"], rev=1)
        assert fake.calls("PATCH") == [] and len(fake.calls("POST", "/upload/")) == 2  # only the two task files
        for file_id, before in ask_files.items():
            assert fake.files[file_id] == before
        fake.requests.clear()
        sync(c)
        assert [r[0:2] for r in fake.requests] == [("GET", "/drive/v3/files")]
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT * FROM drive_files ORDER BY ask_id").fetchall() == mapping
        assert conn.execute("SELECT task_id, rev FROM drive_task_files ORDER BY task_id").fetchall() == [(1, 1), (2, 1)]


def test_task_file_changed_outside_the_bridge_is_overwritten(dclient, fake, sender, notifier, caplog):
    task = create_task(dclient, title="Ours")
    sync(dclient)
    file = fake.file("task-1.json")
    doc = {**json.loads(file["content"]), "title": "Theirs", "status": "done", "rev": 5, "writer": "app",
           "note": {"keep": True}}
    fake.write(file["id"], doc, {"task_id": "1", "status": "done", "agent": "allen", "rev": "5"})
    with caplog.at_level(logging.WARNING, logger="asks.drive"):
        sync(dclient)
    assert "task-1.json was changed outside the bridge (rev 5, writer app): overwriting it" in caplog.text
    after = get_task(dclient, task["id"])
    assert (after["title"], after["status"], len(after["events"])) == ("Ours", "active", 1)  # nothing replayed
    doc = assert_task_mirrored(dclient, fake, 1, rev=6, kept=("note",))
    assert doc["note"] == {"keep": True}  # unknown top-level fields are kept, as in ask files
    assert notifier.flush() and sender.sent == []
    fake.requests.clear()
    sync(dclient)
    assert writes(fake) == []


def test_task_files_do_not_disturb_ask_replay(dclient, fake, sender, notifier, caplog):
    ask = create(dclient, title="Шить M?")
    task = create_task(dclient)
    orphan = fake.add("task-99.json", {"id": 99}, {"rev": "3"})
    odd = fake.add("task-x.json", {"x": 1})
    elsewhere = fake.add("task-1.json", {"id": 1}, {"rev": "9"}, parents=("root",))
    before = {fid: dict(fake.files[fid]) for fid in (orphan, odd, elsewhere)}
    with caplog.at_level(logging.WARNING, logger="asks.drive"):
        sync(dclient)
        task_file = dict(fake.file("task-1.json"))
        app_action(fake, ask["id"], "answered", answer="Да, шей")
        sync(dclient)
        sync(dclient)
    replayed = get(dclient, ask["id"])
    assert (replayed["status"], replayed["answer"]) == ("answered", "Да, шей")
    assert notifier.flush() and sender.texts == [answer_text(replayed)]
    assert_mirrored(dclient, fake, ask["id"], rev=3)
    assert fake.file("task-1.json") == task_file  # untouched by the ask's replay
    assert_task_mirrored(dclient, fake, task["id"], rev=1)
    for fid, file in before.items():
        assert fake.files[fid] == file
    assert caplog.text.count("task-99.json has no task #99") == 1


def test_task_file_adopted_or_written_again(dclient, fake):
    task = create_task(dclient)
    sync(dclient)
    file_id = fake.file("task-1.json")["id"]
    with sqlite3.connect(dclient.app.state.settings.db_path) as conn:
        conn.execute("DELETE FROM drive_task_files")  # the mapping is lost: the file is adopted, not duplicated
    fake.requests.clear()
    sync(dclient)
    assert writes(fake) == [] and fake.file("task-1.json")["id"] == file_id
    dclient.patch(f"/api/tasks/{task['id']}", json={"ref": "TG 1"}, headers=auth(ALLEN))
    sync(dclient)
    assert assert_task_mirrored(dclient, fake, 1, rev=2)["ref"] == "TG 1"
    with fake.lock:
        del fake.files[file_id]  # gone from Drive: written again as a new file
    dclient.patch(f"/api/tasks/{task['id']}", json={"ref": "TG 2"}, headers=auth(ALLEN))
    sync(dclient)
    doc = assert_task_mirrored(dclient, fake, 1, rev=1)
    assert doc["ref"] == "TG 2" and fake.file("task-1.json")["id"] != file_id


# ---------- replies on tasks from the app (SPEC v5) ----------

def test_app_task_reply_is_replayed_once(dclient, fake, sender, notifier, caplog):
    task = create_task(dclient, title="Chase the supplier")
    sync(dclient)
    app_task_reply(fake, task["id"], "What's the status?\nany news", "status")
    with caplog.at_level(logging.WARNING, logger="asks.drive"):
        sync(dclient)
    assert "changed outside the bridge" not in caplog.text
    after = get_task(dclient, task["id"])
    replied = after["events"][-1]
    assert (replied["kind"], replied["actor"], replied["payload"]) == (
        "replied", "denis", {"text": "What's the status?\nany news", "preset": "status"})
    assert after["reply_pending"] is True and after["status"] == "active"
    doc = assert_task_mirrored(dclient, fake, task["id"])   # pushed back with the bridge's event id
    assert [e["kind"] for e in doc["events"]] == ["created", "replied"]
    fake.requests.clear()
    sync(dclient)                                            # the same reply again: nothing applied or written
    assert writes(fake) == [] and len(get_task(dclient, task["id"])["events"]) == 2
    assert sender.sent == []


def test_app_task_reply_on_a_task_finished_meanwhile_is_skipped(dclient, fake, sender, notifier, caplog):
    task = create_task(dclient, title="Chase the supplier")
    sync(dclient)
    stale = json.loads(fake.file("task-1.json")["content"])
    assert dclient.post(f"/api/tasks/{task['id']}/done", json={}, headers=auth(ALLEN)).status_code == 200
    sync(dclient)
    app_task_reply(fake, task["id"], "Is this still needed?", "still-needed", base=stale)  # an old copy
    with caplog.at_level(logging.INFO, logger="asks.drive"):
        sync(dclient)
    assert "skipped the human's replied of task #1" in caplog.text
    after = get_task(dclient, task["id"])
    assert after["status"] == "done" and [e["kind"] for e in after["events"]] == ["created", "done"]
    doc = assert_task_mirrored(dclient, fake, task["id"])   # rewritten from SQLite
    assert doc["status"] == "done"
