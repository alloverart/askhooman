"""Hosted bridge: pictures and video on asks (SPEC v5 step 4): a create with attachments, ask_attach / ask_detach on
one user's Drive, ids that are never reused, the per-ask caps, posters, owner and open rules, and what Drive holds
after a failure half-way or an app write in flight. Direct AskMedia calls against FakeGoogle's Drive; the single-tenant
bridge only answers `attachments: []`."""

import hashlib
import json
import logging
import re
import time
import tracemalloc

import httpx
import pytest

from server.drive import DriveClient
from server.hosted.attachments import (
    AskMedia,
    Item,
    next_attachment_id,
    plan_attach,
    plan_detach,
    prepare,
    prepare_all,
)
from server.hosted.media import ASK_MEDIA_MAX, ATTACH_MAX, POSTER_MAX_BYTES, VIDEO_MAX_BYTES
from server.hosted.records import HOSTED_ASK, HostedAsks
from server.mirror import ASK, app_properties, build_doc
from server.rules import Change, Conflict, Forbidden, Invalid, NotFound, Unavailable
from server.tokens import Identity

from .conftest import ALLEN as ALLEN_TOKEN
from .conftest import auth
from .fake_drive import app_action
from .hosted_support import DENIS_SUB, PUBLIC, connect, mcp, register, tool_error, tool_result
from .media_samples import box, data_url, jpeg, mp4, png, png_of_size

ALLEN = Identity("agent", "allen")
FEYNMAN = Identity("agent", "feynman")
DENIS = Identity("denis")
PIC = "https://pics.example/cat.png"
CLIP = "https://pics.example/clip.mp4"
DRIVE_LINK = "https://drive.google.com/file/d/abc/view"
MEDIA_PROPS = {"ask_id": "1", "kind": "media", "attachment": "1"}
MB = 1024 * 1024


def soon() -> float:
    return time.monotonic() + 30


def internet(google) -> httpx.Client:
    return httpx.Client(transport=google.public_transport())


def service(hosted, google, name: str = "Allen") -> tuple[AskMedia, str]:
    """An agent connected to Denis's account, its AskMedia (his Drive) and its access token."""
    client = register(hosted)
    token = connect(hosted, google, client["client_id"], agent=name)["access_token"]
    return AskMedia(hosted.app.state.pool.records(DENIS_SUB)), token


def image(raw: bytes | None = None, **extra) -> Item:
    return prepare({"data": data_url("image/png", raw or png(32, 24)), **extra}, None, soon())


def fetched(google, url: str, raw: bytes, mime: str, **extra) -> Item:
    google.publish(url, raw, headers={"Content-Type": mime})
    return prepare({"url": url, **extra}, internet(google), soon())


def link(url: str = DRIVE_LINK, **extra) -> Item:
    return prepare({"url": url, "link": True, **extra}, None, soon())


def with_free(raw: bytes, total: int) -> bytes:
    """The MP4 grown to exactly `total` bytes by a trailing `free` box."""
    grown = raw + box(b"free", bytes(total - len(raw) - 8))
    assert len(grown) == total
    return grown


def media_files(drive) -> list[str]:
    return sorted(f["name"] for f in drive.files.values() if f["name"].startswith("media-"))


def uploads(drive) -> int:
    """Files created so far (ask files and media alike: a multipart POST each, or a resumable session POST)."""
    return len(drive.calls("POST", "/upload/"))


def invalid(call, *args, **kwargs) -> str:
    with pytest.raises(Invalid) as raised:
        call(*args, **kwargs)
    return str(raised.value)


def old_ask(ask_id: int, status: str = "open", agent: str = "allen") -> dict:
    """An ask file from before attachments existed (no `attachments` field)."""
    return {"id": ask_id, "agent": agent, "title": "From before pictures", "context": None, "options": [],
            "links": [], "priority": "normal", "ref": None, "status": status, "answer": None, "answered_at": None,
            "resolution": None, "closed_by": None, "closed_at": None, "created_at": "2026-10-31T10:00:00.000Z",
            "updated_at": "2026-10-31T10:00:00.000Z", "events": []}


# ---------- create ----------

def test_create_with_image_and_link(hosted, google):
    media, token = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    raw = png(32, 24)
    google.publish(PIC, raw, headers={"Content-Type": "image/png"})
    items = prepare_all([{"url": PIC, "caption": "  The cat  "}, {"url": DRIVE_LINK, "link": True}], internet(google))
    ask = media.create(ALLEN, {"title": "Which one?", "options": ["Take #1", "Take #2"]}, items)

    assert sorted(f["name"] for f in drive.files.values()) == ["ask-1.json", "media-1-1.png"]
    file = drive.file("media-1-1.png")
    assert file["content"] == raw and file["mimeType"] == "image/png" and file["appProperties"] == MEDIA_PROPS
    added = ask["attachments"][0]["added_at"]
    assert added.endswith("Z") and ask["attachments"][1]["added_at"].endswith("Z")
    assert ask["attachments"] == [
        {"id": 1, "kind": "image", "mime": "image/png", "width": 32, "height": 24, "bytes": len(raw),
         "caption": "The cat", "source_url": None, "file_id": file["id"], "added_at": added},
        {"id": 2, "kind": "link", "url": DRIVE_LINK, "title": "drive.google.com", "provider": "drive",
         "caption": None, "poster": None, "added_at": ask["attachments"][1]["added_at"]},
    ]
    # the item shapes, key order included (the apps and the SPEC agree on them)
    assert list(ask["attachments"][0]) == ["id", "kind", "mime", "width", "height", "bytes", "caption", "source_url",
                                           "file_id", "added_at"]
    assert list(ask["attachments"][1]) == ["id", "kind", "url", "title", "provider", "caption", "poster", "added_at"]
    assert "events" not in ask and ask["id"] == 1 and ask["status"] == "open"

    doc = drive.doc(1)
    assert doc["attachments"] == ask["attachments"] and doc["rev"] == 1 and doc["writer"] == "bridge"
    assert [(e["id"], e["kind"], e["actor"]) for e in doc["events"]] == [
        (1, "created", "allen"), (2, "attached", "allen"), (3, "attached", "allen")]
    assert [e["payload"] for e in doc["events"][1:]] == [{"attachment": a} for a in ask["attachments"]]
    assert all(e["at"] == doc["created_at"] for e in doc["events"])
    assert drive.file("ask-1.json")["appProperties"] == {"ask_id": "1", "status": "open", "agent": "allen", "rev": "1"}
    # reads carry the attachments as metadata, through the collection and over MCP
    assert HostedAsks(media.records).get(1, with_events=False) == ask
    assert tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["attachments"] == ask["attachments"]
    assert tool_result(mcp(hosted, token, "asks_mine"))["asks"][0]["attachments"] == ask["attachments"]


def test_create_without_attachments_is_a_plain_create(hosted, google):
    media, _ = service(hosted, google)
    ask = media.create(ALLEN, {"title": "Plain?", "attachments": []}, [])
    assert ask["attachments"] == [] and google.drive(DENIS_SUB).doc(1)["attachments"] == []
    assert [e["kind"] for e in google.drive(DENIS_SUB).doc(1)["events"]] == ["created"]
    with pytest.raises(Forbidden):  # the human never creates asks (plan_create's rule), with pictures or without
        media.create(DENIS, {"title": "x"}, [image()])
    assert "title" in invalid(media.create, ALLEN, {"title": ""}, [image()])
    assert media_files(google.drive(DENIS_SUB)) == []  # refused before any write


def test_create_rolls_back_media(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    items = [image(png(8, 8)), image(png(9, 9)), image(png(10, 10))]

    drive.fail_next("POST", "/upload/", 503, after=1)  # the 2nd media write fails
    with pytest.raises(Unavailable):
        media.create(ALLEN, {"title": "Pics?"}, items)
    assert drive.files == {} and len(drive.calls("DELETE")) == 1  # the 1st picture was deleted again

    drive.fail_next("POST", "/upload/", 503, after=3)  # the three pictures go up, the ask file write fails
    with pytest.raises(Unavailable):
        media.create(ALLEN, {"title": "Pics?"}, items)
    assert drive.files == {} and len(drive.calls("DELETE")) == 4

    ask = media.create(ALLEN, {"title": "Pics?"}, items)  # Drive is back: the next id, every file in place
    assert ask["id"] == 3 and [a["id"] for a in ask["attachments"]] == [1, 2, 3]
    assert media_files(drive) == ["media-3-1.png", "media-3-2.png", "media-3-3.png"]


def test_create_checks_the_caps_before_writing(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    seven = [image(png(8, 8)) for _ in range(ATTACH_MAX + 1)]
    assert f"at most {ATTACH_MAX}" in invalid(media.create, ALLEN, {"title": "Pics?"}, seven)
    assert "at most 6" in invalid(prepare_all, [{"data": "x"}] * 7, None)  # before any item is looked at
    assert drive.files == {}


# ---------- attach ----------

def test_ids_never_reused(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    ask = media.create(ALLEN, {"title": "Which?"}, [])
    assert [a["id"] for a in media.attach(ALLEN, 1, image())["attachments"]] == [1]
    assert [a["id"] for a in media.attach(ALLEN, 1, link())["attachments"]] == [1, 2]
    assert [a["id"] for a in media.detach(ALLEN, 1, 2)["attachments"]] == [1]
    third = media.attach(ALLEN, 1, image(png(4, 4), caption="third"))
    assert [a["id"] for a in third["attachments"]] == [1, 3]
    assert media_files(drive) == ["media-1-1.png", "media-1-3.png"]
    assert drive.file("media-1-3.png")["appProperties"] == {**MEDIA_PROPS, "attachment": "3"}
    doc = drive.doc(1)
    assert [e["kind"] for e in doc["events"]] == ["created", "attached", "attached", "detached", "attached"]
    assert doc["events"][-1]["payload"]["attachment"]["id"] == 3 and doc["rev"] == 5
    assert third["updated_at"] == doc["events"][-1]["at"] > ask["updated_at"]
    assert next_attachment_id({**doc, "attachments": []}) == 4  # even with every item gone, the history counts


def test_seventh_attachment_refused(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Six?"}, [image(png(n, n)) for n in range(1, ATTACH_MAX + 1)])
    message = invalid(media.attach, ALLEN, 1, image())
    assert f"at most {ATTACH_MAX}" in message and "poster" in message
    assert len(media_files(drive)) == ATTACH_MAX  # refused before any write
    assert len(drive.doc(1)["events"]) == 1 + ATTACH_MAX


def test_forty_megabytes_per_ask(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    clip = with_free(mp4(640, 360, 10), VIDEO_MAX_BYTES)
    ask = media.create(ALLEN, {"title": "Clips?"}, [fetched(google, CLIP, clip, "video/mp4")])
    assert ask["attachments"][0]["kind"] == "video" and ask["attachments"][0]["bytes"] == VIDEO_MAX_BYTES
    ask = media.attach(ALLEN, 1, fetched(google, CLIP, clip, "video/mp4", caption="Second clip"))
    assert sum(a["bytes"] for a in ask["attachments"]) == ASK_MEDIA_MAX  # exactly 40 MB: still allowed
    assert drive.file("media-1-2.mp4")["content"] == clip
    message = invalid(media.attach, ALLEN, 1, image(png_of_size(1024)))
    assert "40 MB" in message and "Drive link" in message
    assert media_files(drive) == ["media-1-1.mp4", "media-1-2.mp4"]  # nothing written for the refused one
    assert "40 MB" in invalid(media.create, ALLEN, {"title": "Too much?"},
                              [fetched(google, CLIP, clip, "video/mp4")] * 2 + [image(png_of_size(1024))])
    assert "ask-2.json" not in [f["name"] for f in drive.files.values()]


def test_poster_rules(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Clip or card?"},
                 [fetched(google, CLIP, mp4(320, 240, 3), "video/mp4"), link(), image()])
    assert media_files(drive) == ["media-1-1.mp4", "media-1-3.png"]

    still = png(16, 9)
    ask = media.attach(ALLEN, 1, image(still), poster_for=1)
    poster_file = drive.file("media-1-1-poster.png")
    assert poster_file["content"] == still and poster_file["appProperties"] == MEDIA_PROPS
    video = ask["attachments"][0]
    assert video["poster"] == {"file_id": poster_file["id"], "mime": "image/png", "width": 16, "height": 9,
                               "bytes": len(still)}
    assert [a["id"] for a in ask["attachments"]] == [1, 2, 3]  # a poster is not an attachment
    event = drive.doc(1)["events"][-1]
    assert event["kind"] == "attached" and event["payload"] == {"attachment": video}

    # a second poster replaces the first and its file goes after the ask is written
    other = jpeg(20, 10)
    ask = media.attach(ALLEN, 1, prepare({"data": data_url("image/jpeg", other)}, None, soon()), poster_for=1)
    assert poster_file["id"] not in drive.files
    replaced = drive.file("media-1-1-poster.jpg")
    assert ask["attachments"][0]["poster"] == {"file_id": replaced["id"], "mime": "image/jpeg", "width": 20,
                                               "height": 10, "bytes": len(other)}
    assert media_files(drive) == ["media-1-1-poster.jpg", "media-1-1.mp4", "media-1-3.png"]
    assert drive.doc(1)["events"][-1]["payload"]["attachment"]["poster"]["file_id"] == replaced["id"]

    # a link card takes a poster too
    ask = media.attach(ALLEN, 1, image(still), poster_for=2)
    assert ask["attachments"][1]["poster"]["file_id"] == drive.file("media-1-2-poster.png")["id"]

    # refused before any write: a poster for an image, for an unknown id, over 1 MB, a video or a link as poster
    before = media_files(drive)
    assert "image" in invalid(media.attach, ALLEN, 1, image(still), poster_for=3)
    with pytest.raises(NotFound) as missing:
        media.attach(ALLEN, 1, image(still), poster_for=9)
    assert "#9" in str(missing.value)
    too_big = fetched(google, PIC, png_of_size(POSTER_MAX_BYTES + 1), "image/png")  # fine as a picture, not a poster
    assert "1 MB" in invalid(media.attach, ALLEN, 1, too_big, poster_for=1)
    assert "still" in invalid(media.attach, ALLEN, 1, fetched(google, CLIP, mp4(32, 32, 1), "video/mp4"),
                              poster_for=1)
    assert "link" in invalid(media.attach, ALLEN, 1, link(), poster_for=1)
    assert "caption" in invalid(media.attach, ALLEN, 1, image(still, caption="x"), poster_for=1)
    assert "poster_for" in invalid(media.attach, ALLEN, 1, image(still), poster_for="1")
    assert media_files(drive) == before

    # posters don't count toward 6
    for n in range(3):
        media.attach(ALLEN, 1, image(png(2 + n, 2)))
    assert len(media.attach(ALLEN, 1, image(still), poster_for=2)["attachments"]) == ATTACH_MAX
    assert "at most" in invalid(media.attach, ALLEN, 1, image(still))
    assert next_attachment_id(HostedAsks(media.records).get(1)) == 7
    assert len(media_files(drive)) == ATTACH_MAX - 1 + 2  # five files of their own (one card) and two posters


def test_detach_deletes_files(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Clip?"}, [fetched(google, CLIP, mp4(320, 240, 3), "video/mp4"), image()])
    media.attach(ALLEN, 1, image(png(16, 9)), poster_for=1)
    video = HostedAsks(media.records).get(1)["attachments"][0]
    assert media_files(drive) == ["media-1-1-poster.png", "media-1-1.mp4", "media-1-2.png"]

    ask = media.detach(ALLEN, 1, 1)
    assert [a["id"] for a in ask["attachments"]] == [2] and media_files(drive) == ["media-1-2.png"]
    doc = drive.doc(1)
    assert doc["events"][-1]["kind"] == "detached" and doc["events"][-1]["payload"] == {"attachment": video}
    assert doc["events"][-1]["actor"] == "allen" and doc["attachments"] == ask["attachments"]
    assert ask["updated_at"] == doc["events"][-1]["at"]
    with pytest.raises(NotFound) as missing:
        media.detach(ALLEN, 1, 1)
    assert "#1" in str(missing.value)
    with pytest.raises(NotFound):
        media.detach(ALLEN, 1, "2")
    with pytest.raises(NotFound):
        media.detach(ALLEN, 7, 1)
    ask = media.detach(ALLEN, 1, 2)
    assert ask["attachments"] == [] and media_files(drive) == []
    assert [e["kind"] for e in drive.doc(1)["events"]] == ["created", "attached", "attached", "attached",
                                                           "detached", "detached"]


def test_detach_deletes_only_this_asks_media(hosted, google, caplog):
    """A tampered ask file could name the ask file itself or another ask's picture as an attachment's file."""
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "One?"}, [image()])
    media.create(ALLEN, {"title": "Two?"}, [image()])
    doc = drive.doc(1)
    doc["attachments"][0]["file_id"] = drive.file("ask-2.json")["id"]
    doc["attachments"].append({**doc["attachments"][0], "id": 2, "file_id": drive.file("media-2-1.png")["id"]})
    doc["attachments"].append({**doc["attachments"][0], "id": 3, "file_id": "no-such-file"})
    doc["attachments"].append({**doc["attachments"][0], "id": 4, "file_id": 12})
    drive.write(drive.file("ask-1.json")["id"], doc, {"rev": "9"})
    with caplog.at_level(logging.WARNING, logger="asks.hosted.media"):
        for n in (1, 2, 3, 4):
            media.detach(ALLEN, 1, n)
    assert sorted(f["name"] for f in drive.files.values()) == ["ask-1.json", "ask-2.json", "media-1-1.png",
                                                              "media-2-1.png"]
    assert HostedAsks(media.records).get(1)["attachments"] == []
    assert sum("not deleted" in r.message for r in caplog.records) >= 2


def test_owner_and_open_only(hosted, google):
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Mine?"}, [image()])
    with pytest.raises(Forbidden) as other:
        media.attach(FEYNMAN, 1, image())
    assert "only the agent who created ask #1 (allen)" in str(other.value)
    with pytest.raises(Forbidden):
        media.detach(FEYNMAN, 1, 1)
    with pytest.raises(Forbidden):
        media.attach(DENIS, 1, image())
    with pytest.raises(Forbidden):
        media.detach(DENIS, 1, 1)
    assert media_files(drive) == ["media-1-1.png"]

    app_action(drive, 1, "answered", answer="This one")
    with pytest.raises(Conflict) as answered:
        media.attach(ALLEN, 1, image())
    assert str(answered.value) == "ask #1 is answered; only open asks can be edited"
    with pytest.raises(Conflict):
        media.detach(ALLEN, 1, 1)
    app_action(drive, 1, "closed")
    with pytest.raises(Conflict) as closed:
        media.attach(ALLEN, 1, image(png(2, 2)), poster_for=1)
    assert "is closed" in str(closed.value)
    with pytest.raises(Conflict):
        media.detach(ALLEN, 1, 1)
    assert media_files(drive) == ["media-1-1.png"] and len(drive.doc(1)["events"]) == 4
    with pytest.raises(NotFound):
        media.attach(ALLEN, 2, image())


def test_attach_survives_app_write(hosted, google, monkeypatch):
    """The app writes the file between the bridge's media write and its ask write: the ask write starts over from
    a fresh download, keeps what the app wrote, and the picture is uploaded once, not once per attempt."""
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Race?"}, [])
    original = DriveClient.get_meta
    raced = []

    def get_meta(self, file_id):
        if not raced:  # right between the bridge's read and its write: the user answers, then thinks again
            raced.append(True)
            app_action(drive, 1, "answered", answer="Hmm")
            app_action(drive, 1, "reopened", base=None)
        return original(self, file_id)

    monkeypatch.setattr(DriveClient, "get_meta", get_meta)
    before = uploads(drive)
    ask = media.attach(ALLEN, 1, image(), poster_for=0)
    assert ask["status"] == "open" and [a["id"] for a in ask["attachments"]] == [1]
    doc = drive.doc(1)
    assert [e["kind"] for e in doc["events"]] == ["created", "answered", "reopened", "attached"]
    assert doc["events"][1]["payload"]["answer"] == "Hmm" and doc["rev"] == 4 and doc["writer"] == "bridge"
    assert media_files(drive) == ["media-1-1.png"] and uploads(drive) == before + 1  # one media write, two attempts
    assert len(drive.calls("PATCH")) == 1 and drive.calls("DELETE") == []


def test_attach_after_answer_deletes_media(hosted, google, monkeypatch):
    """The user answers while the picture is being written: the ask is no longer open, so the attach is a 409,
    the user's answer stays and the file the bridge had just written is deleted again."""
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Race?"}, [])
    original = DriveClient.upload_media

    def upload_media(self, *args, **kwargs):
        meta = original(self, *args, **kwargs)
        app_action(drive, 1, "answered", answer="Take the red one")  # lands right after the media write
        return meta

    monkeypatch.setattr(DriveClient, "upload_media", upload_media)
    with pytest.raises(Conflict) as raised:
        media.attach(ALLEN, 1, image())
    assert str(raised.value) == "ask #1 is answered; only open asks can be edited"
    doc = drive.doc(1)
    assert doc["answer"] == "Take the red one" and doc["attachments"] == [] and doc["writer"] == "app"
    assert [e["kind"] for e in doc["events"]] == ["created", "answered"]
    assert media_files(drive) == [] and len(drive.calls("DELETE")) == 1
    monkeypatch.setattr(DriveClient, "upload_media", original)

    # the same race caught at the modifiedTime check: the write starts over, sees the answer, gives up
    app_action(drive, 1, "reopened")
    checked = DriveClient.get_meta
    raced = []

    def get_meta(self, file_id):
        if not raced:
            raced.append(True)
            app_action(drive, 1, "answered", answer="Blue")
        return checked(self, file_id)

    monkeypatch.setattr(DriveClient, "get_meta", get_meta)
    with pytest.raises(Conflict):
        media.attach(ALLEN, 1, image())
    assert drive.doc(1)["answer"] == "Blue" and media_files(drive) == [] and len(drive.calls("DELETE")) == 2
    # answered before the call: refused before any write
    with pytest.raises(Conflict):
        media.attach(ALLEN, 1, image())
    assert len(drive.calls("DELETE")) == 2 and len(drive.doc(1)["events"]) == 4


def test_second_attach_in_flight_gets_a_conflict(hosted, google, monkeypatch):
    """Two attaches of one agent on one ask at once: both pick id 1 before writing; the second ask write finds the
    id taken and deletes its own file instead of renumbering it (an option may already say 'Take #1')."""
    media, _ = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    media.create(ALLEN, {"title": "Race?"}, [])
    original = DriveClient.upload_media
    first = []

    def upload_media(self, *args, **kwargs):
        meta = original(self, *args, **kwargs)
        if not first:
            first.append(True)
            monkeypatch.setattr(DriveClient, "upload_media", original)
            media.attach(ALLEN, 1, image(png(5, 5), caption="the other"))  # the other call completes in between
        return meta

    monkeypatch.setattr(DriveClient, "upload_media", upload_media)
    with pytest.raises(Conflict) as raised:
        media.attach(ALLEN, 1, image())
    assert "try again" in str(raised.value)
    ask = HostedAsks(media.records).get(1)
    assert [(a["id"], a["caption"]) for a in ask["attachments"]] == [(1, "the other")]
    assert media_files(drive) == ["media-1-1.png"] and len(drive.calls("DELETE")) == 1
    assert drive.file("media-1-1.png")["id"] == ask["attachments"][0]["file_id"]


def test_old_asks_take_attachments(hosted, google):
    media, token = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    old = old_ask(7)
    drive.add("ask-7.json", build_doc(old, 4), app_properties(old, 4, ASK))
    assert tool_result(mcp(hosted, token, "ask_get", {"id": 7}))["attachments"] == []
    ask = media.attach(ALLEN, 7, image())
    assert [a["id"] for a in ask["attachments"]] == [1] and drive.doc(7)["attachments"] == ask["attachments"]
    assert media_files(drive) == ["media-7-1.png"]
    # an ask file whose attachments are not a list of objects: the junk is dropped, never written back as is
    doc = drive.doc(7)
    doc["attachments"] = [doc["attachments"][0], "junk", 5]
    drive.write(drive.file("ask-7.json")["id"], doc, {})
    assert len(HostedAsks(media.records).get(7)["attachments"]) == 1
    doc["attachments"] = "nope"
    drive.write(drive.file("ask-7.json")["id"], doc, {})
    assert HostedAsks(media.records).get(7)["attachments"] == []
    assert HOSTED_ASK.fields[-1] == "attachments" and "attachments" not in ASK.fields


# ---------- prepare: one item of ask_create / the arguments of ask_attach ----------

def test_prepare_items(google):
    raw = png(10, 20)
    http = internet(google)
    google.publish(PIC, raw, headers={"Content-Type": "image/png"})
    item = prepare({"url": PIC, "caption": " Cat ", "source_url": " https://example.com/cat "}, http, soon())
    assert (item.kind, item.url, item.title, item.caption, item.source_url) == ("image", None, None, "Cat",
                                                                                 "https://example.com/cat")
    assert item.checked.mime == "image/png" and item.checked.raw == raw
    clip = mp4(64, 48, 2)
    google.publish(CLIP, clip, headers={"Content-Type": "video/mp4"})
    assert prepare({"url": CLIP}, http, soon()).kind == "video"
    data = prepare({"data": data_url("image/png", raw)}, None, soon())
    assert data.kind == "image" and data.checked.bytes == len(raw) and data.caption is None
    card = prepare({"url": "https://docs.google.com/spreadsheets/d/x/edit", "link": True, "title": " Costs "}, None,
                   soon())
    assert card == Item("link", None, "https://docs.google.com/spreadsheets/d/x/edit", "Costs", None, None)
    assert prepare({"url": "https://Example.com/a?b=c", "link": True}, None, soon()).title == "example.com"
    long_host = ".".join(["a" * 60] * 4) + ".example"  # 251 chars, a valid DNS name
    assert prepare({"url": f"https://{long_host}/x", "link": True}, None, soon()).title == long_host[:200]
    assert "source_url is for pictures and videos; a link card's url is the original" in invalid(
        prepare, {"url": "https://x.example/a", "link": True, "source_url": "https://example.com/orig"}, None, soon())

    assert "exactly one of url and data" in invalid(prepare, {"caption": "x"}, None, soon())
    assert "exactly one of url and data" in invalid(prepare, {"url": PIC, "data": "data:x"}, None, soon())
    assert "exactly one of url and data" in invalid(prepare, {"url": "", "data": ""}, None, soon())
    assert "unknown fields: bytes, mime" in invalid(prepare, {"url": PIC, "mime": "x", "bytes": 1}, None, soon())
    assert "must be an object" in invalid(prepare, "https://x.example/", None, soon())
    assert "link" in invalid(prepare, {"data": data_url("image/png", raw), "link": True}, None, soon())
    assert "title" in invalid(prepare, {"url": PIC, "title": "Named"}, http, soon())
    assert "link must be true or false" in invalid(prepare, {"url": PIC, "link": "yes"}, None, soon())
    assert "caption is too long" in invalid(prepare, {"data": data_url("image/png", raw), "caption": "x" * 301},
                                            None, soon())
    assert "title is too long" in invalid(prepare, {"url": PIC, "link": True, "title": "x" * 201}, None, soon())
    assert "source_url must name a host" in invalid(prepare, {"url": PIC, "link": True,
                                                              "source_url": "https://8.8.8.8/x"}, None, soon())
    assert "url must use https" in invalid(prepare, {"url": "http://example.com/x", "link": True}, None, soon())
    assert "Google Drive" in invalid(prepare, {"url": DRIVE_LINK}, http, soon())
    assert "data must be" in invalid(prepare, {"data": "data:image/gif;base64,R0lGOD"}, None, soon())
    assert http.get(PIC).status_code == 200  # the fetch tests themselves live in test_hosted_media_fetch


def test_prepare_all_names_the_bad_item(google):
    http = internet(google)
    google.publish(PIC, png(4, 4), headers={"Content-Type": "image/png"})
    message = invalid(prepare_all, [{"url": PIC}, {"url": "https://x.example/a", "link": True}, {"caption": "x"}],
                      http)
    assert message.startswith("attachments[2]: ") and "exactly one of url and data" in message
    assert "attachments must be a list" in invalid(prepare_all, {"url": PIC}, http)
    assert [i.kind for i in prepare_all([{"url": PIC}, {"url": "https://x.example/a", "link": True}], http)] == \
        ["image", "link"]
    assert prepare_all([], http) == [] and prepare_all(None, http) == []
    big = with_free(mp4(16, 16, 1), VIDEO_MAX_BYTES)
    google.publish(CLIP, big, headers={"Content-Type": "video/mp4"})
    message = invalid(prepare_all, [{"url": CLIP}, {"url": CLIP}, {"url": CLIP}], http)
    assert message.startswith("attachments[2]: ") and "40 MB" in message  # over the cap: the 3rd is not fetched


def test_prepare_all_reads_no_more_than_the_ask_has_room_for():
    """A create holds at most its 40 MB (the budget of its 2 slots): after a 20 MB and a 15 MB video the third fetch
    may take the 5 MB left, so its body is cut there (or refused by its Content-Length), never read whole first."""
    big, mid = with_free(mp4(640, 360, 3.0), VIDEO_MAX_BYTES), with_free(mp4(640, 360, 3.0), 15 * MB)
    bodies = {"/1.mp4": big, "/2.mp4": mid, "/3.mp4": big}
    served: dict[str, int] = {}
    declared: set[str] = set()  # paths that send their Content-Length

    class Body(httpx.SyncByteStream):  # counts what the server got to send, 1 MB at a time
        def __init__(self, path: str):
            self.path = path

        def __iter__(self):
            view = memoryview(bodies[self.path])
            for i in range(0, len(view), MB):
                served[self.path] = served.get(self.path, 0) + MB
                yield bytes(view[i:i + MB])

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        length = {"Content-Length": str(len(bodies[path]))} if path in declared else {}
        return httpx.Response(200, headers={"Content-Type": "video/mp4", **length}, stream=Body(path))

    http = httpx.Client(transport=httpx.MockTransport(handle))
    items = [{"url": f"https://clips.example/{n}.mp4"} for n in (1, 2, 3)]
    room = ASK_MEDIA_MAX - VIDEO_MAX_BYTES - 15 * MB
    tracemalloc.start()
    try:
        message = invalid(prepare_all, items, http)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert served == {"/1.mp4": VIDEO_MAX_BYTES, "/2.mp4": 15 * MB, "/3.mp4": room + MB}  # + the chunk that crossed
    assert message.startswith(f"attachments[2]: the attachments would take at least {ASK_MEDIA_MAX + MB} bytes; "
                              "at most 40 MB are stored per ask")
    assert peak < ASK_MEDIA_MAX + 4 * MB  # the two bodies kept and ≤ 6 MB of the third (it was read whole: ~58 MB)
    # a Content-Length over the room left: refused before one byte of it is read, with the exact total
    served.clear()
    declared.add("/3.mp4")
    message = invalid(prepare_all, items, http)
    assert "/3.mp4" not in served
    assert message.startswith(f"attachments[2]: the attachments would take {2 * VIDEO_MAX_BYTES + 15 * MB} bytes; ")


# ---------- the pure rules ----------

def test_plan_attach_and_detach_rules():
    ask = {**old_ask(3), "attachments": [], "events": [
        {"id": 1, "ask_id": 3, "at": "t", "actor": "allen", "kind": "created", "payload": {}},
        {"id": 2, "ask_id": 3, "at": "t", "actor": "allen", "kind": "attached", "payload": {"attachment": {"id": 2}}},
        {"id": 3, "ask_id": 3, "at": "t", "actor": "allen", "kind": "detached", "payload": {"attachment": {"id": 2}}},
        {"id": 4, "kind": "attached", "payload": "junk"},
        {"id": 5, "kind": "attached", "payload": {"attachment": {"id": True}}},
    ]}
    assert next_attachment_id(ask) == 3
    assert next_attachment_id({"attachments": [{"id": 5}, {"id": "9"}, "x"], "events": "nope"}) == 6
    picture = {"id": 3, "kind": "image", "mime": "image/png", "width": 1, "height": 1, "bytes": 10, "caption": None,
               "source_url": None, "file_id": "f1", "added_at": "2026-11-12T10:00:00.000Z"}
    current, change = plan_attach(ALLEN, picture, 0, lambda: ask, "now")
    assert current is ask and isinstance(change, Change)
    assert change == Change({"attachments": [picture], "updated_at": "now"}, "attached", {"attachment": picture}, "now")
    with pytest.raises(Conflict) as taken:  # id 2 was taken while the file was being written
        plan_attach(ALLEN, {**picture, "id": 2}, 0, lambda: ask, "now")
    assert "try again" in str(taken.value)
    with pytest.raises(Forbidden):
        plan_attach(FEYNMAN, picture, 0, lambda: ask, "now")
    with pytest.raises(Conflict):
        plan_attach(ALLEN, picture, 0, lambda: {**ask, "status": "closed"}, "now")
    with pytest.raises(NotFound):
        plan_attach(ALLEN, {"file_id": "p", "mime": "image/png", "width": 1, "height": 1, "bytes": 1}, 2,
                    lambda: ask, "now")
    _, detach = plan_detach(ALLEN, 3, lambda: {**ask, "attachments": [picture]}, "later")
    assert detach == Change({"attachments": [], "updated_at": "later"}, "detached", {"attachment": picture}, "later")
    with pytest.raises(NotFound):
        plan_detach(ALLEN, 4, lambda: {**ask, "attachments": [picture]}, "later")


# ---------- MCP: ask_create(attachments=…), ask_attach, ask_upload, ask_detach ----------

SHOW = ("Show, don't describe: attach the picture or clip your user decides on. The apps number attachments 1, 2, 3 in "
        "order, so options can say 'Take #2'. A file on Google Drive your user can open: link=true with a poster. A "
        "file you can read and they can't: upload its bytes and set source_url to the original. Prefer "
        "ask_create(attachments=…), so the question appears with its pictures.")
IMAGE_KEYS = ["id", "kind", "mime", "width", "height", "bytes", "caption", "source_url", "file_id", "added_at"]
VIDEO_KEYS = ["id", "kind", "mime", "width", "height", "duration_ms", "bytes", "caption", "source_url", "file_id",
              "poster", "added_at"]
LINK_KEYS = ["id", "kind", "url", "title", "provider", "caption", "poster", "added_at"]
BUSY = "busy, try again in a few seconds"


def failure(hosted, token: str, name: str, arguments: dict) -> str:
    """A tool error's own text: the SDK puts "Error executing tool <name>: " in front of every one."""
    error = tool_error(mcp(hosted, token, name, arguments))
    prefix = f"Error executing tool {name}: "
    assert error.startswith(prefix), error
    return error.removeprefix(prefix)


def tools_list(hosted, token: str) -> dict[str, dict]:
    response = hosted.post("/mcp", headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18",
                                            "Accept": "application/json, text/event-stream"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200, response.text
    return {t["name"]: t for t in response.json()["result"]["tools"]}


def metadata_only(asks: list[dict]) -> None:
    """What the read tools show of attachments: the stored shapes, never the bytes or a link to them."""
    keys = {"image": IMAGE_KEYS, "video": VIDEO_KEYS, "link": LINK_KEYS}
    for ask in asks:
        for item in ask["attachments"]:
            assert list(item) == keys[item["kind"]], item
    text = json.dumps(asks)
    assert "data:" not in text and "base64" not in text and "alt=media" not in text and "googleapis" not in text


def test_mcp_create_with_attachments(hosted, google):
    _, token = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    raw = png(40, 30)
    google.publish(PIC, raw, headers={"Content-Type": "image/png"})
    ask = tool_result(mcp(hosted, token, "ask_create", {
        "title": "Which one?", "options": ["Take #1", "Take #2"],
        "attachments": [{"url": PIC, "caption": "The cat"}, {"url": DRIVE_LINK, "link": True, "title": "The deck"}]}))
    file = drive.file("media-1-1.png")
    assert file["content"] == raw and file["appProperties"] == MEDIA_PROPS
    assert ask["attachments"] == [
        {"id": 1, "kind": "image", "mime": "image/png", "width": 40, "height": 30, "bytes": len(raw),
         "caption": "The cat", "source_url": None, "file_id": file["id"],
         "added_at": ask["attachments"][0]["added_at"]},
        {"id": 2, "kind": "link", "url": DRIVE_LINK, "title": "The deck", "provider": "drive", "caption": None,
         "poster": None, "added_at": ask["attachments"][1]["added_at"]}]
    assert [e["kind"] for e in drive.doc(1)["events"]] == ["created", "attached", "attached"]
    # the read tools: metadata only
    mine = tool_result(mcp(hosted, token, "asks_mine"))["asks"]
    listed = tool_result(mcp(hosted, token, "asks_list"))["asks"]
    got = tool_result(mcp(hosted, token, "ask_get", {"id": 1}))
    assert mine[0]["attachments"] == listed[0]["attachments"] == got["attachments"] == ask["attachments"]
    assert [e["payload"] for e in got["events"][1:]] == [{"attachment": a} for a in ask["attachments"]]
    metadata_only(mine + listed + [got])
    # without attachments, or with [], ask_create is the plain one
    assert tool_result(mcp(hosted, token, "ask_create", {"title": "Plain?"}))["attachments"] == []
    assert tool_result(mcp(hosted, token, "ask_create", {"title": "Empty?", "attachments": []}))["attachments"] == []
    assert media_files(drive) == ["media-1-1.png"]


def test_mcp_bad_item_creates_nothing(hosted, google):
    _, token = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    picture = {"data": data_url("image/png", png(8, 8))}
    google.publish(PIC, png(10, 10), headers={"Content-Type": "image/png"})
    for items, words in (
            ([{"url": PIC}, {"url": "http://pics.example/cat.png"}], "invalid: attachments[1]: url must use https"),
            ([picture, picture, {"url": DRIVE_LINK}], "invalid: attachments[2]: AskHooman can't open files in Google "
                                                       "Drive: download it and use ask_upload, or attach the link "
                                                       "with link=true"),
            ([picture, {"data": "data:image/gif;base64,R0lGODlh"}], "invalid: attachments[1]: data must be a "
                                                                    "data:image/png"),
            ([picture, {"url": PIC, "data": picture["data"]}], "invalid: attachments[1]: exactly one of url and data"),
            ([picture, {"data": picture["data"], "title": "x"}], "invalid: attachments[1]: title is for link cards"),
            ([picture] * 7, "invalid: attachments: at most 6 items, got 7")):
        error = failure(hosted, token, "ask_create", {"title": "Which one?", "attachments": items})
        assert error.startswith(words), error
    # an item with a field the schema doesn't have is refused by the schema, naming it
    assert "colour" in failure(hosted, token, "ask_create", {"title": "Which one?",
                                                            "attachments": [{**picture, "colour": "red"}]})
    # a field error comes first: nothing is fetched for a create that can't happen
    assert failure(hosted, token, "ask_create", {"title": " ", "attachments": [{"url": CLIP}]}).startswith(
        "invalid: title")
    assert tool_result(mcp(hosted, token, "asks_mine", {"status": "all"}))["asks"] == []
    assert [f["name"] for f in drive.files.values()] == []


def test_mcp_upload_curl(hosted, google):
    _, token = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Which one?"}))
    raw = jpeg(64, 48)
    issued = tool_result(mcp(hosted, token, "ask_upload", {"id": 1, "mime": "image/jpeg", "bytes": len(raw),
                                                           "caption": "From my disk", "source_url": DRIVE_LINK}))
    secret = issued["upload"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert re.fullmatch(r"qtu_[A-Za-z0-9_-]{43}", secret)
    assert list(hosted.store.all("uploads")) == [hashlib.sha256(secret.encode()).hexdigest()]
    assert issued == {"upload": {"url": f"{PUBLIC}/api/uploads", "method": "PUT",
                                 "headers": {"Authorization": f"Bearer {secret}", "Content-Type": "image/jpeg"},
                                 "expires_at": issued["upload"]["expires_at"]},
                      "curl": f"curl -sS --fail-with-body -X PUT -H 'Authorization: Bearer {secret}' "
                              f"-H 'Content-Type: image/jpeg' --data-binary @FILE {PUBLIC}/api/uploads"}
    assert media_files(drive) == []
    # what the curl line sends
    sent = hosted.put("/api/uploads", content=raw, headers=issued["upload"]["headers"])
    assert sent.status_code == 201, sent.text
    attachment = tool_result(mcp(hosted, token, "asks_mine"))["asks"][0]["attachments"][0]
    assert attachment == sent.json()["attachment"]
    assert (attachment["caption"], attachment["source_url"], attachment["mime"]) == ("From my disk", DRIVE_LINK,
                                                                                     "image/jpeg")
    assert drive.file("media-1-1.jpg")["content"] == raw
    # refusals come back as tool errors, before any token exists
    for arguments, words in (({"mime": "image/gif", "bytes": 10}, "invalid: mime must be image/png, image/jpeg, "
                                                                  "image/webp or video/mp4: convert to PNG, JPEG or "
                                                                  "WebP; send motion as MP4"),
                             ({"mime": "video/mp4", "bytes": VIDEO_MAX_BYTES + 1}, "invalid: a video may be at most"),
                             ({"mime": "image/png", "bytes": 10, "poster_for": 1}, "invalid: attachment #1 is an "
                                                                                   "image: a poster is a still"),
                             ({"mime": "image/png", "bytes": 0}, "invalid: bytes must be the file's size")):
        error = failure(hosted, token, "ask_upload", {"id": 1, **arguments})
        assert error.startswith(words), error
    assert failure(hosted, token, "ask_upload", {"id": 9, "mime": "image/png", "bytes": 10}).startswith("not found: ")
    assert hosted.store.all("uploads") == {}


def test_mcp_attach_and_detach(hosted, google):
    _, token = service(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Which one?"}))
    clip = mp4(320, 240, 3)
    google.publish(CLIP, clip, headers={"Content-Type": "video/mp4"})
    by_url = tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "url": CLIP, "caption": "The clip"}))
    assert [(a["id"], a["kind"], a["caption"]) for a in by_url["attachments"]] == [(1, "video", "The clip")]
    by_data = tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "data": data_url("image/png", png(8, 8))}))
    assert [a["kind"] for a in by_data["attachments"]] == ["video", "image"]
    card = tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "url": DRIVE_LINK, "link": True}))
    assert (card["attachments"][2]["title"], card["attachments"][2]["provider"]) == ("drive.google.com", "drive")
    still = png(16, 9)
    poster = tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "data": data_url("image/png", still),
                                                           "poster_for": 3}))
    assert poster["attachments"][2]["poster"]["bytes"] == len(still) and len(poster["attachments"]) == 3
    assert media_files(drive) == ["media-1-1.mp4", "media-1-2.png", "media-1-3-poster.png"]
    metadata_only([tool_result(mcp(hosted, token, "ask_get", {"id": 1}))])
    assert failure(hosted, token, "ask_attach", {"id": 1}) == "invalid: exactly one of url and data is needed"
    assert failure(hosted, token, "ask_attach", {"id": 1, "data": data_url("image/png", still), "title": "x"}) \
        .startswith("invalid: title is for link cards")

    detached = tool_result(mcp(hosted, token, "ask_detach", {"id": 1, "attachment_id": 3}))
    assert [a["id"] for a in detached["attachments"]] == [1, 2]
    assert media_files(drive) == ["media-1-1.mp4", "media-1-2.png"]
    assert failure(hosted, token, "ask_detach", {"id": 1, "attachment_id": 3}) == \
        "not found: ask #1 has no attachment #3"
    # ids are never reused
    again = tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "url": DRIVE_LINK, "link": True}))
    assert [a["id"] for a in again["attachments"]] == [1, 2, 4]

    # the owner agent only, open asks only
    feynman = connect(hosted, google, register(hosted)["client_id"], agent="Feynman")["access_token"]
    calls = (("ask_attach", {"id": 1, "url": DRIVE_LINK, "link": True}),
             ("ask_upload", {"id": 1, "mime": "image/png", "bytes": 10}),
             ("ask_detach", {"id": 1, "attachment_id": 1}))
    for name, arguments in calls:
        assert failure(hosted, feynman, name, arguments) == \
            "forbidden: only the agent who created ask #1 (allen) can edit it", name
    app_action(drive, 1, "answered", answer="The clip")
    for name, arguments in calls:
        assert failure(hosted, token, name, arguments) == \
            "conflict: ask #1 is answered; only open asks can be edited", name
    assert media_files(drive) == ["media-1-1.mp4", "media-1-2.png"] and hosted.store.all("uploads") == {}


def test_rate_limit(hosted, google):
    """60 attachment calls an hour per agent (each item of a create, each attach, upload request and detach), one
    budget with the REST routes: the 61st is a tool error."""
    _, token = service(hosted, google)
    picture = {"data": data_url("image/png", png(8, 8))}
    tool_result(mcp(hosted, token, "ask_create", {"title": "Which one?", "attachments": [picture, picture]}))  # 2
    tool_result(mcp(hosted, token, "ask_attach", {"id": 1, **picture}))  # 3
    tool_result(mcp(hosted, token, "ask_upload", {"id": 1, "mime": "image/png", "bytes": 10}))  # 4
    assert hosted.post("/api/asks/1/attachments", json=picture,
                       headers={"Authorization": f"Bearer {token}"}).status_code == 201  # 5: REST counts too
    for _ in range(55):  # 6…60: a refused detach is still a call
        assert failure(hosted, token, "ask_detach", {"id": 1, "attachment_id": 99}).startswith("not found: ")
    for name, arguments in (("ask_attach", {"id": 1, **picture}),
                            ("ask_upload", {"id": 1, "mime": "image/png", "bytes": 10}),
                            ("ask_detach", {"id": 1, "attachment_id": 1}),
                            ("ask_create", {"title": "More?", "attachments": [picture]})):
        assert failure(hosted, token, name, arguments) == (
            "rate limited: too many attachment calls: at most 60 an hour per agent (each attach, upload, detach and "
            "each item of an ask_create counts)"), name
    assert len(tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["attachments"]) == 4
    # a plain ask is not an attachment call
    assert tool_result(mcp(hosted, token, "ask_create", {"title": "Plain?"}))["id"] == 2
    assert hosted.app.state.media.guard.slots.in_use == 0


def test_busy(hosted, google):
    """At most 4 slots of media in flight per instance, shared with the REST routes: a call that would fetch gets
    the busy text alone (no "unavailable: " in front); a call that holds no media goes through."""
    _, token = service(hosted, google)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Which one?"}))
    google.publish(PIC, png(10, 10), headers={"Content-Type": "image/png"})
    slots = hosted.app.state.media.guard.slots
    assert slots.take(4)
    assert failure(hosted, token, "ask_attach", {"id": 1, "url": PIC}) == BUSY
    assert failure(hosted, token, "ask_create", {"title": "Pics?", "attachments": [{"url": PIC}]}) == BUSY
    assert tool_result(mcp(hosted, token, "asks_mine"))["counts"]["open"] == 1  # the busy create made nothing
    assert tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "data": data_url("image/png", png(8, 8))}))
    assert tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "url": DRIVE_LINK, "link": True}))
    assert tool_result(mcp(hosted, token, "ask_upload", {"id": 1, "mime": "image/png", "bytes": 10}))
    assert tool_result(mcp(hosted, token, "ask_detach", {"id": 1, "attachment_id": 1}))
    slots.give(1)  # a create that fetches needs 2 slots, an attach by url 1
    assert failure(hosted, token, "ask_create", {"title": "Pics?", "attachments": [{"url": PIC}]}) == BUSY
    assert tool_result(mcp(hosted, token, "ask_attach", {"id": 1, "url": PIC}))
    slots.give(1)
    assert tool_result(mcp(hosted, token, "ask_create", {"title": "Pics?", "attachments": [{"url": PIC}]}))
    slots.give(2)
    assert slots.in_use == 0


def test_docstring_sentence(hosted, google):
    _, token = service(hosted, google)
    tools = tools_list(hosted, token)
    for name in ("ask_create", "ask_attach", "ask_upload"):
        assert SHOW in tools[name]["description"], name
    assert "attachments[2]: …" in tools["ask_create"]["description"]
    assert "ask_update options" in tools["ask_detach"]["description"]
    schema = tools["ask_create"]["inputSchema"]
    attachments = schema["properties"]["attachments"]
    assert attachments["type"] == "array" and attachments["default"] == []
    item = schema["$defs"][attachments["items"]["$ref"].rsplit("/", 1)[1]]
    assert set(item["properties"]) == {"url", "data", "link", "title", "caption", "source_url"}
    assert item["additionalProperties"] is False and "required" not in item
    assert set(tools["ask_attach"]["inputSchema"]["properties"]) == {
        "id", "url", "data", "link", "title", "caption", "source_url", "poster_for"}
    assert tools["ask_attach"]["inputSchema"]["required"] == ["id"]
    assert set(tools["ask_upload"]["inputSchema"]["properties"]) == {
        "id", "mime", "bytes", "caption", "source_url", "poster_for"}
    assert tools["ask_upload"]["inputSchema"]["required"] == ["id", "mime", "bytes"]
    assert tools["ask_detach"]["inputSchema"]["required"] == ["id", "attachment_id"]
    for name in ("ask_attach", "ask_upload", "ask_detach"):
        hints = tools[name]["annotations"]
        assert tools[name]["title"] == hints["title"] and hints["readOnlyHint"] is False, name
    # detach deletes the item's files from Drive: the one destructive tool
    assert [name for name, t in tools.items() if t["annotations"]["destructiveHint"]] == ["ask_detach"]
    # the house rules and /llms.txt say it too
    assert "<b>Show, don't describe.</b>" in hosted.get("/agents").text
    assert "ask_attach, ask_upload, ask_detach" in hosted.get("/llms.txt").text


# ---------- the single-tenant bridge ----------

def test_single_tenant_attachments_empty(client):
    created = client.post("/api/asks", json={"title": "Plain?", "attachments": []}, headers=auth(ALLEN_TOKEN))
    assert created.status_code == 201 and created.json()["attachments"] == []
    ask_id = created.json()["id"]
    assert client.get(f"/api/asks/{ask_id}", headers=auth(ALLEN_TOKEN)).json()["attachments"] == []
    assert client.get("/api/asks", headers=auth(ALLEN_TOKEN)).json()["asks"][0]["attachments"] == []
    refused = client.post("/api/asks", json={"title": "Pic?", "attachments": [{"url": PIC}]}, headers=auth(ALLEN_TOKEN))
    assert refused.status_code == 422 and refused.json()["detail"] == "attachments need the hosted AskHooman"
    assert client.get("/api/asks", headers=auth(ALLEN_TOKEN)).json()["counts"]["open"] == 1
