"""Hosted bridge: voice answers (SPEC v5 "5. Voice answers"). The apps write `answer_voice` into the ask file next to
the answer; the bridge checks it on every read, drops a bad or stale one on its next write, keeps it without the file on
close, clears it on reopen and deletes the recording when an agent closes or reopens. FakeGoogle's Drive plays the
user's Drive; `answered_by_voice` plays the apps' send (SPEC «Sending»)."""

import json
import logging

import pytest

from server.hosted.records import HostedAsks
from server.rules import Change, Invalid, plan_answer
from server.tokens import Identity

from .conftest import ALLEN as ALLEN_TOKEN
from .conftest import DENIS as DENIS_TOKEN
from .conftest import auth
from .fake_drive import app_action, now_iso
from .hosted_support import DENIS_SUB, connect, mcp, register, tool_result

ALLEN = Identity("agent", "allen")
DENIS = Identity("denis")
MB = 1024 * 1024
AUDIO = b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00M4A mp42isom" + bytes(range(256)) * 8  # the bytes are not looked at
VOICE_KEYS = ("file_id", "mime", "bytes", "duration_ms", "lang", "transcript_source", "transcript", "answered_at",
              "deleted_at")


def agent(hosted, google, name: str = "Allen") -> str:
    """An agent connected to Denis's account: its access token."""
    return connect(hosted, google, register(hosted)["client_id"], agent=name)["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def asks_of(hosted) -> HostedAsks:
    """Denis's asks as the bridge reads them (no agent: no links)."""
    return HostedAsks(hosted.app.state.pool.records(DENIS_SUB))


def stored(voice: dict | None) -> dict | None:
    """answer_voice without what the bridge adds for the reader (url, url_expires_at)."""
    return None if voice is None else {key: voice[key] for key in VOICE_KEYS}


def voice_files(drive) -> list[str]:
    return sorted(f["name"] for f in drive.files.values() if f["name"].startswith("voice-"))


def answered_by_voice(drive, ask_id: int, text: str = "Yes, ship it", transcript: str | None = "yes ship it",
                      source: str = "on_device", raw: bytes = AUDIO, mime: str = "audio/mp4",
                      drive_mime: str | None = None, **changes) -> dict:
    """What the apps do (SPEC «Sending»): 1) the recording into appDataFolder as voice-<ask id>-<unix ms>.<ext> with
    appProperties ask_id and kind "voice"; 2) the answer, answer_voice and the `answered` event in one write. Returns
    the answer_voice written (`changes` replace its fields, e.g. a stale answered_at)."""
    at = now_iso()
    ext = "m4a" if mime == "audio/mp4" else "webm"
    file_id = drive.add(f"voice-{ask_id}-{len(drive.files) + 1700000000000}.{ext}", raw,
                        {"ask_id": str(ask_id), "kind": "voice"}, mime=drive_mime or mime)
    file = drive.file(f"ask-{ask_id}.json")
    doc = json.loads(file["content"])
    voice = {"file_id": file_id, "mime": mime, "bytes": len(raw), "duration_ms": 42000, "lang": "ru-RU",
             "transcript_source": source, "transcript": transcript if source == "on_device" else None,
             "answered_at": at, "deleted_at": None, **changes}
    payload = {"answer": text, "previous_answer": doc.get("answer"), "voice": voice,
               "previous_voice": doc.get("answer_voice")}
    doc.update(status="answered", answer=text, answered_at=at, updated_at=at, answer_voice=voice)
    doc["events"].append({"id": max(e["id"] for e in doc["events"]) + 1, "ask_id": ask_id, "at": at,
                          "actor": "denis", "kind": "answered", "payload": payload})
    doc["rev"] += 1
    doc["writer"] = "app"
    drive.write(file["id"], doc, {"ask_id": str(ask_id), "status": "answered", "agent": doc["agent"],
                                  "rev": str(doc["rev"])})
    return voice


def rewrite(drive, ask_id: int, **fields) -> None:
    """Change top-level fields of the ask file, as any client of the user's Drive could."""
    file = drive.file(f"ask-{ask_id}.json")
    doc = json.loads(file["content"])
    doc.update(fields)
    drive.write(file["id"], doc, {})


# ---------- the data model ----------

def test_answer_voice_validation(hosted, google):
    """The apps write answer_voice into the user's own file, so the bridge checks it on every read: the shape, each
    field's type and caps, an unknown transcript_source. A bad one reads as null; the transcript is cleaned like every
    text field."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    voice = answered_by_voice(drive, 1)
    asks = asks_of(hosted)
    assert stored(asks.get(1)["answer_voice"]) == voice
    assert list(asks.get(1)["answer_voice"])[:9] == list(VOICE_KEYS)

    def read(**changes):
        rewrite(drive, 1, answer_voice={**voice, **changes})
        found = asks.get(1)["answer_voice"]
        listed = asks.list("all")["asks"][0]["answer_voice"]
        assert stored(found) == stored(listed)
        return stored(found)

    bad = [
        {"file_id": 12}, {"file_id": ""}, {"file_id": "a/b"}, {"file_id": "x?alt=media"}, {"file_id": "f" * 201},
        {"file_id": None},  # no file and not deleted
        {"mime": "audio/mpeg"}, {"mime": "text/html"}, {"mime": "AUDIO/MP4"}, {"mime": None},
        {"bytes": 0}, {"bytes": -1}, {"bytes": 5 * MB + 1}, {"bytes": "1000"}, {"bytes": True}, {"bytes": 1.5},
        {"duration_ms": 999}, {"duration_ms": 300001}, {"duration_ms": "42000"}, {"duration_ms": True},
        {"duration_ms": 42000.0},
        {"lang": ""}, {"lang": "a" * 8 + "-bbbbbbbb" * 3 + "c"}, {"lang": 5}, {"lang": None}, {"lang": "ru RU"},
        {"lang": "ru_RU"},
        {"transcript_source": "cloud"}, {"transcript_source": "On_device"}, {"transcript_source": None},
        {"transcript": 7}, {"transcript": "x" * 4001}, {"transcript": ["yes"]},
        {"transcript_source": "none", "transcript": "heard something"},
        {"answered_at": None}, {"answered_at": 5},
        {"deleted_at": 5}, {"deleted_at": now_iso()},  # deleted, yet a file
    ]
    for changes in bad:
        assert read(**changes) is None, changes
    for key in VOICE_KEYS:  # every field must be there
        rewrite(drive, 1, answer_voice={k: v for k, v in voice.items() if k != key})
        assert asks.get(1)["answer_voice"] is None, key
    for junk in ("voice", [voice], 5, True, {}):
        rewrite(drive, 1, answer_voice=junk)
        assert asks.get(1)["answer_voice"] is None, junk

    good = [
        {"bytes": 5 * MB}, {"bytes": 1}, {"duration_ms": 1000}, {"duration_ms": 300000},
        {"lang": "a" * 8 + "-bbbbbbbb" * 3}, {"lang": "en"}, {"lang": "zh-Hant-TW"},
        {"transcript": "x" * 4000}, {"transcript": None}, {"transcript_source": "none", "transcript": None},
        {"mime": "audio/webm"}, {"file_id": None, "deleted_at": now_iso()}, {"file_id": "1AbC-_xyz" * 10},
    ]
    for changes in good:
        assert read(**changes) == {**voice, **changes}, changes
    # cleaned like every text field: control characters dropped, line breaks kept, stripped; empty is null
    assert read(transcript="  \x1b[31mда,\r\nшей\x07  ")["transcript"] == "[31mда,\nшей"
    assert read(transcript="   ")["transcript"] is None
    # extra keys are not part of it (and never reach an agent)
    assert read(url="https://evil.example/x", note="hi") == voice


def test_stale_answer_voice_ignored_and_dropped(hosted, google):
    """An older app re-answers or reopens and keeps answer_voice as a field it doesn't know: its answered_at is no
    longer the ask's, so it reads as null, and the bridge's next write of that ask drops it. The recording then counts
    as nobody's and is left to the apps' sweep. A bad one is dropped the same way."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    asks = asks_of(hosted)
    for title in ("Ship?", "Paint?", "Print?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))

    voice = answered_by_voice(drive, 1)
    app_action(drive, 1, "answered", answer="No, wait")  # an older app: a new answer, the old answer_voice kept
    assert drive.doc(1)["answer_voice"] == voice and drive.doc(1)["answered_at"] != voice["answered_at"]
    assert asks.get(1)["answer_voice"] is None
    assert tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_voice"] is None
    tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Waited"}))  # the bridge's next write
    assert drive.doc(1)["answer_voice"] is None
    assert len(voice_files(drive)) == 1  # not the ask's recording any more: the sweep's

    stale = answered_by_voice(drive, 2)
    app_action(drive, 2, "reopened")  # an older app reopens and keeps it
    assert drive.doc(2)["answer_voice"] == stale and asks.get(2)["answer_voice"] is None
    tool_result(mcp(hosted, token, "ask_update", {"id": 2, "title": "Paint it red?"}))
    assert drive.doc(2)["answer_voice"] is None

    answered_by_voice(drive, 3, duration_ms=999)  # bad
    assert asks.get(3)["answer_voice"] is None and drive.doc(3)["answer_voice"]["duration_ms"] == 999
    tool_result(mcp(hosted, token, "ask_close", {"id": 3}))
    doc = drive.doc(3)
    assert doc["answer_voice"] is None and doc["status"] == "closed"
    assert len(voice_files(drive)) == 3  # neither was the ask's recording: nothing deleted


def test_reopen_clears_answer_voice(hosted, google):
    """Reopen clears answer_voice; the `reopened` event payload carries it (with the cleared answer)."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))
    voice = answered_by_voice(drive, 1)
    reopened = hosted.post("/api/asks/1/reopen", headers=bearer(token))
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "open" and reopened.json()["answer_voice"] is None
    doc = drive.doc(1)
    assert doc["answer_voice"] is None and doc["answer"] is None and doc["answered_at"] is None
    event = doc["events"][-1]
    assert (event["kind"], event["actor"]) == ("reopened", "allen")
    assert event["payload"]["answer_voice"] == voice and event["payload"]["answer"] == "Yes, ship it"
    assert event["payload"]["previous_status"] == "answered"
    assert asks_of(hosted).get(1)["answer_voice"] is None

    # closed (the recording deleted), then reopened: the payload carries what the ask had
    answered_by_voice(drive, 2)
    tool_result(mcp(hosted, token, "ask_close", {"id": 2, "resolution": "Shipped"}))
    closed_voice = drive.doc(2)["answer_voice"]
    assert closed_voice["file_id"] is None
    hosted.post("/api/asks/2/reopen", headers=bearer(token)).raise_for_status()
    doc = drive.doc(2)
    assert doc["answer_voice"] is None and doc["events"][-1]["payload"]["answer_voice"] == closed_voice


def test_close_keeps_answer_voice_without_file(hosted, google):
    """Close keeps answer_voice (the transcript and the rest stay for the record) with file_id null and deleted_at set
    to the close; the `closed` event is as before."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    voice = answered_by_voice(drive, 1)
    closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    doc = drive.doc(1)
    assert doc["answer_voice"] == {**voice, "file_id": None, "deleted_at": doc["closed_at"]}
    assert doc["closed_at"] == closed["closed_at"] and doc["answered_at"] == voice["answered_at"]
    assert stored(closed["answer_voice"]) == doc["answer_voice"]
    assert stored(asks_of(hosted).get(1)["answer_voice"]) == doc["answer_voice"]
    assert doc["events"][-1]["payload"] == {"resolution": "Shipped", "previous_status": "answered"}
    # a voice answer the user deleted already (file_id null) stays as it is on close
    tool_result(mcp(hosted, token, "ask_create", {"title": "Paint?"}))
    gone = answered_by_voice(drive, 2, file_id=None, deleted_at=now_iso())
    tool_result(mcp(hosted, token, "ask_close", {"id": 2}))
    assert drive.doc(2)["answer_voice"] == gone


def test_no_tool_or_rest_body_sets_answer_voice(hosted, google):
    """Only the user writes answer_voice, in the same write as the answer: no MCP tool, no REST body and no unknown
    field passed through sets it. A new ask has it null."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    forged = {"file_id": "forged1", "mime": "audio/mp4", "bytes": 10, "duration_ms": 2000, "lang": "en",
              "transcript_source": "on_device", "transcript": "the user said yes", "answered_at": now_iso(),
              "deleted_at": None}
    created = tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    assert created["answer_voice"] is None and drive.doc(1)["answer_voice"] is None
    # MCP: no tool has such a parameter, and an extra argument is dropped before the tool runs
    for name, arguments in (("ask_create", {"title": "Forged?", "answer_voice": forged}),
                            ("ask_update", {"id": 1, "title": "Ship today?", "answer_voice": forged}),
                            ("ask_close", {"id": 1, "answer_voice": forged})):
        assert tool_result(mcp(hosted, token, name, arguments))["answer_voice"] is None, name
    # REST: every body with fields refuses it; reopen takes no body and ignores one
    for method, path, body in (("POST", "/api/asks", {"title": "Forged?", "answer_voice": forged}),
                               ("POST", "/api/asks", {"title": "Forged?", "attachments": [], "answer_voice": forged}),
                               ("PATCH", "/api/asks/2", {"answer_voice": forged}),
                               ("POST", "/api/asks/2/answer", {"answer": "Yes", "answer_voice": forged}),
                               ("POST", "/api/asks/2/close", {"answer_voice": forged})):
        response = hosted.request(method, path, json=body, headers=bearer(token))
        assert response.status_code == 422 and "answer_voice" in response.json()["detail"], (method, path)
    reopened = hosted.post("/api/asks/1/reopen", json={"answer_voice": forged}, headers=bearer(token))
    assert reopened.status_code == 200 and reopened.json()["answer_voice"] is None
    # the services themselves: an unknown field is refused, never passed through
    asks = asks_of(hosted)
    for call, args in ((asks.create, (ALLEN, {"title": "Forged?", "answer_voice": forged})),
                       (asks.update, (ALLEN, 1, {"answer_voice": forged}))):
        with pytest.raises(Invalid) as refused:
            call(*args)
        assert "unknown fields: answer_voice" in str(refused.value)
    for ask in asks.list("all")["asks"]:
        assert ask["answer_voice"] is None, ask["id"]
    for name in [f["name"] for f in drive.files.values() if f["name"].startswith("ask-")]:
        doc = json.loads(drive.file(name)["content"])
        assert doc.get("answer_voice") is None and "forged1" not in json.dumps(doc), name


def test_answer_noop_rule_with_voice(hosted, google):
    """Re-answering with the same text and no new recording stays a no-op; a new recording is a change. The `answered`
    payload adds voice and previous_voice."""
    at = "2026-11-17T10:00:00.000Z"
    voice = {"file_id": "rec1", "mime": "audio/mp4", "bytes": 1000, "duration_ms": 42000, "lang": "ru-RU",
             "transcript_source": "on_device", "transcript": "да", "answered_at": at, "deleted_at": None}
    ask = {"id": 1, "agent": "allen", "status": "answered", "answer": "Да", "answered_at": at, "answer_voice": voice}
    now = "2026-11-17T11:00:00.000Z"

    assert plan_answer(DENIS, "  Да ", lambda: ask, now) == (ask, None)  # same text, no recording: nothing changes
    newer = {**voice, "file_id": "rec2", "transcript": "да!"}
    _, change = plan_answer(DENIS, "Да", lambda: ask, now, voice=newer)  # same text, a new recording
    assert isinstance(change, Change) and change.kind == "answered"
    assert change.updates["answer_voice"] == {**newer, "answered_at": now} and change.updates["answered_at"] == now
    assert change.payload == {"answer": "Да", "previous_answer": "Да", "voice": {**newer, "answered_at": now},
                              "previous_voice": voice}
    _, change = plan_answer(DENIS, "Нет", lambda: ask, now)  # new text, no recording: the old one no longer belongs
    assert change.updates["answer_voice"] is None
    assert change.payload == {"answer": "Нет", "previous_answer": "Да", "voice": None, "previous_voice": voice}
    plain = {**ask, "answer_voice": None}
    _, change = plan_answer(DENIS, "Нет", lambda: plain, now)
    assert "answer_voice" not in change.updates  # nothing to clear (the single-tenant bridge has no column for it)
    assert change.payload == {"answer": "Нет", "previous_answer": "Да", "voice": None, "previous_voice": None}

    # on the hosted bridge's files: the same text again writes nothing
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    answered_by_voice(drive, 1, text="Да")
    before = drive.doc(1)
    again = asks_of(hosted).answer(DENIS, 1, "Да")
    assert drive.doc(1) == before and stored(again["answer_voice"]) == before["answer_voice"]


def test_bridge_deletes_recording_on_agent_close_and_reopen(hosted, google, caplog):
    """Whoever makes the transition deletes the file: the bridge for an agent's ask_close and reopen, after the ask is
    written, and only a file Drive says is this ask's recording (the user can name any file in their ask file)."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?", "Print?", "Pack?", "Post?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))
    first = answered_by_voice(drive, 1)
    second = answered_by_voice(drive, 2, mime="audio/webm")
    assert len(voice_files(drive)) == 2
    tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    assert first["file_id"] not in drive.files and second["file_id"] in drive.files
    hosted.post("/api/asks/2/reopen", headers=bearer(token)).raise_for_status()
    assert voice_files(drive) == []
    assert [c[1] for c in drive.calls("DELETE")] == [f"/drive/v3/files/{first['file_id']}",
                                                     f"/drive/v3/files/{second['file_id']}"]

    # a tampered ask file names the ask file of another ask, or another ask's recording: neither is deleted
    own = answered_by_voice(drive, 5)
    answered_by_voice(drive, 3, file_id=drive.file("ask-5.json")["id"])
    answered_by_voice(drive, 4, file_id=own["file_id"])
    deletes = len(drive.calls("DELETE"))
    with caplog.at_level(logging.WARNING):
        tool_result(mcp(hosted, token, "ask_close", {"id": 3}))
        tool_result(mcp(hosted, token, "ask_close", {"id": 4}))
    assert drive.file("ask-5.json") and own["file_id"] in drive.files
    assert len(drive.calls("DELETE")) == deletes
    assert sum("not deleted" in r.getMessage() for r in caplog.records) == 2
    # the user closes in the app: the app deletes the recording, the bridge doesn't
    app_action(drive, 5, "closed", resolution="Done")
    asks_of(hosted).get(5)
    tool_result(mcp(hosted, token, "asks_mine", {"status": "all"}))
    assert len(drive.calls("DELETE")) == deletes and own["file_id"] in drive.files


def test_failed_recording_delete_is_logged_without_content(hosted, google, caplog):
    """Deleting is best effort: the close stands, the failure is logged without the recording or the transcript, and
    the file is left to the apps' sweep."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    raw = b"AUDIO-OF-THE-USER-" + AUDIO
    voice = answered_by_voice(drive, 1, raw=raw, transcript="the secret plan is to ship on friday")
    drive.fail_next("DELETE", "/drive/v3/files/", 500)
    with caplog.at_level(logging.DEBUG):
        closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    assert closed["status"] == "closed" and closed["answer_voice"]["file_id"] is None
    assert voice["file_id"] in drive.files  # left to the sweep
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not delete the voice recording" in logged and f"ask #1" in logged
    assert "secret plan" not in logged and "AUDIO-OF-THE-USER" not in logged and "ship on friday" not in logged


# ---------- the single-tenant bridge ----------

def test_single_tenant_answer_voice_null(client):
    """Voice answers are hosted only: the single-tenant bridge always gives answer_voice null, and its events carry the
    new payload keys as null."""
    created = client.post("/api/asks", json={"title": "Ship?"}, headers=auth(ALLEN_TOKEN))
    assert created.status_code == 201 and created.json()["answer_voice"] is None
    ask_id = created.json()["id"]
    answered = client.post(f"/api/asks/{ask_id}/answer", json={"answer": "Yes"}, headers=auth(DENIS_TOKEN))
    assert answered.json()["answer_voice"] is None
    refused = client.post(f"/api/asks/{ask_id}/answer", json={"answer": "Yes", "answer_voice": {"file_id": "x"}},
                          headers=auth(DENIS_TOKEN))
    assert refused.status_code == 422
    client.post(f"/api/asks/{ask_id}/reopen", headers=auth(DENIS_TOKEN)).raise_for_status()
    ask = client.get(f"/api/asks/{ask_id}", headers=auth(ALLEN_TOKEN)).json()
    assert ask["answer_voice"] is None
    assert client.get("/api/asks", headers=auth(ALLEN_TOKEN)).json()["asks"][0]["answer_voice"] is None
    answered_event, reopened_event = ask["events"][1:]
    assert answered_event["payload"] == {"answer": "Yes", "previous_answer": None, "voice": None,
                                         "previous_voice": None}
    assert reopened_event["payload"]["answer_voice"] is None
