"""Hosted bridge: voice answers (SPEC v5 "5. Voice answers"). The apps write `answer_voice` into the ask file next to
the answer; the bridge checks it on every read, drops a bad or stale one on its next write, keeps it without the file on
close, clears it on reopen and deletes the recording when an agent closes or reopens. FakeGoogle's Drive plays the
user's Drive; `answered_by_voice` plays the apps' send (SPEC «Sending»)."""

import base64
import json
import logging
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from server.hosted.crypto import CryptoError, Keyring
from server.hosted.records import HostedAsks
from server.rules import Change, Invalid, plan_answer, plan_reopen
from server.tokens import Identity

from .conftest import ALLEN as ALLEN_TOKEN
from .conftest import DENIS as DENIS_TOKEN
from .conftest import auth
from .fake_drive import app_action, now_iso
from .hosted_support import (
    DENIS_SUB,
    KEY,
    OLGA_EMAIL,
    OLGA_SUB,
    PUBLIC,
    build,
    connect,
    id_token,
    mcp,
    register,
    tool_result,
)

ALLEN = Identity("agent", "allen")
DENIS = Identity("denis")
MB = 1024 * 1024
AUDIO = b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00M4A mp42isom" + bytes(range(256)) * 8  # the bytes are not looked at
# What the agents are told (SPEC "What the agent gets": the asks_mine sentence word for word).
ASKS_MINE_VOICE = ("answer_voice set: your user answered by voice. With transcript_source on_device, answer is their "
                   "checked text. With none, answer is what they typed or the placeholder 'Voice answer (m:ss), not "
                   "transcribed': download answer_voice.url (valid 1 h; read the ask again for a new one) and "
                   "transcribe it, or ask them. The recording is deleted when you close the ask.")
ASK_GET_VOICE = ("On your own ask answered by voice, answer_voice.url is a new link to the recording (valid 1 h), "
                 "as in asks_mine; other agents get url null.")
ASK_CLOSE_VOICE = ("Closing deletes the recording of a voice answer (answer_voice) from your user's Drive: download it "
                   "first if you still need it.")
LLMS_VOICE = ("- answer_voice on an answered ask: your human answered by voice; answer_voice.url (asks_mine, ask_get; "
              "valid 1 h) downloads the recording to listen to or transcribe; ask_close deletes it")
NOT_FOUND = {"detail": "no such recording, or the link has expired: read the ask again for a new link"}
GONE = {"detail": "the recording was deleted"}
VOICE_KEYS = ("file_id", "mime", "bytes", "duration_ms", "lang", "transcript_source", "transcript", "answered_at",
              "deleted_at")


def agent(hosted, google, name: str = "Allen", sub: str = DENIS_SUB, email: str = "owner@example.com") -> str:
    """An agent connected to Denis's account (or another user's): its access token."""
    return connect(hosted, google, register(hosted)["client_id"], agent=name, sub=sub, email=email)["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def agent_id(hosted, google, name: str = "Allen", sub: str = DENIS_SUB, email: str = "owner@example.com") -> str:
    agents = hosted.get("/api/agents", headers=id_token(google, sub, email)).json()["agents"]
    return next(a["id"] for a in agents if a["name"] == name)


def link(hosted, token: str, ask_id: int = 1) -> str:
    """The path of the owner's link to the ask's recording, as ask_get gives it."""
    url = tool_result(mcp(hosted, token, "ask_get", {"id": ask_id}))["answer_voice"]["url"]
    return url.removeprefix(PUBLIC)


def sealed_path(path: str, keyring: Keyring | None = None, context: str = "media", drop: str = "", **changes) -> str:
    """A token like the bridge's, sealed here with the claims of `path` changed."""
    found = {k: v for k, v in {**claims(f"{PUBLIC}{path}"), **changes}.items() if k != drop}
    return f"/media/v1/{(keyring or Keyring(KEY)).encrypt(json.dumps(found), context)}"


def claims(url: str) -> dict:
    """What a link's token says, opened with the bridge's key (SPEC: {user, agent, ask_id, file_id, exp}, AES-GCM,
    context "media")."""
    assert url.startswith(f"{PUBLIC}/media/v1/"), url
    return json.loads(Keyring(KEY).decrypt(url.removeprefix(f"{PUBLIC}/media/v1/"), "media"))


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def test_plan_reopen_clears_answer_voice():
    """The rule itself: reopen sets answer_voice to null and carries it in the `reopened` payload (the bridge's write
    would also drop a voice answer on an open ask, so this is checked where the rule is)."""
    at = "2026-11-17T10:00:00.000Z"
    voice = {"file_id": "rec1", "mime": "audio/mp4", "bytes": 1000, "duration_ms": 42000, "lang": "ru-RU",
             "transcript_source": "on_device", "transcript": "да", "answered_at": at, "deleted_at": None}
    ask = {"id": 1, "agent": "allen", "status": "answered", "answer": "Да", "answered_at": at, "resolution": None,
           "closed_by": None, "closed_at": None, "answer_voice": voice}
    now = "2026-11-17T11:00:00.000Z"
    for status, recording in (("answered", voice), ("closed", {**voice, "file_id": None, "deleted_at": now})):
        _, change = plan_reopen(ALLEN, lambda: {**ask, "status": status, "answer_voice": recording}, now)
        assert change.updates["answer_voice"] is None, status
        assert change.payload["answer_voice"] == recording, status
    _, change = plan_reopen(ALLEN, lambda: {**ask, "answer_voice": None}, now)
    assert "answer_voice" not in change.updates and change.payload["answer_voice"] is None


def test_older_app_close_or_reopen_leaves_no_link(hosted, google):
    """An app that doesn't know answer_voice keeps it as an unknown field when it closes or reopens: file_id set, and
    answered_at unchanged by a close. The ask's status decides. Closed: answer_voice shows as stored, url is always
    null (asks_mine, ask_get, REST, the webhook) and the old link is 410. Open (reopened keeping answered_at too): it
    counts as null for every reader, the old link is 404, and the bridge's next write drops it."""
    token = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/voice"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))

    voice = answered_by_voice(drive, 1)
    path = link(hosted, token)
    assert hosted.get(path).status_code == 200
    app_action(drive, 1, "closed", resolution="Done")  # an older app: answer_voice kept as it was, file_id and all
    assert drive.doc(1)["answer_voice"] == voice and drive.doc(1)["status"] == "closed"
    reads = (tool_result(mcp(hosted, token, "asks_mine", {"status": "closed"}))["asks"][0],
             tool_result(mcp(hosted, token, "ask_get", {"id": 1})),
             hosted.get("/api/asks/1", headers=bearer(token)).json())
    for got in reads:
        assert got["answer_voice"]["file_id"] == voice["file_id"]  # shown as stored
        assert (got["answer_voice"]["url"], got["answer_voice"]["url_expires_at"]) == (None, None)
    response = hosted.get(path)
    assert (response.status_code, response.json()) == (410, GONE)
    hosted.post("/api/events", json={"ask_id": 1, "kind": "closed"}, headers=id_token(google)).raise_for_status()
    assert hosted.app.state.webhooks.flush(5)
    body = json.loads(google.webhook_calls[-1].content)
    assert body["type"] == "ask.closed" and body["ask"]["answer_voice"]["url"] is None

    answered_by_voice(drive, 2)
    path = link(hosted, token, 2)
    assert hosted.get(path).status_code == 200
    rewrite(drive, 2, status="open", answer=None)  # reopened by an older app that kept answered_at and answer_voice
    for who in (token, feynman):
        assert tool_result(mcp(hosted, who, "ask_get", {"id": 2}))["answer_voice"] is None
        assert hosted.get("/api/asks/2", headers=bearer(who)).json()["answer_voice"] is None
    assert tool_result(mcp(hosted, token, "asks_mine"))["asks"][0]["answer_voice"] is None
    assert asks_of(hosted).get(2)["answer_voice"] is None
    response = hosted.get(path)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert drive.doc(2)["answer_voice"] is not None
    tool_result(mcp(hosted, token, "ask_update", {"id": 2, "title": "Paint it red?"}))  # the bridge's next write
    assert drive.doc(2)["answer_voice"] is None


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
    assert f"could not delete the voice recording {voice['file_id']} (left to the apps' sweep): Unavailable" in logged
    assert "secret plan" not in logged and "AUDIO-OF-THE-USER" not in logged and "ship on friday" not in logged


def test_recording_delete_never_fails_the_close(hosted, google, caplog, monkeypatch):
    """Whatever goes wrong while deleting the recording, the close (already written) stands: the failure is logged
    with its type and the file id only."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    voice = answered_by_voice(drive, 1, transcript="the secret plan")

    def broken(file_id):
        raise RuntimeError(f"unexpected trouble with {file_id}: the secret plan")

    monkeypatch.setattr(hosted.app.state.pool.client(DENIS_SUB), "delete", broken)
    with caplog.at_level(logging.WARNING):
        closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    assert closed["status"] == "closed" and drive.doc(1)["status"] == "closed"
    assert voice["file_id"] in drive.files
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert f"could not delete the voice recording {voice['file_id']} (left to the apps' sweep): RuntimeError" in logged
    assert "secret plan" not in logged


# ---------- what the agent gets ----------

def voice_parts(events: list) -> list[dict]:
    """The voice objects in an ask's history: the voice and previous_voice of `answered`, the answer_voice of
    `reopened`, in order."""
    return [e["payload"][key] for e in events for key in ("voice", "previous_voice", "answer_voice")
            if isinstance(e.get("payload"), dict) and isinstance(e["payload"].get(key), dict)]


def test_only_owner_gets_url(hosted, google):
    """For the owner agent only (asks_mine, ask_get, its REST) answer_voice carries url and url_expires_at:
    <public URL>/media/v1/<token>, the token sealed with the bridge's key (AES-GCM, context "media"), so it reveals
    nothing; valid 1 h, a new one on every read. Other agents get url null, and so does everyone once the recording is
    deleted. The link is never written to Drive. The text the device heard is the owner's too: every other reader gets
    transcript null, in answer_voice and in the history (the user edits that text before sending)."""
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    allen_id = agent_id(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "ask_create", {"title": "Ship?"}))
    answered_by_voice(drive, 1, transcript="yes ship it")
    voice = answered_by_voice(drive, 1, text="Yes, ship it today", transcript="yes ship it today")

    reads = {"asks_mine": tool_result(mcp(hosted, allen, "asks_mine"))["asks"][0],
             "ask_get": tool_result(mcp(hosted, allen, "ask_get", {"id": 1})),
             "asks_list": tool_result(mcp(hosted, allen, "asks_list"))["asks"][0],
             "GET /api/asks/1": hosted.get("/api/asks/1", headers=bearer(allen)).json(),
             "GET /api/asks": hosted.get("/api/asks", params={"agent": "allen"},
                                         headers=bearer(allen)).json()["asks"][0]}
    urls = set()
    for read, ask in reads.items():
        got = ask["answer_voice"]
        assert stored(got) == voice and list(got) == [*VOICE_KEYS, "url", "url_expires_at"], read
        sealed = claims(got["url"])
        assert sealed == {"user": DENIS_SUB, "agent": allen_id, "ask_id": 1, "file_id": voice["file_id"],
                          "exp": sealed["exp"]}, read
        assert isinstance(sealed["exp"], int) and abs(sealed["exp"] - (time.time() + 3600)) < 60, read
        assert got["url_expires_at"] == iso(sealed["exp"]), read
        token = got["url"].rsplit("/", 1)[1]
        assert voice["file_id"] not in token and allen_id not in token and "ask_id" not in token, read  # sealed
        with pytest.raises(CryptoError):
            Keyring(KEY).decrypt(token, f"google-refresh:{DENIS_SUB}")  # bound to its own context
        urls.add(got["url"])
    assert len(urls) == len(reads)  # a new link on every read

    others = (tool_result(mcp(hosted, feynman, "asks_list"))["asks"][0],
              tool_result(mcp(hosted, feynman, "ask_get", {"id": 1})),
              hosted.get("/api/asks/1", headers=bearer(feynman)).json(),
              hosted.get("/api/asks", headers=bearer(feynman)).json()["asks"][0])
    for ask in others:
        assert stored(ask["answer_voice"]) == {**voice, "transcript": None}
        assert (ask["answer_voice"]["url"], ask["answer_voice"]["url_expires_at"]) == (None, None)
    events = tool_result(mcp(hosted, allen, "ask_get", {"id": 1}))["events"]
    assert "/media/v1/" not in json.dumps(events)  # the history is the file's: no links in it
    assert [part["transcript"] for part in voice_parts(events)] == ["yes ship it", "yes ship it today", "yes ship it"]
    for history in (tool_result(mcp(hosted, feynman, "ask_get", {"id": 1}))["events"],
                    hosted.get("/api/asks/1", headers=bearer(feynman)).json()["events"]):
        assert [part["transcript"] for part in voice_parts(history)] == [None, None, None]
        assert [part["file_id"] for part in voice_parts(history)] == [part["file_id"] for part in voice_parts(events)]
    for ask in others:
        assert "yes ship it" not in json.dumps(ask)
    assert "/media/v1/" not in json.dumps(drive.doc(1))

    closed = tool_result(mcp(hosted, allen, "ask_close", {"id": 1, "resolution": "Shipped"}))
    for ask in (closed, tool_result(mcp(hosted, allen, "ask_get", {"id": 1})),
                tool_result(mcp(hosted, allen, "asks_mine", {"status": "closed"}))["asks"][0]):
        assert ask["answer_voice"]["file_id"] is None
        assert (ask["answer_voice"]["url"], ask["answer_voice"]["url_expires_at"]) == (None, None)
    assert "/media/v1/" not in json.dumps(drive.doc(1))
    assert tool_result(mcp(hosted, feynman, "ask_get", {"id": 1}))["answer_voice"]["transcript"] is None
    assert tool_result(mcp(hosted, allen, "ask_get", {"id": 1}))["answer_voice"]["transcript"] == "yes ship it today"
    # an ask without a voice answer: null, as before
    assert tool_result(mcp(hosted, allen, "ask_create", {"title": "Paint?"}))["answer_voice"] is None
    assert tool_result(mcp(hosted, allen, "ask_get", {"id": 2}))["answer_voice"] is None
    # reopened: the reopened event carries the voice answer, its text still the owner's alone
    tool_result(mcp(hosted, allen, "ask_create", {"title": "Print?"}))
    answered_by_voice(drive, 3, transcript="print it")
    hosted.post("/api/asks/3/reopen", headers=bearer(allen)).raise_for_status()
    mine = tool_result(mcp(hosted, allen, "ask_get", {"id": 3}))["events"]
    theirs = tool_result(mcp(hosted, feynman, "ask_get", {"id": 3}))["events"]
    assert [part["transcript"] for part in voice_parts(mine)] == ["print it", "print it"]
    assert [part["transcript"] for part in voice_parts(theirs)] == [None, None]
    assert theirs[-1]["kind"] == "reopened" and "print it" not in json.dumps(theirs)


def test_webhook_body_carries_voice_url(hosted, google):
    """The ask.answered webhook goes to the owner agent: its ask's answer_voice carries the owner's url. The feed
    event, ask_wait and POST /api/events' reply don't change."""
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/voice"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    voice = answered_by_voice(drive, 1)
    sent = hosted.post("/api/events", json={"ask_id": 1, "kind": "answered"}, headers=id_token(google))
    assert sent.status_code == 202 and sent.json() == {"queued": 1, "fed": 1}
    assert hosted.app.state.webhooks.flush(5)
    body = json.loads(google.webhook_calls[-1].content)
    assert body["type"] == "ask.answered" and body["answer"] == "Yes, ship it"
    got = body["ask"]["answer_voice"]
    assert stored(got) == voice and got["url_expires_at"]
    sealed = claims(got["url"])
    assert (sealed["user"], sealed["agent"], sealed["ask_id"], sealed["file_id"]) == (DENIS_SUB, owner, 1,
                                                                                       voice["file_id"])
    assert got["url_expires_at"] == iso(sealed["exp"])
    fed = hosted.app.state.feed.read(owner, "0")["events"]
    assert len(fed) == 1 and "ask" not in fed[0] and "/media/v1/" not in json.dumps(fed)
    waited = tool_result(mcp(hosted, token, "ask_wait", {"timeout": 0}))
    assert "/media/v1/" not in json.dumps(waited) and "answer_voice" not in json.dumps(waited)
    # once the agent closed it, the next webhook (the user reopens in the app) has no link
    tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    app_action(drive, 1, "reopened")
    hosted.post("/api/events", json={"ask_id": 1, "kind": "reopened"}, headers=id_token(google)).raise_for_status()
    assert hosted.app.state.webhooks.flush(5)
    assert json.loads(google.webhook_calls[-1].content)["ask"]["answer_voice"] is None


# ---------- GET /media/v1/<token> ----------

def test_media_route_404s(hosted, google):
    """A tampered, expired or foreign token, a file that is no longer the ask's recording, a revoked agent: 404 with one
    message for all, whatever the reason."""
    allen = agent(hosted, google)
    feynman_id = (agent(hosted, google, "Feynman"), agent_id(hosted, google, "Feynman"))[1]
    allen_id = agent_id(hosted, google)
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?"):
        tool_result(mcp(hosted, allen, "ask_create", {"title": title}))
    voice = answered_by_voice(drive, 1)
    path = link(hosted, allen)
    assert hosted.get(path).content == AUDIO
    token = path.removeprefix("/media/v1/")
    flipped = token[:-5] + ("A" if token[-5] != "A" else "B") + token[-4:]
    refused = {
        "tampered": f"/media/v1/{flipped}",
        "cut short": path[:-12],
        "garbage": "/media/v1/v1.k1.garbage",
        "empty": "/media/v1/",
        "a path below it": f"{path}/more",
        "too long": f"{path}{'A' * 3000}",
        "another context": sealed_path(path, context=f"webhook:{allen_id}"),
        "another key": sealed_path(path, keyring=Keyring(base64.b64encode(b"z" * 32).decode())),
        "expired": sealed_path(path, exp=int(time.time()) - 1),
        "a claim missing": sealed_path(path, drop="agent"),
        "a claim of the wrong type": sealed_path(path, ask_id="1"),
        "another user": sealed_path(path, user=OLGA_SUB),
        "an unknown agent": sealed_path(path, agent="ag_" + "x" * 16),
        "another agent of the user": sealed_path(path, agent=feynman_id),
        "another ask": sealed_path(path, ask_id=2),
        "an ask that doesn't exist": sealed_path(path, ask_id=99),
        "another file": sealed_path(path, file_id=drive.file("ask-2.json")["id"]),
    }
    for name, refused_path in refused.items():
        response = hosted.get(refused_path)
        assert (response.status_code, response.json()) == (404, NOT_FOUND), name
        assert response.headers["cache-control"] == "no-store", name

    # another user's agent with a link naming Denis's recording: her ask 1 has its own recording
    olga = agent(hosted, google, "Olga's bot", sub=OLGA_SUB, email=OLGA_EMAIL)
    tool_result(mcp(hosted, olga, "ask_create", {"title": "Hers?"}))
    answered_by_voice(google.drive(OLGA_SUB), 1)
    olga_path = link(hosted, olga)
    assert hosted.get(olga_path).status_code == 200
    stolen = sealed_path(olga_path, file_id=voice["file_id"])
    assert (hosted.get(stolen).status_code, hosted.get(stolen).json()) == (404, NOT_FOUND)

    # the ask file names a file that is not this ask's recording (another ask's, an ask file, one over 5 MB)
    other = answered_by_voice(drive, 2)
    rewrite(drive, 1, answer_voice={**voice, "file_id": other["file_id"]})
    assert hosted.get(link(hosted, allen)).json() == NOT_FOUND
    rewrite(drive, 1, answer_voice={**voice, "file_id": drive.file("ask-2.json")["id"]})
    assert hosted.get(link(hosted, allen)).json() == NOT_FOUND
    answered_by_voice(drive, 1, raw=bytes(5 * MB + 1), bytes=1000)
    assert hosted.get(link(hosted, allen)).json() == NOT_FOUND

    # not the current file: the user answered again with a new recording
    answered_by_voice(drive, 1)
    old = link(hosted, allen)
    answered_by_voice(drive, 1, text="Yes, today")
    current = link(hosted, allen)
    assert hosted.get(old).json() == NOT_FOUND and hosted.get(current).status_code == 200
    # a revoked agent's links die with it
    hosted.delete(f"/api/agents/{allen_id}", headers=id_token(google)).raise_for_status()
    assert (hosted.get(current).status_code, hosted.get(current).json()) == (404, NOT_FOUND)


def test_old_link_after_agent_reopen_404(hosted, google):
    """The agent reopens its answered ask: answer_voice is cleared and the recording deleted, so the link it had is a
    404, the one message."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    answered_by_voice(drive, 1)
    path = link(hosted, token)
    assert hosted.get(path).content == AUDIO
    hosted.post("/api/asks/1/reopen", headers=bearer(token)).raise_for_status()
    response = hosted.get(path)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_media_route_deleted_410(hosted, google):
    """Once the recording is deleted: 410. Closed (file_id null), or gone from Drive while the ask still names it (the
    apps' sweep after 30 days, the user deleting the app's data)."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?", "Print?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))
    answered_by_voice(drive, 1)
    path = link(hosted, token)
    assert hosted.get(path).status_code == 200
    tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    response = hosted.get(path)
    assert (response.status_code, response.json()) == (410, GONE)
    assert response.headers["cache-control"] == "no-store"

    swept = answered_by_voice(drive, 2)
    path = link(hosted, token, 2)
    drive.files.pop(swept["file_id"])
    assert (hosted.get(path).status_code, hosted.get(path).json()) == (410, GONE)

    closed_in_app = answered_by_voice(drive, 3)
    path = link(hosted, token, 3)
    app_action(drive, 3, "closed", resolution="Done",
               extra={"answer_voice": {**closed_in_app, "file_id": None, "deleted_at": now_iso()}})
    drive.files.pop(closed_in_app["file_id"])
    assert (hosted.get(path).status_code, hosted.get(path).json()) == (410, GONE)


def test_media_route_headers(google):
    """No Authorization and no CORS; the exact headers of SPEC "What the agent gets"."""
    app, _ = build(google, cors_origins=("https://app.example",))
    with TestClient(app, base_url=PUBLIC) as hosted:
        token = agent(hosted, google)
        drive = google.drive(DENIS_SUB)
        for title in ("Ship?", "Paint?"):
            tool_result(mcp(hosted, token, "ask_create", {"title": title}))
        answered_by_voice(drive, 1)
        answered_by_voice(drive, 2, raw=AUDIO[:1500], mime="audio/webm")
        path = link(hosted, token)
        response = hosted.get(path, headers={"Origin": "https://app.example"})
        assert response.status_code == 200 and response.content == AUDIO
        assert dict(response.headers) == {
            "content-type": "audio/mp4",
            "content-length": str(len(AUDIO)),
            "content-disposition": 'attachment; filename="ask-1-voice.m4a"',
            "cache-control": "private, no-store",
            "x-content-type-options": "nosniff",
            "content-security-policy": "sandbox",
        }
        webm = hosted.get(link(hosted, token, 2))
        assert webm.content == AUDIO[:1500] and webm.headers["content-type"] == "audio/webm"
        assert webm.headers["content-disposition"] == 'attachment; filename="ask-2-voice.webm"'
        # no Authorization: one sent is not looked at
        for headers in (bearer(token), {"Authorization": "Bearer junk"}):
            assert hosted.get(path, headers=headers).content == AUDIO
        # no CORS, for an answer, an error or a preflight
        for response in (hosted.get("/media/v1/nope", headers={"Origin": "https://app.example"}),
                         hosted.options(path, headers={"Origin": "https://app.example",
                                                       "Access-Control-Request-Method": "GET"})):
            assert not any(h.startswith("access-control-") for h in response.headers), dict(response.headers)


def test_media_route_content_type_from_allowlist(hosted, google):
    """The type is answer_voice's (audio/mp4 or audio/webm), never Drive's: a file Drive serves as text/html still goes
    out as audio, as an attachment, sandboxed and not sniffed."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    page = b"<html><body><script>fetch('/api/agents')</script></body></html>" + bytes(1000)
    answered_by_voice(drive, 1, raw=page, drive_mime="text/html")
    path = link(hosted, token)
    assert drive.files[claims(PUBLIC + path)["file_id"]]["mimeType"] == "text/html"  # what Drive will say
    response = hosted.get(path)
    assert response.status_code == 200 and response.content == page
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["content-disposition"] == 'attachment; filename="ask-1-voice.m4a"'
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_media_route_rate_limit(hosted, google):
    """60 requests a minute for each agent of a user, counted once the token opens: the 61st is 429 with Retry-After.
    The user's other agents and other users go on, so one leaked or revoked link can't block them."""
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "ask_create", {"title": "Ship?"}))
    tool_result(mcp(hosted, feynman, "ask_create", {"title": "Paint?"}))
    answered_by_voice(drive, 1)
    answered_by_voice(drive, 2)
    allen_path, feynman_path = link(hosted, allen, 1), link(hosted, feynman, 2)
    for n in range(60):
        assert hosted.get(allen_path).status_code == 200, n
    for n in range(60):
        assert hosted.get(feynman_path).status_code == 200, n
    for path in (allen_path, feynman_path):
        response = hosted.get(path)
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert response.json() == {"detail": "too many requests for recordings: at most 60 a minute per agent"}
    assert hosted.get("/media/v1/v1.k1.garbage").status_code == 404  # a token that doesn't open counts for nobody
    olga = agent(hosted, google, "Olga's bot", sub=OLGA_SUB, email=OLGA_EMAIL)
    tool_result(mcp(hosted, olga, "ask_create", {"title": "Hers?"}))
    answered_by_voice(google.drive(OLGA_SUB), 1)
    assert hosted.get(link(hosted, olga)).status_code == 200


def test_media_route_oversized_file_without_size_404(hosted, google):
    """A file over 5 MB for which Drive reports no size: the download stops at the cap and the answer is the one 404
    (not a 503 that would tell the agent to try again)."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    answered_by_voice(drive, 1, raw=bytes(5 * MB + 1), bytes=1000)
    path = link(hosted, token)
    real = drive._fields

    def no_size(file, fields):
        found = real(file, fields)
        found.pop("size", None)
        return found

    drive._fields = no_size
    response = hosted.get(path)
    drive._fields = real
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert hosted.app.state.media.guard.slots.in_use == 0


class Watch:
    """The app, noting the media slots in use whenever a recording's body goes out."""

    def __init__(self, app):
        self.app = app
        self.seen: list[int] = []

    async def __call__(self, scope, receive, send):
        async def watched(message):
            if message["type"] == "http.response.body" and scope["path"].startswith("/media/v1/"):
                self.seen.append(self.app.state.media.guard.slots.in_use)
            await send(message)

        await self.app(scope, receive, watched)


def test_media_slot_held_until_the_response_is_sent(google):
    """The recording's bytes keep their media slot until they have been sent, so the bound on media in flight
    (deploy/hosted/README.md) holds for voice too; the slot comes back after, whatever the outcome."""
    app, _ = build(google)
    watch = Watch(app)
    with TestClient(watch, base_url=PUBLIC) as hosted:
        token = agent(hosted, google)
        drive = google.drive(DENIS_SUB)
        tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
        voice = answered_by_voice(drive, 1)
        path = link(hosted, token)
        slots = app.state.media.guard.slots
        assert hosted.get(path).content == AUDIO
        assert watch.seen == [1] and slots.in_use == 0
        drive.fail_next("GET", f"/drive/v3/files/{voice['file_id']}", 500, after=1)
        assert hosted.get(path).status_code == 503 and slots.in_use == 0
        drive.files[voice["file_id"]]["content"] = bytes(5 * MB + 1)
        assert hosted.get(path).status_code == 404 and slots.in_use == 0


def test_media_route_drive_unavailable_503(hosted, google):
    """Drive unreachable: 503 with Retry-After (the link stays good for its hour). So is an instance whose media slots
    are all taken (the recording passes through memory, counted like pictures)."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    voice = answered_by_voice(drive, 1)
    path = link(hosted, token)
    unavailable = {"detail": "Google Drive is not available right now, try again"}
    drive.down = True
    response = hosted.get(path)
    assert (response.status_code, response.json()) == (503, unavailable) and response.headers["retry-after"] == "30"
    drive.down = False
    drive.fail_next("GET", f"/drive/v3/files/{voice['file_id']}", 500, after=1)  # the metadata, then the bytes
    response = hosted.get(path)
    assert (response.status_code, response.json()) == (503, unavailable)
    slots = hosted.app.state.media.guard.slots
    assert slots.take(4)
    response = hosted.get(path)
    assert (response.status_code, response.headers["retry-after"]) == (503, "5")
    slots.give(4)
    assert hosted.get(path).content == AUDIO and slots.in_use == 0


def test_media_route_logs_nothing_of_the_recording(hosted, google, caplog):
    """Neither the recording, nor the transcript, nor the token reaches the server's log, whatever the outcome."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    raw = b"RAW-VOICE-BYTES-" + AUDIO
    voice = answered_by_voice(drive, 1, raw=raw, transcript="our secret launch date is friday")
    path = link(hosted, token)
    secret_token = path.removeprefix("/media/v1/")
    with caplog.at_level(logging.DEBUG):
        assert hosted.get(path).content == raw
        hosted.get(path[:-3])
        drive.fail_next("GET", f"/drive/v3/files/{voice['file_id']}", 500, after=1)
        hosted.get(path)
        drive.down = True
        hosted.get(path)
        drive.down = False
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("asks"))
    assert "Drive call failed" in logged  # the failures are logged, without the rest
    for secret in ("RAW-VOICE-BYTES", "secret launch", secret_token[-40:]):
        assert secret not in logged, secret


# ---------- what the agents are told ----------

def tools_list(client, token: str) -> dict[str, str]:
    """Each tool's description as the calling model reads it (white space joined)."""
    response = client.post("/mcp", headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18",
                                            "Accept": "application/json, text/event-stream"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200, response.text
    return {t["name"]: " ".join(t["description"].split()) for t in response.json()["result"]["tools"]}


def test_voice_docstrings(hosted, google, client):
    """asks_mine says what answer_voice means and what to do with it (the SPEC's sentence), ask_get and ask_close what
    concerns them, and /llms.txt lists it with the tools. The single-tenant bridge has no voice answers: not a word."""
    tools = tools_list(hosted, agent(hosted, google))
    assert ASKS_MINE_VOICE in tools["asks_mine"]
    assert ASK_GET_VOICE in tools["ask_get"]
    assert ASK_CLOSE_VOICE in tools["ask_close"]
    assert LLMS_VOICE in " ".join(hosted.get("/llms.txt").text.split())
    single = tools_list(client, ALLEN_TOKEN)
    assert not any("voice" in description for description in single.values())


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
