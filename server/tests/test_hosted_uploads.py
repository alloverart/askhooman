"""Hosted bridge: one-time uploads and the attachment REST (SPEC v5 step 4, "How the bytes get in" and "REST"):
POST /api/asks/{id}/uploads hands out a `qtu_` token bound to the user, agent, ask, type, size, caption, source_url
and poster_for; PUT /api/uploads takes exactly the declared bytes once; POST/DELETE …/attachments and a create with
`attachments`; the per-instance guard (the `media` bucket and the 4 slots) and the body caps."""

import hashlib
import json
import re
import time
from datetime import datetime, timezone

import anyio
import pytest

from server.app import MAX_BODY
from server.hosted import media_api
from server.hosted.media_api import Slots
from server.hosted.ratelimit import LIMITS, RateLimiter

from .conftest import ALLEN as ALLEN_TOKEN
from .conftest import auth
from .fake_drive import app_action
from .hosted_support import DENIS_SUB, OLGA_EMAIL, OLGA_SUB, PUBLIC, connect, id_token, mcp, register
from .media_samples import box, data_url, jpeg, mp4, png, png_of_size

PIC = "https://pics.example/cat.png"
CLIP = "https://pics.example/clip.mp4"
BIG_CLIP = "https://pics.example/big.mp4"
DRIVE_LINK = "https://drive.google.com/file/d/abc/view"
MEDIA_PROPS = {"ask_id": "1", "kind": "media", "attachment": "1"}
GRANT_FIELDS = {"user", "agent_id", "agent", "ask_id", "mime", "bytes", "caption", "source_url", "poster_for",
                "expires_at"}
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00" \
      b"\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


# ---------- helpers ----------

def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def allen(hosted, google, sub: str = DENIS_SUB, email: str = "owner@example.com") -> str:
    """An OAuth agent of the user (its sign-in also gives the bridge the user's Drive): its access token."""
    return connect(hosted, google, register(hosted)["client_id"], agent="Allen", sub=sub, email=email)["access_token"]


def key_for(hosted, google, name: str) -> str:
    """Another agent of Denis's, with an agent key from the app."""
    response = hosted.post("/api/agents/keys", json={"name": name}, headers=id_token(google))
    assert response.status_code == 201, response.text
    return response.json()["key"]


def new_ask(hosted, token: str, title: str = "Which one?", **fields) -> dict:
    response = hosted.post("/api/asks", json={"title": title, **fields}, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


def request_upload(hosted, token: str, ask_id: int, **body):
    return hosted.post(f"/api/asks/{ask_id}/uploads", json=body, headers=bearer(token))


def upload(hosted, token: str, ask_id: int, raw: bytes, mime: str = "image/png", **extra) -> dict:
    response = request_upload(hosted, token, ask_id, mime=mime, bytes=len(raw), **extra)
    assert response.status_code == 201, response.text
    return response.json()


def upload_token(issued: dict) -> str:
    return issued["upload"]["headers"]["Authorization"].removeprefix("Bearer ")


def put(hosted, issued: dict, raw: bytes, **headers):
    return hosted.put("/api/uploads", content=raw, headers={**issued["upload"]["headers"], **headers})


def with_free(raw: bytes, total: int) -> bytes:
    """The MP4 grown to exactly `total` bytes by a trailing `free` box."""
    return raw + box(b"free", bytes(total - len(raw) - 8))


def media_files(drive) -> list[str]:
    return sorted(f["name"] for f in drive.files.values() if f["name"].startswith("media-"))


def slots_in_use(hosted) -> int:
    return hosted.app.state.media.guard.slots.in_use


def raw_put(hosted, headers: dict, chunks: list[bytes], pause: float = 0.0,
            stall: bool = False) -> tuple[int, dict, int]:
    """PUT /api/uploads straight into the ASGI app, as a client that may lie about its Content-Length, wait `pause`
    seconds before each chunk, or (`stall`) send nothing more after its chunks: returns (status, JSON body, how many
    body chunks the server took)."""
    chunks = list(chunks)
    taken = []
    sent: list[dict] = []

    async def receive():
        if chunks:
            await anyio.sleep(pause)
            chunk = chunks.pop(0)
            taken.append(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks) or stall}
        if stall:
            await anyio.sleep_forever()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT", "scheme": "https",
             "path": "/api/uploads", "raw_path": b"/api/uploads", "query_string": b"", "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": ("203.0.113.7", 4242), "server": ("qtracker.test", 443)}

    async def call():
        await hosted.app(scope, receive, send)

    hosted.portal.call(call)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body), len(taken)


# ---------- the upload ----------

def test_upload_round_trip(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token, options=["Take #1", "Neither"])
    raw = png(64, 48)
    before = time.time()
    issued = request_upload(hosted, token, 1, mime="image/png", bytes=len(raw), caption="  The cat  ",
                            source_url=DRIVE_LINK)
    assert issued.status_code == 201, issued.text
    assert issued.headers["cache-control"] == "no-store"
    body = issued.json()
    secret = upload_token(body)
    assert re.fullmatch(r"qtu_[A-Za-z0-9_-]{43}", secret)
    assert body["upload"] == {"url": f"{PUBLIC}/api/uploads", "method": "PUT",
                              "headers": {"Authorization": f"Bearer {secret}", "Content-Type": "image/png"},
                              "expires_at": body["upload"]["expires_at"]}
    assert body["curl"] == (f"curl -sS --fail-with-body -X PUT -H 'Authorization: Bearer {secret}' "
                            f"-H 'Content-Type: image/png' --data-binary @FILE {PUBLIC}/api/uploads")
    expires = datetime.fromisoformat(body["upload"]["expires_at"].replace("Z", "+00:00"))
    assert expires.tzinfo == timezone.utc and before + 899 <= expires.timestamp() <= time.time() + 901

    # stored as a SHA-256 digest only, with everything the token is bound to
    stored = hosted.store.all("uploads")
    assert list(stored) == [hashlib.sha256(secret.encode()).hexdigest()]
    grant = stored[hashlib.sha256(secret.encode()).hexdigest()]
    assert set(grant) == GRANT_FIELDS and secret not in str(grant)
    agent_id = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["id"]
    assert {k: v for k, v in grant.items() if k != "expires_at"} == {
        "user": DENIS_SUB, "agent_id": agent_id, "agent": "allen", "ask_id": 1, "mime": "image/png",
        "bytes": len(raw), "caption": "The cat", "source_url": DRIVE_LINK, "poster_for": 0}
    assert abs(grant["expires_at"] - expires.timestamp()) < 1
    assert media_files(drive) == []  # nothing in Drive until the bytes come

    # the agent's shell sends the file, as the curl line says
    sent = put(hosted, body, raw)
    assert sent.status_code == 201, sent.text
    file = drive.file("media-1-1.png")
    assert file["content"] == raw and file["appProperties"] == MEDIA_PROPS
    attachment = sent.json()["attachment"]
    assert sent.json() == {"ask_id": 1, "attachment": {
        "id": 1, "kind": "image", "mime": "image/png", "width": 64, "height": 48, "bytes": len(raw),
        "caption": "The cat", "source_url": DRIVE_LINK, "file_id": file["id"], "added_at": attachment["added_at"]}}
    ask = hosted.get("/api/asks/1", headers=bearer(token)).json()
    assert ask["attachments"] == [attachment]
    doc = drive.doc(1)
    assert doc["events"][-1]["kind"] == "attached" and doc["events"][-1]["actor"] == "allen"
    assert doc["events"][-1]["payload"] == {"attachment": attachment}
    assert hosted.store.all("uploads") == {}  # used up

    # a clip, then a poster for it: the reply is the attachment the poster belongs to
    clip = mp4(320, 240, 3)
    sent = put(hosted, upload(hosted, token, 1, clip, "video/mp4", caption="The clip"), clip)
    assert sent.status_code == 201, sent.text
    video = sent.json()["attachment"]
    assert (video["id"], video["kind"], video["duration_ms"], video["poster"]) == (2, "video", 3000, None)
    assert drive.file("media-1-2.mp4")["content"] == clip
    still = jpeg(32, 18)
    sent = put(hosted, upload(hosted, token, 1, still, "image/jpeg", poster_for=2), still)
    assert sent.status_code == 201, sent.text
    poster_file = drive.file("media-1-2-poster.jpg")
    assert sent.json() == {"ask_id": 1, "attachment": {**video, "poster": {
        "file_id": poster_file["id"], "mime": "image/jpeg", "width": 32, "height": 18, "bytes": len(still)}}}
    assert [a["id"] for a in hosted.get("/api/asks/1", headers=bearer(token)).json()["attachments"]] == [1, 2]
    assert slots_in_use(hosted) == 0


def test_token_single_use(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    assert put(hosted, issued, raw).status_code == 201
    again = put(hosted, issued, raw)
    assert again.status_code == 401 and "upload token" in again.json()["detail"]
    assert again.headers["www-authenticate"].startswith("Bearer")
    # a refused PUT uses the token up too: one try per token, whatever the outcome
    issued = upload(hosted, token, 1, raw)
    assert put(hosted, issued, bytes(len(raw))).status_code == 422
    assert put(hosted, issued, raw).status_code == 401
    assert media_files(drive) == ["media-1-1.png"]
    assert [a["id"] for a in hosted.get("/api/asks/1", headers=bearer(token)).json()["attachments"]] == [1]


def test_token_bound_to_its_ask(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token, "First?")
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    new_ask(hosted, token, "Second?")  # newer: the token still names ask 1
    sent = put(hosted, issued, raw)
    assert sent.status_code == 201 and sent.json()["ask_id"] == 1
    assert media_files(drive) == ["media-1-1.png"]
    assert hosted.get("/api/asks/2", headers=bearer(token)).json()["attachments"] == []

    # another agent of the same user can't get a token for Allen's ask (nor attach or detach), and Allen not for theirs
    feynman = key_for(hosted, google, "Feynman")
    refused = request_upload(hosted, feynman, 1, mime="image/png", bytes=len(raw))
    assert refused.status_code == 403 and "only the agent who created ask #1 (allen)" in refused.json()["detail"]
    assert hosted.post("/api/asks/1/attachments", json={"data": data_url("image/png", raw)},
                       headers=bearer(feynman)).status_code == 403
    assert hosted.delete("/api/asks/1/attachments/1", headers=bearer(feynman)).status_code == 403
    new_ask(hosted, feynman, "Feynman's?")
    assert request_upload(hosted, token, 3, mime="image/png", bytes=len(raw)).status_code == 403
    assert request_upload(hosted, token, 99, mime="image/png", bytes=len(raw)).status_code == 404
    assert hosted.store.all("uploads") == {}  # nothing issued for the refused ones


def test_token_expiry(hosted, google):
    token = allen(hosted, google)
    new_ask(hosted, token)
    raw = png(8, 8)
    early, late = upload(hosted, token, 1, raw), upload(hosted, token, 1, raw)
    grants = hosted.app.state.uploads
    now = time.time()
    grants.clock = lambda: now + 899
    assert put(hosted, early, raw).status_code == 201
    grants.clock = lambda: now + 901
    expired = put(hosted, late, raw)
    assert expired.status_code == 401 and "expired" in expired.json()["detail"]
    assert hosted.store.all("uploads") == {}  # an expired token is gone once tried


def test_wrong_size(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    raw = png_of_size(1000)

    # more than declared: 413 from the Content-Length alone, before a byte of the body is read
    too_much = put(hosted, upload(hosted, token, 1, raw), raw + b"x")
    assert too_much.status_code == 413 and "1000" in too_much.json()["detail"]
    status, body, taken = raw_put(hosted, {**upload(hosted, token, 1, raw)["upload"]["headers"],
                                           "Content-Length": "1001"}, [raw[:600], raw[600:] + b"x"])
    assert (status, taken) == (413, 0)
    # a client that sends more than its own Content-Length: reading stops at the declared size
    headers = {**upload(hosted, token, 1, raw)["upload"]["headers"], "Content-Length": "1000"}
    status, body, taken = raw_put(hosted, headers, [raw[:600], raw[600:], b"x" * 500, b"x" * 500])
    assert status == 413 and taken == 3
    # less than declared: 422, from the Content-Length, or when the body ends early
    too_little = put(hosted, upload(hosted, token, 1, raw), raw[:999])
    assert too_little.status_code == 422 and "exactly 1000 bytes" in too_little.json()["detail"]
    headers = {**upload(hosted, token, 1, raw)["upload"]["headers"], "Content-Length": "1000"}
    status, body, _ = raw_put(hosted, headers, [raw[:999]])
    assert status == 422 and "exactly 1000 bytes" in body["detail"]
    # no Content-Length at all (chunked): refused before anything is buffered
    chunked = hosted.put("/api/uploads", content=iter([raw[:500], raw[500:]]),
                         headers=upload(hosted, token, 1, raw)["upload"]["headers"])
    assert chunked.status_code == 411 and "Content-Length" in chunked.json()["detail"]

    assert media_files(drive) == [] and hosted.get("/api/asks/1", headers=bearer(token)).json()["attachments"] == []
    assert put(hosted, upload(hosted, token, 1, raw), raw).status_code == 201  # the exact size works
    assert slots_in_use(hosted) == 0


def test_slow_upload_frees_its_slot(hosted, google, monkeypatch):
    """A PUT holds a media slot while it reads: a client that stops sending, or trickles, gets a 408 and the slot
    goes back (deadlines shortened here from 20 s per piece and 120 s for the whole body)."""
    assert (media_api.UPLOAD_IDLE, media_api.UPLOAD_DEADLINE) == (20.0, 120.0)
    monkeypatch.setattr(media_api, "UPLOAD_IDLE", 0.5)
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    raw = png_of_size(1000)

    def headers() -> dict:
        return {**upload(hosted, token, 1, raw)["upload"]["headers"], "Content-Length": "1000"}

    monkeypatch.setattr(media_api, "UPLOAD_DEADLINE", 5.0)
    stall_headers, started = headers(), time.monotonic()
    status, body, taken = raw_put(hosted, stall_headers, [raw[:100]], stall=True)  # sends a little, then nothing
    assert (status, body["detail"], taken) == (408, "upload too slow: request a new upload", 1)
    assert time.monotonic() - started < 3  # the idle deadline, well before the whole-body one
    assert slots_in_use(hosted) == 0
    monkeypatch.setattr(media_api, "UPLOAD_DEADLINE", 0.3)
    stalled = headers()
    status, body, taken = raw_put(hosted, stalled, [raw[i:i + 50] for i in range(0, 1000, 50)], pause=0.05)
    assert (status, body["detail"]) == (408, "upload too slow: request a new upload") and taken < 20  # each piece in
    assert slots_in_use(hosted) == 0                                                         # time, the whole not
    assert media_files(drive) == [] and hosted.store.all("uploads") == {}
    status, body, _ = raw_put(hosted, stalled, [raw])  # the token went with the slow PUT
    assert status == 401
    status, body, _ = raw_put(hosted, headers(), [raw[:500], raw[500:]])
    assert status == 201 and body["attachment"]["bytes"] == 1000
    assert slots_in_use(hosted) == 0


def test_wrong_type(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    picture = jpeg(20, 10)
    refused = put(hosted, upload(hosted, token, 1, picture, "image/png"), picture)  # declared PNG, sends a JPEG
    assert refused.status_code == 422
    assert refused.json()["detail"] == "the file was declared image/png but its bytes are image/jpeg"
    # a GIF declared as a PNG: refused by its bytes, with the hint
    refused = put(hosted, upload(hosted, token, 1, GIF, "image/png"), GIF)
    assert refused.status_code == 422 and "convert to PNG, JPEG or WebP; send motion as MP4" in refused.json()["detail"]
    # an MP4 declared as a picture, and the other way round
    clip = mp4(32, 32, 1)
    assert put(hosted, upload(hosted, token, 1, clip, "image/webp"), clip).status_code == 422
    raw = png(8, 8)
    assert put(hosted, upload(hosted, token, 1, raw, "video/mp4"), raw).status_code == 422
    # the Content-Type sent must be the declared one
    mismatch = put(hosted, upload(hosted, token, 1, raw), raw, **{"Content-Type": "image/jpeg"})
    assert mismatch.status_code == 422 and "Content-Type must be image/png" in mismatch.json()["detail"]
    assert put(hosted, upload(hosted, token, 1, raw), raw, **{"Content-Type": "Image/PNG; x=1"}).status_code == 201
    assert media_files(drive) == ["media-1-1.png"]


def test_closed_ask(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    assert hosted.post("/api/asks/1/close", json={"resolution": "Not needed"}, headers=bearer(token)).status_code == 200
    posts = len(drive.calls("POST", "/upload/"))
    refused = put(hosted, issued, raw)
    assert refused.status_code == 409 and refused.json()["detail"] == "ask #1 is closed; only open asks can be edited"
    assert media_files(drive) == [] and len(drive.calls("POST", "/upload/")) == posts  # nothing written to Drive
    # answered in the app after the token was issued: the same
    new_ask(hosted, token, "Second?")
    issued = upload(hosted, token, 2, raw)
    app_action(drive, 2, "answered", answer="Yes")
    assert put(hosted, issued, raw).status_code == 409
    assert media_files(drive) == [] and drive.doc(2)["answer"] == "Yes"
    # and a closed ask gets no token at all
    refused = request_upload(hosted, token, 1, mime="image/png", bytes=len(raw))
    assert refused.status_code == 409 and hosted.store.all("uploads") == {}


def test_token_only_on_upload_route(hosted, google):
    token = allen(hosted, google)
    new_ask(hosted, token)
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    secret = upload_token(issued)
    # an upload token opens nothing else, and showing it elsewhere doesn't use it up
    for method, path in (("GET", "/api/asks"), ("GET", "/api/me"), ("GET", "/api/asks/1"),
                         ("POST", "/api/asks/1/attachments"), ("POST", "/api/asks/1/uploads"),
                         ("GET", "/api/uploads"), ("POST", "/api/uploads"), ("GET", "/api/agents")):
        response = hosted.request(method, path, headers=bearer(secret), json={})
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["detail"] == "an upload token works only for its own PUT /api/uploads"
    assert mcp(hosted, secret, "asks_mine").status_code == 401
    assert len(hosted.store.all("uploads")) == 1
    # and PUT /api/uploads takes nothing but an upload token
    for other in (token, key_for(hosted, google, "Feynman"), "qtu_" + "A" * 43, "qtu_short", "nonsense"):
        refused = hosted.put("/api/uploads", content=raw, headers={**bearer(other), "Content-Type": "image/png"})
        assert refused.status_code == 401, (other, refused.text)
    assert hosted.put("/api/uploads", content=raw, headers={"Content-Type": "image/png"}).status_code == 401
    assert hosted.put("/api/uploads", content=raw,
                      headers={**id_token(google), "Content-Type": "image/png"}).status_code == 401
    assert put(hosted, issued, raw).status_code == 201


def test_other_routes_keep_256k_cap(hosted, google):
    token = allen(hosted, google)
    new_ask(hosted, token)
    big = png_of_size(1024 * 1024)  # 1 MB: four times the cap of every other route
    sent = put(hosted, upload(hosted, token, 1, big), big)
    assert sent.status_code == 201, sent.text
    assert google.drive(DENIS_SUB).file("media-1-1.png")["content"] == big
    over = b'{"title": "' + b"x" * MAX_BODY + b'"}'
    for method, path in (("POST", "/api/asks"), ("POST", "/api/asks/1/attachments"), ("POST", "/api/asks/1/uploads"),
                         ("PATCH", "/api/asks/1"), ("DELETE", "/api/asks/1/attachments/1")):
        response = hosted.request(method, path, content=over, headers={**bearer(token),
                                                                       "Content-Type": "application/json"})
        assert response.status_code == 413 and f"max {MAX_BODY} bytes" in response.json()["detail"], (method, path)
    response = hosted.post("/mcp", content=over, headers={**bearer(token), "Content-Type": "application/json"})
    assert response.status_code == 413


# ---------- the attachment REST ----------

def test_rest_attach_detach(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    raw = png(32, 24)
    attached = hosted.post("/api/asks/1/attachments", json={"data": data_url("image/png", raw), "caption": " One "},
                           headers=bearer(token))
    assert attached.status_code == 201, attached.text
    ask = attached.json()
    assert ask["id"] == 1 and "events" not in ask  # like PATCH: the ask without its history
    assert [(a["id"], a["kind"], a["caption"]) for a in ask["attachments"]] == [(1, "image", "One")]

    google.publish(PIC, png(10, 10), headers={"Content-Type": "image/png"})
    ask = hosted.post("/api/asks/1/attachments", json={"url": PIC}, headers=bearer(token)).json()
    assert [a["id"] for a in ask["attachments"]] == [1, 2] and ask["attachments"][1]["width"] == 10
    card = hosted.post("/api/asks/1/attachments", json={"url": DRIVE_LINK, "link": True, "title": "The deck"},
                       headers=bearer(token))
    assert card.status_code == 201
    assert card.json()["attachments"][2] == {**card.json()["attachments"][2], "kind": "link", "title": "The deck",
                                             "provider": "drive", "poster": None}
    poster = hosted.post("/api/asks/1/attachments", json={"data": data_url("image/png", png(16, 9)), "poster_for": 3},
                         headers=bearer(token))
    assert poster.status_code == 201 and poster.json()["attachments"][2]["poster"]["width"] == 16
    assert media_files(drive) == ["media-1-1.png", "media-1-2.png", "media-1-3-poster.png"]

    # refusals, before anything is written
    for body, status, words in (
        ({"url": PIC, "data": data_url("image/png", raw)}, 422, "exactly one of url and data"),
        ({}, 422, "exactly one of url and data"),
        ({"url": PIC, "mime": "image/png"}, 422, "mime"),
        ({"url": DRIVE_LINK}, 422, "Google Drive"),
        ({"data": data_url("image/png", raw), "poster_for": 1}, 422, "image"),
        ({"data": data_url("image/png", raw), "poster_for": 9}, 404, "#9"),
        ({"data": "data:image/gif;base64,R0lGOD"}, 422, "data must be"),
    ):
        response = hosted.post("/api/asks/1/attachments", json=body, headers=bearer(token))
        assert response.status_code == status and words in response.json()["detail"], (body, response.text)
    assert hosted.post("/api/asks/9/attachments", json={"data": data_url("image/png", raw)},
                       headers=bearer(token)).status_code == 404
    assert media_files(drive) == ["media-1-1.png", "media-1-2.png", "media-1-3-poster.png"]

    detached = hosted.delete("/api/asks/1/attachments/3", headers=bearer(token))
    assert detached.status_code == 200, detached.text
    assert [a["id"] for a in detached.json()["attachments"]] == [1, 2]
    assert media_files(drive) == ["media-1-1.png", "media-1-2.png"]  # the card's poster went with it
    assert hosted.delete("/api/asks/1/attachments/3", headers=bearer(token)).status_code == 404
    assert hosted.delete("/api/asks/1/attachments/x", headers=bearer(token)).status_code == 422
    assert [e["kind"] for e in drive.doc(1)["events"]][-1] == "detached"

    app_action(drive, 1, "answered", answer="#2")
    assert hosted.post("/api/asks/1/attachments", json={"data": data_url("image/png", raw)},
                       headers=bearer(token)).status_code == 409
    assert hosted.delete("/api/asks/1/attachments/1", headers=bearer(token)).status_code == 409
    assert slots_in_use(hosted) == 0


def test_rest_create_with_attachments(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    google.publish(PIC, png(12, 12), headers={"Content-Type": "image/png"})
    created = hosted.post("/api/asks", json={"title": "Which?", "options": ["Take #1", "Take #2"], "attachments": [
        {"url": PIC, "caption": "Cat"}, {"url": DRIVE_LINK, "link": True}]}, headers=bearer(token))
    assert created.status_code == 201, created.text
    ask = created.json()
    assert [(a["id"], a["kind"]) for a in ask["attachments"]] == [(1, "image"), (2, "link")]
    assert media_files(drive) == ["media-1-1.png"]
    assert [e["kind"] for e in drive.doc(1)["events"]] == ["created", "attached", "attached"]

    # [] and null are a plain ask
    for empty in ([], None):
        plain = hosted.post("/api/asks", json={"title": "Plain?", "attachments": empty}, headers=bearer(token))
        assert plain.status_code == 201 and plain.json()["attachments"] == [], plain.text
    # a bad item fails the create, named by its index, and nothing is written
    files = sorted(drive.files)
    for attachments, words in (
        ([{"url": PIC}, {"caption": "no url"}], "attachments[1]: exactly one of url and data"),
        ([{"url": PIC}, "https://x.example/a"], "attachments[1]: an attachment must be an object"),
        ("https://x.example/a", "attachments must be a list"),
        ([{"data": data_url("image/png", png(2, 2))}] * 7, "at most 6"),
    ):
        refused = hosted.post("/api/asks", json={"title": "Bad?", "attachments": attachments}, headers=bearer(token))
        assert refused.status_code == 422 and words in refused.json()["detail"], refused.text
    assert hosted.post("/api/asks", json={"title": "", "attachments": [{"url": PIC}]},
                       headers=bearer(token)).status_code == 422
    assert sorted(drive.files) == files
    assert hosted.get("/api/asks", headers=bearer(token)).json()["counts"]["open"] == 3
    assert slots_in_use(hosted) == 0


def test_user_token_forbidden(hosted, google):
    """The user's Google ID token is for the App API only: on the agents' attachment routes it is no agent (401),
    as everywhere on the agents' REST."""
    token = allen(hosted, google)
    new_ask(hosted, token)
    raw = png(8, 8)
    user = id_token(google)
    for method, path, body in (("POST", "/api/asks/1/attachments", {"data": data_url("image/png", raw)}),
                               ("POST", "/api/asks/1/uploads", {"mime": "image/png", "bytes": len(raw)}),
                               ("DELETE", "/api/asks/1/attachments/1", None),
                               ("POST", "/api/asks", {"title": "Mine?", "attachments": []})):
        response = hosted.request(method, path, json=body, headers=user)
        assert response.status_code == 401, (method, path, response.text)
    assert hosted.put("/api/uploads", content=raw, headers={**user, "Content-Type": "image/png"}).status_code == 401
    assert media_files(google.drive(DENIS_SUB)) == [] and hosted.store.all("uploads") == {}


def test_delete_account_drops_upload_tokens(hosted, google):
    token = allen(hosted, google)
    new_ask(hosted, token)
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    olga = allen(hosted, google, sub=OLGA_SUB, email=OLGA_EMAIL)
    new_ask(hosted, olga)
    hers = upload(hosted, olga, 1, raw)
    assert len(hosted.store.all("uploads")) == 2
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert [doc["user"] for doc in hosted.store.all("uploads").values()] == [OLGA_SUB]
    assert put(hosted, issued, raw).status_code == 401
    assert put(hosted, hers, raw).status_code == 201  # Olga's is untouched


def test_revoked_agent_upload_token(hosted, google):
    token = allen(hosted, google)
    new_ask(hosted, token)
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    agent_id = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["id"]
    assert hosted.delete(f"/api/agents/{agent_id}", headers=id_token(google)).status_code == 200
    refused = put(hosted, issued, raw)
    assert refused.status_code == 401 and "disconnected" in refused.json()["detail"]
    assert media_files(google.drive(DENIS_SUB)) == []


def test_upload_request_checks(hosted, google):
    token = allen(hosted, google)
    new_ask(hosted, token)
    google.publish(CLIP, mp4(320, 240, 3), headers={"Content-Type": "video/mp4"})
    assert hosted.post("/api/asks/1/attachments", json={"url": CLIP}, headers=bearer(token)).status_code == 201
    mb = 1024 * 1024
    for body, status, words in (
        ({"mime": "image/gif", "bytes": 10}, 422, "convert to PNG, JPEG or WebP; send motion as MP4"),
        ({"mime": "application/octet-stream", "bytes": 10}, 422, "mime"),
        ({"mime": 5, "bytes": 10}, 422, "mime"),
        ({"mime": "image/png", "bytes": 0}, 422, "bytes"),
        ({"mime": "image/png", "bytes": -1}, 422, "bytes"),
        ({"mime": "image/png", "bytes": True}, 422, "bytes"),
        ({"mime": "image/png", "bytes": "10"}, 422, "bytes"),
        ({"mime": "image/png", "bytes": 10.5}, 422, "bytes"),
        ({"mime": "image/png", "bytes": 5 * mb + 1}, 422, "5 MB"),
        ({"mime": "video/mp4", "bytes": 20 * mb + 1}, 422, "20 MB"),
        ({"mime": "image/png", "bytes": 10, "caption": "x" * 301}, 422, "caption is too long"),
        ({"mime": "image/png", "bytes": 10, "source_url": "http://example.com/x"}, 422, "https"),
        ({"mime": "image/png", "bytes": 10, "title": "x"}, 422, "title"),
        ({"mime": "image/png", "bytes": mb + 1, "poster_for": 1}, 422, "1 MB"),
        ({"mime": "video/mp4", "bytes": 10, "poster_for": 1}, 422, "still"),
        ({"mime": "image/png", "bytes": 10, "poster_for": 1, "caption": "x"}, 422, "caption"),
        ({"mime": "image/png", "bytes": 10, "poster_for": 9}, 404, "#9"),
        ({"mime": "image/png", "bytes": 10, "poster_for": "1"}, 422, "poster_for"),
        ({"bytes": 10}, 422, "mime"),
    ):
        response = request_upload(hosted, token, 1, **body)
        assert response.status_code == status and words in response.json()["detail"], (body, response.text)
    assert hosted.store.all("uploads") == {}
    assert request_upload(hosted, token, 1, mime=" Image/PNG ", bytes=5 * mb).status_code == 201
    assert request_upload(hosted, token, 1, mime="video/mp4", bytes=20 * mb).status_code == 201
    assert request_upload(hosted, token, 1, mime="image/webp", bytes=mb, poster_for=1).status_code == 201
    assert [g["mime"] for g in hosted.store.all("uploads").values()] == ["image/png", "video/mp4", "image/webp"]

    # the per-ask caps are checked before a token is issued: 40 MB stored in all, at most 6 attachments
    small = hosted.get("/api/asks/1", headers=bearer(token)).json()["attachments"][0]["bytes"]
    google.publish(BIG_CLIP, with_free(mp4(16, 16, 1), 20 * mb), headers={"Content-Type": "video/mp4"})
    assert hosted.post("/api/asks/1/attachments", json={"url": BIG_CLIP}, headers=bearer(token)).status_code == 201
    over = request_upload(hosted, token, 1, mime="video/mp4", bytes=20 * mb)
    assert over.status_code == 422 and "40 MB" in over.json()["detail"]
    assert request_upload(hosted, token, 1, mime="video/mp4", bytes=20 * mb - small).status_code == 201  # 40 MB
    for n in range(4):
        assert hosted.post("/api/asks/1/attachments", json={"data": data_url("image/png", png(2 + n, 2))},
                           headers=bearer(token)).status_code == 201
    full = request_upload(hosted, token, 1, mime="image/png", bytes=10)
    assert full.status_code == 422 and "at most 6" in full.json()["detail"]
    assert request_upload(hosted, token, 1, mime="image/png", bytes=10, poster_for=1).status_code == 201  # not a 7th


# ---------- the guard: 60 attachment calls an hour per agent, 4 slots per instance ----------

def test_busy_slots(hosted, google):
    token = allen(hosted, google)
    drive = google.drive(DENIS_SUB)
    new_ask(hosted, token)
    google.publish(PIC, png(10, 10), headers={"Content-Type": "image/png"})
    raw = png(8, 8)
    issued = upload(hosted, token, 1, raw)
    slots = hosted.app.state.media.guard.slots
    assert slots.take(4)
    for response in (hosted.post("/api/asks/1/attachments", json={"url": PIC}, headers=bearer(token)),
                     hosted.post("/api/asks", json={"title": "Pics?", "attachments": [{"url": PIC}]},
                                 headers=bearer(token))):
        assert response.status_code == 503, response.text
        assert response.headers["retry-after"] == "5" and response.json()["detail"] == \
            "busy, try again in a few seconds"
    # a busy PUT has used its token up (claimed first, whatever the outcome), and says so
    busy = put(hosted, issued, raw)
    assert busy.status_code == 503 and busy.headers["retry-after"] == "5"
    assert busy.json()["detail"] == "busy: request a new upload in a few seconds"
    assert hosted.store.all("uploads") == {}
    assert media_files(drive) == [] and hosted.get("/api/asks", headers=bearer(token)).json()["counts"]["open"] == 1
    # nothing to fetch, nothing held: data, link cards, detach and upload requests go through
    assert hosted.post("/api/asks/1/attachments", json={"data": data_url("image/png", raw)},
                       headers=bearer(token)).status_code == 201
    assert hosted.post("/api/asks", json={"title": "Card?", "attachments": [{"url": DRIVE_LINK, "link": True}]},
                       headers=bearer(token)).status_code == 201
    assert request_upload(hosted, token, 1, mime="image/png", bytes=10).status_code == 201
    assert hosted.delete("/api/asks/1/attachments/1", headers=bearer(token)).status_code == 200
    # a create that fetches needs two slots; an attach by url one
    slots.give(1)
    assert hosted.post("/api/asks", json={"title": "Pics?", "attachments": [{"url": PIC}]},
                       headers=bearer(token)).status_code == 503
    assert hosted.post("/api/asks/1/attachments", json={"url": PIC}, headers=bearer(token)).status_code == 201
    slots.give(3)
    assert slots.in_use == 0


def test_media_rate_limit(hosted, google):
    token = allen(hosted, google)
    limiter = hosted.app.state.limiter
    assert LIMITS["media"] == (60, 3600.0) and limiter.limits["media"] == (60, 3600.0)
    limiter.limits["media"] = (5, 3600.0)
    raw = png(8, 8)
    picture = {"data": data_url("image/png", raw)}
    # each item of a create counts, then each attach, upload request and detach
    assert hosted.post("/api/asks", json={"title": "Pics?", "attachments": [picture, picture]},
                       headers=bearer(token)).status_code == 201
    assert hosted.post("/api/asks/1/attachments", json=picture, headers=bearer(token)).status_code == 201
    assert request_upload(hosted, token, 1, mime="image/png", bytes=len(raw)).status_code == 201
    assert hosted.delete("/api/asks/1/attachments/1", headers=bearer(token)).status_code == 200
    for response in (hosted.post("/api/asks/1/attachments", json=picture, headers=bearer(token)),
                     request_upload(hosted, token, 1, mime="image/png", bytes=len(raw)),
                     hosted.delete("/api/asks/1/attachments/2", headers=bearer(token)),
                     hosted.post("/api/asks", json={"title": "More?", "attachments": [picture]},
                                 headers=bearer(token))):
        assert response.status_code == 429, response.text
        assert "at most 5" in response.json()["detail"] and response.headers["retry-after"] == "3600"
    # a plain create, reads and the PUT of a token already issued are not attachment calls
    plain = hosted.post("/api/asks", json={"title": "Plain?", "attachments": []}, headers=bearer(token))
    assert plain.status_code == 201
    assert len(hosted.store.all("uploads")) == 1  # the refused upload request issued nothing
    # another agent has its own budget
    feynman = key_for(hosted, google, "Feynman")
    new_ask(hosted, feynman, "Feynman's?")
    assert hosted.post("/api/asks/3/attachments", json=picture, headers=bearer(feynman)).status_code == 201
    # a create over what is left counts nothing
    limiter.limits["media"] = (7, 3600.0)
    assert hosted.post("/api/asks", json={"title": "Three?", "attachments": [picture] * 3},
                       headers=bearer(token)).status_code == 429
    assert hosted.post("/api/asks/1/attachments", json=picture, headers=bearer(token)).status_code == 201
    assert slots_in_use(hosted) == 0


def test_slots_and_counting_many():
    slots = Slots(4)
    assert slots.take(0) and slots.in_use == 0
    assert slots.take(2) and slots.take(1) and not slots.take(2) and slots.in_use == 3
    assert slots.take(1) and not slots.take(1)
    slots.give(2)
    assert slots.take(2) and slots.in_use == 4
    slots.give(4)
    assert slots.in_use == 0
    limiter = RateLimiter({"media": (5, 3600.0)}, clock=lambda: 100.0)
    assert limiter.allow("media", "ag", 3) and not limiter.allow("media", "ag", 3)  # refused: counts nothing
    assert limiter.allow("media", "ag", 2) and not limiter.allow("media", "ag")
    assert limiter.allow("media", "other", 5)


# ---------- the single-tenant bridge ----------

@pytest.mark.parametrize("value", [[{"url": PIC}], "x", {"url": PIC}, [1]])
def test_single_tenant_refuses_any_attachments(client, value):
    refused = client.post("/api/asks", json={"title": "Pic?", "attachments": value}, headers=auth(ALLEN_TOKEN))
    assert refused.status_code == 422 and refused.json()["detail"] == "attachments need the hosted AskHooman"
    for path in ("/api/asks/1/attachments", "/api/asks/1/uploads"):
        assert client.post(path, json={}, headers=auth(ALLEN_TOKEN)).status_code in (404, 405)
    assert client.put("/api/uploads", content=b"x", headers=auth(ALLEN_TOKEN)).status_code in (404, 405)
