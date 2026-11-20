"""Hosted bridge: GET /media/v1/<token> for a normal answer file (SPEC 7.4 «The route», «Budgets», «Drive refusals»,
«Normal file: streaming, not buffering»): the check order, the exact headers, Range, the slots and budgets, Drive's
refusals, the length check against `bytes`, and the idempotent `downloaded_at`."""

import base64
import json
import logging

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from server.hosted import files_api
from server.hosted.crypto import Keyring
from server.hosted.files import FileLinks
from server.hosted.files_api import mark_blocked, mark_downloaded
from server.hosted.ratelimit import ByteBudget

from .fake_drive import FakeDrive
from .hosted_support import DENIS_SUB, KEY, PUBLIC, build, id_token, mcp, tool_error, tool_result
from .test_hosted_files import answered_by_files, item
from .test_hosted_voice import agent, agent_id, answered_by_voice, bearer, rewrite

BODY = bytes(range(256)) * 20  # 5120 bytes
NO_SUCH = {"detail": "no such file, or the link has expired: read the ask again for a new link"}
DELETED = {"detail": "the file was deleted"}
BLOCKED = {"detail": "Google blocked this file"}
MIB = 1024 * 1024


def put_file(drive, ask_id=1, n=1, content=BODY, mime="application/octet-stream", **props) -> str:
    """A file in the user's appDataFolder as the apps store an answer's file (SPEC 7.2)."""
    app_properties = {"ask_id": str(ask_id), "kind": "file", "file": str(n), "secret": "0", **props}
    return drive.add(f"afile-{ask_id}-{n}-{1700000000000 + len(drive.files)}", content, app_properties, mime=mime)


class World:
    """An app with one agent (Allen), its ask #1 answered with one file (BODY) and the link to it."""

    def __init__(self, hosted, google, content=BODY, name="Q3 report.PDF", **over):
        self.hosted, self.google = hosted, google
        self.token = agent(hosted, google)
        self.drive = google.drive(DENIS_SUB)
        tool_result(mcp(hosted, self.token, "ask_create", {"title": "Ship?"}))
        self.file_id = put_file(self.drive, content=content)
        self.items = [item(1, name=name, mime="application/pdf", bytes=len(content), file_id=self.file_id, **over)]
        answered_by_files(self.drive, 1, self.items)
        self.path = self.link()

    def link(self, ask_id=1, n=0) -> str:
        files = tool_result(mcp(self.hosted, self.token, "ask_get", {"id": ask_id}))["answer_files"]
        if "url" not in files[n]:  # a secret file has no link: one made as the bridge makes them, to try it here
            ask, owner = self.drive.doc(ask_id), self.hosted.app.state
            url, _ = owner.file_links.url_for(DENIS_SUB, agent_id(self.hosted, self.google), ask_id,
                                              ask["answered_at"], ask["answer_files"][n]["file_id"],
                                              ask["answer_files"][n]["id"])
            return url.removeprefix(PUBLIC)
        return files[n]["url"].removeprefix(PUBLIC)

    def claims(self, path=None) -> dict:
        return self.hosted.app.state.file_links.open((path or self.path).rsplit("/", 1)[1])

    def sealed(self, **changes) -> str:
        found = {**self.claims(), **changes}
        return f"/media/v1/{Keyring(KEY).encrypt(json.dumps(found), 'file')}"

    def get(self, path=None, **kwargs):
        return self.hosted.get(path or self.path, **kwargs)

    def ask(self) -> dict:
        return self.drive.doc(1)

    def stored(self) -> dict:
        return self.ask()["answer_files"][0]

    def content_reads(self) -> list:
        """The reads of the answer file's bytes from Drive so far."""
        return [c for c in self.drive.calls("GET", f"/drive/v3/files/{self.file_id}") if c[2].get("alt") == "media"]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.ask()["events"]]


@pytest.fixture
def world(hosted, google):
    return World(hosted, google)


# ---------- headers, bodies ----------

def test_download_headers_exactly(world):
    response = world.get()
    assert response.status_code == 200 and response.content == BODY
    assert dict(response.headers) == {
        "content-type": "application/octet-stream",
        "content-length": str(len(BODY)),
        "content-disposition": "attachment; filename=\"ask-1-file-1.pdf\"; filename*=UTF-8''Q3%20report.PDF",
        "cache-control": "private, no-store",
        "x-content-type-options": "nosniff",
        "content-security-policy": "sandbox",
    }
    assert not any(h.startswith("access-control-") for h in response.headers)


def test_octet_stream_even_when_drive_and_the_item_say_html(hosted, google):
    world = World(hosted, google)
    world.drive.files[world.file_id]["mimeType"] = "text/html"
    rewrite(world.drive, 1, answer_files=[{**world.stored(), "mime": "text/html"}])
    response = world.get()
    assert response.headers["content-type"] == "application/octet-stream" and response.content == BODY


def test_unicode_name_goes_into_filename_star(hosted, google):
    world = World(hosted, google, name="Отчёт 2026.xlsx")
    disposition = world.get().headers["content-disposition"]
    assert disposition == ("attachment; filename=\"ask-1-file-1.xlsx\"; "
                           "filename*=UTF-8''%D0%9E%D1%82%D1%87%D1%91%D1%82%202026.xlsx")


def test_head_is_405(world):
    assert world.hosted.head(world.path).status_code == 405


def test_range_resumes_with_206(world):
    response = world.get(headers={"Range": "bytes=1000-"})
    assert response.status_code == 206 and response.content == BODY[1000:]
    assert response.headers["content-range"] == f"bytes 1000-{len(BODY) - 1}/{len(BODY)}"
    assert response.headers["content-length"] == str(len(BODY) - 1000)
    assert response.headers["content-type"] == "application/octet-stream"
    assert [h["range"] for h in world.drive.headers if "range" in h] == ["bytes=1000-"]
    assert world.get(headers={"Range": "bytes=0-"}).status_code == 200  # from the start: the whole file
    past = world.get(headers={"Range": f"bytes={len(BODY)}-"})
    assert past.status_code == 416 and past.headers["content-range"] == f"bytes */{len(BODY)}"
    assert world.get(headers={"Range": "bytes=5-9"}).content == BODY[5:10]  # a closed range: that part (Task 11)


# ---------- the check order ----------

def test_tokens_that_do_not_open(world):
    token = world.path.rsplit("/", 1)[1]
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    for path in (f"/media/v1/{flipped}", "/media/v1/v1.k1.garbage", "/media/v1/"):
        assert world.get(path).status_code == 404, path
    assert world.get(world.sealed(exp=1)).status_code == 404  # expired: it doesn't open
    assert world.get(world.sealed(user="9999")).json() == NO_SUCH  # another user's Drive holds no such ask


def test_wrong_agent_and_revoked_agent_404(hosted, google):
    world = World(hosted, google)
    other = agent(hosted, google, name="Feynman")
    other_id = agent_id(hosted, google, "Feynman")
    assert other
    assert world.get(world.sealed(agent=other_id)).json() == NO_SUCH  # ask #1 is Allen's
    allen_id = agent_id(hosted, google)
    hosted.delete(f"/api/agents/{allen_id}", headers=id_token(google)).raise_for_status()
    assert world.get().json() == NO_SUCH


def test_stale_reopened_and_unknown_items_404(world):
    assert world.get().status_code == 200
    assert world.get(world.sealed(answered_at="2026-01-01T00:00:00.000Z")).json() == NO_SUCH
    assert world.get(world.sealed(n=2)).json() == NO_SUCH
    assert world.get(world.sealed(file_id="other")).json() == NO_SUCH
    assert world.get(world.sealed(ask_id=7)).json() == NO_SUCH
    # the user answered again: the old link is stale, whatever the new list holds
    answered_by_files(world.drive, 1, world.items, text="Again")
    assert world.get().json() == NO_SUCH
    # reopened by the agent: no answer, no files
    old = world.link()
    assert world.hosted.post("/api/asks/1/reopen", headers=bearer(world.token)).status_code == 200
    assert world.get(old).json() == NO_SUCH


def test_closed_ask_and_deleted_item_410(hosted, google):
    world = World(hosted, google)
    gone = {**world.stored(), "file_id": None, "deleted_at": "2026-11-19T13:00:00.000Z", "deleted_reason": "sent"}
    rewrite(world.drive, 1, answer_files=[gone])
    response = world.get()
    assert (response.status_code, response.json()) == (410, DELETED)
    assert response.headers["cache-control"] == "no-store"


def test_closed_ask_is_410(world):
    assert world.hosted.post("/api/asks/1/close", headers=bearer(world.token), json={}).status_code == 200
    assert (world.get().status_code, world.get().json()) == (410, DELETED)


def test_secret_item_is_not_served_here(hosted, google):
    world = World(hosted, google, secret=True)
    assert world.items[0]["secret"] is True
    assert world.get().json() == NO_SUCH
    assert world.content_reads() == []


@pytest.mark.parametrize("props", [{"kind": "voice"}, {"ask_id": "9"}, {"file": "2"}, {"kind": None}])
def test_drive_says_it_is_not_this_files_item(hosted, google, props):
    world = World(hosted, google)
    world.drive.files[world.file_id]["appProperties"].update(props)
    assert world.get().json() == NO_SUCH
    assert world.content_reads() == []


def test_drive_size_not_bytes_404(hosted, google):
    world = World(hosted, google)
    world.drive.files[world.file_id]["content"] = BODY + b"x"
    assert world.get().json() == NO_SUCH
    assert world.content_reads() == []


def test_size_over_the_cap_404(world, monkeypatch):
    monkeypatch.setattr(files_api, "MAX_FILE_BYTES", len(BODY) - 1)
    assert world.get().json() == NO_SUCH


def test_file_gone_from_drive_410(hosted, google):
    world = World(hosted, google)
    del world.drive.files[world.file_id]
    assert (world.get().status_code, world.get().json()) == (410, DELETED)


def test_voice_links_still_work_on_the_same_path(hosted, google):
    token = agent(hosted, google)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    answered_by_voice(google.drive(DENIS_SUB), 1)
    url = tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_voice"]["url"]
    response = hosted.get(url.removeprefix(PUBLIC))
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mp4"
    assert hosted.get("/media/v1/v1.k1.garbage").json() == {
        "detail": "no such recording, or the link has expired: read the ask again for a new link"}


# ---------- Drive, slots, budgets ----------

def test_drive_unreachable_503(hosted, google):
    world = World(hosted, google)
    world.drive.down = True
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    world.drive.down = False
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 503, after=1)  # the content read, not the check
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert world.stored()["downloaded_at"] is None
    assert world.get().status_code == 200


def test_abusive_403_is_410_and_blocks_the_item(world):
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason="cannotDownloadAbusiveFile")
    response = world.get()
    assert (response.status_code, response.json()) == (410, BLOCKED)
    stored = world.stored()
    assert stored["blocked_at"] is not None and stored["downloaded_at"] is None
    assert stored["file_id"] == world.file_id  # blocked, not deleted
    assert world.kinds()[-1] == "file_blocked"
    # written once: a second refusal changes nothing
    rev = world.ask()["rev"]
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason="cannotDownloadAbusiveFile")
    assert world.get().status_code == 410 and world.ask()["rev"] == rev


def test_rate_limit_403_is_503_not_a_block(world):
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason="userRateLimitExceeded")
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert world.stored()["blocked_at"] is None


def test_fifth_concurrent_stream_503(world):
    slots = world.hosted.app.state.stream_slots
    assert slots.size == 4 and files_api.STREAM_SLOTS == 4
    assert world.hosted.app.state.media.guard.slots is not slots  # separate from the media slots
    assert slots.take(4)
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert response.json() == {"detail": "busy, try again in a few seconds"}
    slots.give(4)
    assert world.get().status_code == 200 and slots.in_use == 0


def test_files_bucket_429(world):
    for _ in range(20):
        assert world.get().status_code == 200
    response = world.get()
    assert response.status_code == 429 and int(response.headers["retry-after"]) >= 1
    assert world.hosted.get("/media/v1/v1.k1.garbage").status_code == 404  # a token that doesn't open counts for nobody


def test_byte_budget_429_charges_nothing_when_refused(world):
    budget = ByteBudget(limit=2 * len(BODY))
    world.hosted.app.state.file_bytes = budget
    assert world.get().status_code == 200 and world.get().status_code == 200
    response = world.get()
    assert response.status_code == 429 and response.headers["retry-after"] == "3600"
    assert budget.charge("probe", 2 * len(BODY)) is True  # (the refused request recorded nothing for its own key)
    assert len(world.content_reads()) == 3  # the refused one opened Drive, charged nothing, and closed the stream
    assert world.hosted.app.state.stream_slots.in_use == 0


# ---------- downloaded_at ----------

def test_downloaded_at_written_once_after_the_response(world):
    assert world.stored()["downloaded_at"] is None
    assert world.get().status_code == 200
    stored = world.stored()
    assert stored["downloaded_at"] is not None
    events = world.ask()["events"]
    assert events[-1]["kind"] == "file_downloaded" and events[-1]["payload"] == {"id": 1}
    assert events[-1]["actor"] == "allen" and events[-1]["at"] == stored["downloaded_at"]
    rev, count = world.ask()["rev"], len(events)
    assert world.get().status_code == 200 and world.get(headers={"Range": "bytes=10-"}).status_code == 206
    assert world.ask()["rev"] == rev and len(world.ask()["events"]) == count  # later downloads write nothing
    assert stored == world.stored()


def test_a_failed_download_does_not_mark_it(world):
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 503, after=1)
    assert world.get().status_code == 503 and world.stored()["downloaded_at"] is None


@pytest.fixture
def pool_of(world):
    return world.hosted.app.state.pool


def test_mark_downloaded_guards(world, pool_of):
    claims = world.claims()
    args = (pool_of, DENIS_SUB, 1, 1, world.file_id, claims["answered_at"])
    stamp = "2026-11-19T14:00:00.000Z"
    assert mark_downloaded(*args[:4], "other", args[5], stamp) is False  # another file_id
    assert mark_downloaded(*args[:3], 2, args[4], args[5], stamp) is False  # another item
    assert mark_downloaded(*args[:5], "2026-01-01T00:00:00.000Z", stamp) is False  # a stale answer
    assert mark_downloaded(pool_of, DENIS_SUB, 9, 1, world.file_id, args[5], stamp) is False  # no such ask
    assert world.stored()["downloaded_at"] is None and "file_downloaded" not in world.kinds()
    assert mark_downloaded(*args, stamp) is True
    assert world.stored()["downloaded_at"] == stamp
    assert mark_downloaded(*args, "2026-11-19T15:00:00.000Z") is False  # already set
    assert world.stored()["downloaded_at"] == stamp and world.kinds().count("file_downloaded") == 1


def test_mark_downloaded_skips_closed_and_reopened(world, pool_of):
    args = (pool_of, DENIS_SUB, 1, 1, world.file_id, world.claims()["answered_at"])
    assert world.hosted.post("/api/asks/1/close", headers=bearer(world.token), json={}).status_code == 200
    assert mark_downloaded(*args, "2026-11-19T14:00:00.000Z") is False
    assert world.hosted.post("/api/asks/1/reopen", headers=bearer(world.token)).status_code == 200
    assert mark_downloaded(*args, "2026-11-19T14:00:00.000Z") is False
    assert "file_downloaded" not in world.kinds()


def test_mark_blocked_has_the_same_guards(world, pool_of):
    args = (pool_of, DENIS_SUB, 1, 1, world.file_id, world.claims()["answered_at"])
    assert mark_blocked(*args[:5], "2026-01-01T00:00:00.000Z", "2026-11-19T14:00:00.000Z") is False
    assert mark_blocked(*args, "2026-11-19T14:00:00.000Z") is True
    assert mark_blocked(*args, "2026-11-19T15:00:00.000Z") is False
    assert world.stored()["blocked_at"] == "2026-11-19T14:00:00.000Z"


# ---------- the length against `bytes`, streams that break ----------

def patch_media(monkeypatch, body_of):
    """FakeDrive's content read answers `body_of(file, drive)` -> an iterator of bytes (Content-Length: the real size)."""
    class Stream(httpx.SyncByteStream):
        def __init__(self, pieces):
            self.pieces = pieces

        def __iter__(self):
            yield from self.pieces

    original = FakeDrive._media

    def media(self, file, request_range):
        if file["appProperties"].get("kind") != "file":  # the ask files are read as they are
            return original(file, request_range)
        return httpx.Response(200, headers={"Content-Type": file["mimeType"],
                                            "Content-Length": str(len(file["content"]))},
                              stream=Stream(body_of(self, file)))

    monkeypatch.setattr(FakeDrive, "_media", media)


def failing_download(world, **kwargs):
    """A GET whose response breaks off: the app raises into the test, or the client sees a short or failed body."""
    with pytest.raises(BaseException) as raised:
        world.get(**kwargs)
    return raised.value


def test_stream_longer_than_bytes_aborts_and_writes_nothing(hosted, google, monkeypatch, caplog):
    """Review Focus 3: the file was replaced under the same id; Drive sends more than `bytes`."""
    world = World(hosted, google)
    patch_media(monkeypatch, lambda drive, file: iter([file["content"], b"EXTRA-BYTES"]))
    with caplog.at_level(logging.WARNING):
        failing_download(world)
    assert world.stored()["downloaded_at"] is None and "file_downloaded" not in world.kinds()
    assert hosted.app.state.stream_slots.in_use == 0
    # Markers no random OAuth token in the setup logs can contain (a space, a long run): "Q3" alone matched a
    # random code_challenge about once in 40 runs.
    assert "EXTRA-BYTES" not in caplog.text and "Q3 report" not in caplog.text
    assert world.path.rsplit("/", 1)[1] not in caplog.text


def test_the_extra_bytes_never_reach_the_client(hosted, google, monkeypatch):
    world = World(hosted, google)
    patch_media(monkeypatch, lambda drive, file: iter([file["content"], b"EXTRA-BYTES"]))
    sent = asgi_get(hosted.app, world.path)
    body = b"".join(m.get("body", b"") for m in sent["messages"])
    assert body == BODY and sent["error"] is not None


def test_a_short_stream_aborts_and_writes_nothing(hosted, google, monkeypatch):
    world = World(hosted, google)
    patch_media(monkeypatch, lambda drive, file: iter([file["content"][:-100]]))
    sent = asgi_get(hosted.app, world.path)
    assert sent["error"] is not None
    assert not (sent["messages"][-1].get("more_body") is False)  # the response never completed
    assert world.stored()["downloaded_at"] is None and hosted.app.state.stream_slots.in_use == 0


def test_an_empty_drive_body_aborts(hosted, google, monkeypatch):
    world = World(hosted, google)
    patch_media(monkeypatch, lambda drive, file: iter([]))
    assert asgi_get(hosted.app, world.path)["error"] is not None
    assert world.stored()["downloaded_at"] is None


def test_dropped_file_mid_stream_ends_with_error_then_410(hosted, google, monkeypatch):
    """Review Focus 1: the user answers again while the agent downloads; Drive loses the dropped file mid-body. The
    stream ends with an error (never a 500 page, never the new answer's file); the user then removes the file."""
    big = bytes(range(256)) * (3 * MIB // 256)
    world = World(hosted, google, content=big)

    def body_of(drive, file):
        yield file["content"][:MIB + 5]
        answered_by_files(drive, 1, [], text="Never mind")  # the re-answer drops the item
        drive.files.pop(file["id"], None)  # and the app deletes its file
        raise httpx.ReadError("the file is gone")

    patch_media(monkeypatch, body_of)
    sent = asgi_get(hosted.app, world.path)
    assert sent["messages"][0]["status"] == 200 and sent["error"] is not None
    assert not (sent["messages"][-1].get("more_body") is False)
    assert hosted.app.state.stream_slots.in_use == 0
    assert world.ask()["answer_files"] == [] and "file_downloaded" not in world.kinds()
    # the old link is for an answer that is gone: refused, with no body of any file
    again = world.get()
    assert again.status_code == 404 and again.json() == NO_SUCH


def test_deleted_item_after_a_stream_error_is_410(hosted, google, monkeypatch):
    """The same break, when the file is removed from the same answer (deleted_at set): the next request is 410."""
    world = World(hosted, google, content=bytes(range(256)) * (2 * MIB // 256))

    def body_of(drive, file):
        yield file["content"][:MIB]
        rewrite(drive, 1, answer_files=[{**world.stored(), "file_id": None, "deleted_at": "2026-11-19T13:00:00.000Z",
                                         "deleted_reason": "sent"}])
        raise httpx.ReadError("the file is gone")

    patch_media(monkeypatch, body_of)
    assert asgi_get(hosted.app, world.path)["error"] is not None
    assert (world.get().status_code, world.get().json()) == (410, DELETED)


def test_client_disconnect_closes_drive_and_gives_the_slot_back(hosted, google):
    world = World(hosted, google, content=bytes(range(256)) * (3 * MIB // 256))
    sent = asgi_get(hosted.app, world.path, fail_send_after=2)
    assert isinstance(sent["error"], OSError)
    assert hosted.app.state.stream_slots.in_use == 0
    assert world.stored()["downloaded_at"] is None


def asgi_get(app, path: str, fail_send_after: int | None = None) -> dict:
    """GET `path` straight through the app: every message sent, and the exception the response ended with (the
    server, not a client, is what drops a connection whose body breaks)."""
    host = PUBLIC.split("://", 1)[1]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host.encode())], "client": ("127.0.0.1", 5000), "server": (host, 443)}
    messages, result = [], {"messages": [], "error": None}

    async def receive():
        await anyio.sleep_forever()

    async def send(message):
        messages.append(message)
        if fail_send_after is not None and len(messages) > fail_send_after:
            raise OSError("the client went away")

    async def run():
        try:
            await app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001 - the point is to see how it ended
            result["error"] = exc.exceptions[0] if isinstance(exc, BaseExceptionGroup) else exc

    anyio.run(run)
    result["messages"] = messages
    return result


# ---------- fix round 1: when the byte budget is charged, a blocked item, the slot during the stamp ----------

def charged(world, budget) -> int:
    """What the budget holds for the agent: the largest download it still takes, subtracted from its limit."""
    key = f"{DENIS_SUB}:{agent_id(world.hosted, world.google)}"
    return sum(n for _, n in budget._charges.get(key, ()))


@pytest.mark.parametrize("failure", [(503, ""), (404, ""), (403, "cannotDownloadAbusiveFile")])
def test_a_failed_open_charges_nothing_a_good_one_charges_bytes(world, failure):
    budget = ByteBudget(limit=10 * len(BODY))
    world.hosted.app.state.file_bytes = budget
    status, reason = failure
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", status, after=1, reason=reason)
    assert world.get().status_code in (503, 410)
    assert charged(world, budget) == 0
    rewrite(world.drive, 1, answer_files=[{**world.stored(), "blocked_at": None}])
    assert world.get().status_code == 200
    assert charged(world, budget) == len(BODY)


def test_refused_charge_closes_the_opened_stream(world):
    world.hosted.app.state.file_bytes = ByteBudget(limit=len(BODY) - 1)
    assert world.get().status_code == 429
    assert world.hosted.app.state.stream_slots.in_use == 0


def test_blocked_item_is_410_without_drive_slot_or_charge(world):
    rewrite(world.drive, 1, answer_files=[{**world.stored(), "blocked_at": "2026-11-19T13:00:00.000Z"}])
    budget = ByteBudget()
    world.hosted.app.state.file_bytes = budget
    slots = world.hosted.app.state.stream_slots
    assert slots.take(4)  # no slot is needed
    response = world.get()
    assert (response.status_code, response.json()) == (410, BLOCKED)
    slots.give(4)
    assert world.drive.calls("GET", f"/drive/v3/files/{world.file_id}") == []
    assert budget._charges == {}


def test_the_slot_is_free_while_downloaded_at_is_written(world, monkeypatch):
    seen = []
    real = files_api.mark_downloaded

    def watching(*args):
        seen.append(world.hosted.app.state.stream_slots.in_use)
        return real(*args)

    monkeypatch.setattr(files_api, "mark_downloaded", watching)
    assert world.get().status_code == 200
    assert seen == [0] and world.stored()["downloaded_at"] is not None


# ---------- Task 6: GET /api/asks/{id}/files/{n}/content, the secret file and its claim (SPEC 7.4) ----------

import threading  # noqa: E402
import time  # noqa: E402

from server.hosted.files import FileClaims, claim_key  # noqa: E402

SENT = {"detail": "the file was deleted after it was sent"}
SECRET_BYTES = b"SECRET-BODY-BYTES " * 100


def secret_world(hosted, google, **over):
    return World(hosted, google, content=SECRET_BYTES, secret=True, **over)


def content_path(ask_id=1, n=1) -> str:
    return f"/api/asks/{ask_id}/files/{n}/content"


def fetch(world, **kwargs):
    return world.hosted.get(content_path(), headers={**bearer(world.token), **kwargs.pop("headers", {})}, **kwargs)


def claim_docs(world) -> dict:
    return world.hosted.store.all("fileclaims")


def asgi_bearer(world, receive_script, fail_send=False) -> dict:
    """GET the content route straight through the app with `receive_script(sent_final)` as the client side."""
    host = PUBLIC.split("://", 1)[1]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
             "path": content_path(), "raw_path": content_path().encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host.encode()), (b"authorization", f"Bearer {world.token}".encode())],
             "client": ("127.0.0.1", 5000), "server": (host, 443)}
    out = {"messages": [], "error": None}
    final_sent = anyio.Event()
    script = receive_script(final_sent)

    async def receive():
        return await script.__anext__()

    async def send(message):
        if fail_send:
            raise OSError("the client went away")
        out["messages"].append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            final_sent.set()

    async def run():
        try:
            await world.hosted.app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc.exceptions[0] if isinstance(exc, BaseExceptionGroup) else exc

    anyio.run(run)
    return out


async def _disconnect_at_once(final_sent):
    yield {"type": "http.request", "body": b"", "more_body": False}
    yield {"type": "http.disconnect"}
    await anyio.sleep_forever()


async def _disconnect_after_final(final_sent):
    yield {"type": "http.request", "body": b"", "more_body": False}
    await final_sent.wait()
    yield {"type": "http.disconnect"}
    await anyio.sleep_forever()


def test_secret_sent_once_then_410(hosted, google):
    world = secret_world(hosted, google)
    response = fetch(world)
    assert response.status_code == 200 and response.content == SECRET_BYTES
    assert response.headers["content-length"] == str(len(SECRET_BYTES))
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith('attachment; filename="ask-1-file-1.pdf"; filename*=')
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    # after the response: claim done, file deleted from Drive, the ask written
    assert world.file_id not in world.drive.files
    stored = world.stored()
    assert stored["file_id"] is None and stored["deleted_reason"] == "sent"
    assert stored["downloaded_at"] is not None and stored["deleted_at"] is not None
    assert world.kinds()[-1] == "file_downloaded"
    assert [d["state"] for d in claim_docs(world).values()] == ["done"]
    again = fetch(world)
    assert (again.status_code, again.json()) == (410, DELETED)  # the item is gone from the ask: the check chain
    assert len(world.content_reads()) == 1


def test_claim_key_is_a_digest():
    key = claim_key("sub-123", 7, "fileid-xyz")
    assert len(key) >= 32 and all(c in "0123456789abcdef" for c in key)
    assert not any(part in key for part in ("sub-123", "fileid-xyz", "2026"))


def test_held_claim_409(hosted, google):
    world = secret_world(hosted, google)
    ask = world.ask()
    key = claim_key(DENIS_SUB, 1, world.file_id)
    hosted.store.put("fileclaims", key, {"user": DENIS_SUB, "ask_id": 1, "state": "held", "until": time.time() + 500,
                                         "owner": "someone"})
    response = fetch(world)
    assert response.status_code == 409 and response.json() == {"detail": "being sent"}
    assert world.content_reads() == [] and world.file_id in world.drive.files


def test_expired_held_claim_taken_over(hosted, google):
    world = secret_world(hosted, google)
    key = claim_key(DENIS_SUB, 1, world.file_id)
    hosted.store.put("fileclaims", key, {"user": DENIS_SUB, "ask_id": 1, "state": "held", "until": time.time() - 1,
                                         "owner": "dead"})
    assert fetch(world).content == SECRET_BYTES
    assert hosted.store.get("fileclaims", key)["state"] == "done"


def test_lease_is_960_seconds(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    monkeypatch.setattr(files_api, "_now", lambda: 1_000_000.0)
    seen = []
    real = FileClaims.take

    def spy(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        seen.append(self.store.all("fileclaims"))
        return result
    monkeypatch.setattr(FileClaims, "take", spy)
    # hold the claim at the moment it is taken: the document says until = now + 900 + 60
    hosted.app.state.stream_slots.take(4)  # all slots busy: 503 after the claim is taken, then released
    fetch(world)
    doc = next(iter(seen[0].values()))
    assert doc["state"] == "held" and doc["until"] == 1_000_000.0 + 960


def test_disconnect_mid_body_releases_claim(hosted, google):
    world = secret_world(hosted, google)
    out = asgi_bearer(world, _disconnect_at_once)
    assert out["error"] is None
    assert claim_docs(world) == {}  # released: the agent can try again
    assert world.file_id in world.drive.files and world.stored()["file_id"] == world.file_id
    assert world.stored()["downloaded_at"] is None
    assert fetch(world).content == SECRET_BYTES


def test_failed_send_releases_claim(hosted, google):
    world = secret_world(hosted, google)
    out = asgi_bearer(world, _disconnect_after_final, fail_send=True)
    assert isinstance(out["error"], OSError)
    assert claim_docs(world) == {} and world.file_id in world.drive.files


def test_done_only_after_final_send_without_disconnect(hosted, google):
    world = secret_world(hosted, google)
    out = asgi_bearer(world, _disconnect_after_final)  # the agent hangs up only once everything was sent
    assert out["error"] is None
    assert out["messages"][0]["status"] == 200 and out["messages"][-1]["body"] == SECRET_BYTES
    assert [d["state"] for d in claim_docs(world).values()] == ["done"]
    assert world.file_id not in world.drive.files


def test_delete_failure_leaves_done_and_route_refuses(hosted, google, caplog):
    world = secret_world(hosted, google)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{world.file_id}", 500)
    with caplog.at_level(logging.DEBUG):
        assert fetch(world).content == SECRET_BYTES
    assert world.file_id in world.drive.files  # the sweep retries
    assert [d["state"] for d in claim_docs(world).values()] == ["done"]
    assert world.stored()["file_id"] == world.file_id and world.stored()["deleted_at"] is None
    again = fetch(world)  # the item still names the file, the claim says it was sent
    assert (again.status_code, again.json()) == (410, SENT)
    assert len(world.content_reads()) == 1
    assert "SECRET-BODY-BYTES" not in caplog.text and "Q3 report" not in caplog.text


def test_secret_past_24h_is_410_expired(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    start = time.time()
    monkeypatch.setattr(files_api, "_now", lambda: start + 24 * 3600 + 120)
    response = fetch(world)
    assert (response.status_code, response.json()) == (410, {"detail": "expired"})
    assert world.content_reads() == [] and claim_docs(world) == {}
    assert world.file_id in world.drive.files  # deletion follows from the sweep


def test_secret_no_range(hosted, google):
    world = secret_world(hosted, google)
    response = fetch(world, headers={"Range": "bytes=10-"})
    assert response.status_code == 200 and response.content == SECRET_BYTES and "content-range" not in response.headers


def test_late_writes_with_other_answered_at_dropped(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    real_delete = files_api.HostedAsks._delete_answer_file

    def reanswered_first(self, *args, **kwargs):  # a re-answer lands while the file is being deleted
        answered_by_files(world.drive, 1, [item(1, file_id="fid-new", bytes=5)], text="Again")
        return real_delete(self, *args, **kwargs)
    monkeypatch.setattr(files_api.HostedAsks, "_delete_answer_file", reanswered_first)
    assert fetch(world).status_code == 200
    stored = world.stored()
    assert stored["file_id"] == "fid-new" and stored["downloaded_at"] is None and stored["deleted_at"] is None
    assert world.kinds().count("file_downloaded") == 0


def test_closed_ask_skipped(hosted, google):
    world = secret_world(hosted, google)
    tool_result(mcp(hosted, world.token, "ask_close", {"id": 1}))
    response = fetch(world)
    assert response.status_code == 410 and claim_docs(world) == {}
    assert world.kinds().count("file_downloaded") == 0


def test_closed_while_sending_writes_nothing(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    real = files_api.HostedAsks._delete_answer_file

    def closed_first(self, *args, **kwargs):
        rewrite(world.drive, 1, status="closed")
        return real(self, *args, **kwargs)
    monkeypatch.setattr(files_api.HostedAsks, "_delete_answer_file", closed_first)
    assert fetch(world).status_code == 200
    assert world.kinds().count("file_downloaded") == 0


def test_normal_file_through_bearer_route_streams(hosted, google):
    world = World(hosted, google)
    response = fetch(world)
    assert response.status_code == 200 and response.content == BODY
    assert response.headers["content-length"] == str(len(BODY))
    assert world.file_id in world.drive.files and claim_docs(world) == {}
    assert world.stored()["downloaded_at"] is not None
    ranged = fetch(world, headers={"Range": "bytes=10-"})
    assert ranged.status_code == 206 and ranged.content == BODY[10:]
    assert hosted.app.state.stream_slots.in_use == 0


def test_bearer_route_checks(hosted, google):
    world = World(hosted, google)
    assert hosted.head(content_path(), headers=bearer(world.token)).status_code == 405
    assert hosted.get(content_path()).status_code == 401
    assert hosted.get(content_path(), headers=id_token(google)).status_code == 403  # the user's own token
    assert hosted.get(content_path(1, 9), headers=bearer(world.token)).status_code == 404
    assert hosted.get(content_path(7, 1), headers=bearer(world.token)).status_code == 404
    other = agent(hosted, google, name="Olga")  # another agent of the same user: not the ask's owner
    assert hosted.get(content_path(), headers=bearer(other)).status_code == 404
    key = hosted.post("/api/agents/keys", json={"name": "Hamilton"}, headers=id_token(google)).json()["key"]
    assert hosted.get(content_path(), headers=bearer(key)).status_code == 404  # an agent key works the same way


def test_bearer_route_budgets(hosted, google):
    world = World(hosted, google)
    for _ in range(20):
        assert fetch(world).status_code == 200
    assert fetch(world).status_code == 429


def test_secret_whose_drive_size_is_not_bytes_404(hosted, google):
    world = secret_world(hosted, google)
    world.drive.files[world.file_id]["content"] = SECRET_BYTES + b"x"  # not the size the item says
    assert fetch(world).status_code == 404 and claim_docs(world) == {} and world.content_reads() == []


def test_secret_over_the_secret_cap_404(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    monkeypatch.setattr(files_api, "MAX_SECRET_BYTES", len(SECRET_BYTES) - 1)  # the route's own cap, not the model's
    assert fetch(world).status_code == 404 and world.content_reads() == [] and claim_docs(world) == {}


def test_secret_drive_failure_releases_the_claim(hosted, google):
    world = secret_world(hosted, google)
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 503, after=1)
    response = fetch(world)
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert claim_docs(world) == {} and fetch(world).content == SECRET_BYTES


def test_two_concurrent_requests_one_gets_bytes(hosted, google, monkeypatch):
    """Review Focus 2: two requests for the same secret file at the same instant: the transaction decides."""
    world = secret_world(hosted, google)
    barrier = threading.Barrier(2)
    real = FileClaims.take

    def together(self, *args, **kwargs):
        barrier.wait(timeout=5)  # both are at the claim before either takes it
        return real(self, *args, **kwargs)
    monkeypatch.setattr(FileClaims, "take", together)
    results = []

    def go():
        results.append(fetch(world))
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], codes
    assert [r.content for r in results if r.status_code == 200] == [SECRET_BYTES]
    monkeypatch.setattr(FileClaims, "take", real)
    assert fetch(world).status_code == 410
    assert len(world.content_reads()) == 1


def test_claims_of_an_ask_go_with_a_close_and_a_reopen(hosted, google):
    world = secret_world(hosted, google)
    store = hosted.store
    store.put("fileclaims", "mine", {"user": DENIS_SUB, "ask_id": 1, "state": "done"})
    store.put("fileclaims", "other-ask", {"user": DENIS_SUB, "ask_id": 2, "state": "done"})
    store.put("fileclaims", "other-user", {"user": "someone-else", "ask_id": 1, "state": "done"})
    tool_result(mcp(hosted, world.token, "ask_close", {"id": 1}))
    assert set(store.all("fileclaims")) == {"other-ask", "other-user"}


def test_no_file_content_in_logs(hosted, google, caplog):
    world = secret_world(hosted, google, name="Q3 report.PDF")
    with caplog.at_level(logging.DEBUG):
        fetch(world)
        fetch(world)
    assert "SECRET-BODY-BYTES" not in caplog.text and "Q3 report" not in caplog.text


# ---------- Task 6, fix round 1 ----------

def test_done_failing_after_a_good_send_still_deletes_and_never_sends_twice(hosted, google, monkeypatch, caplog):
    world = secret_world(hosted, google)
    calls = []

    def broken(self, *args, **kwargs):
        calls.append(1)
        raise RuntimeError("store down")
    monkeypatch.setattr(FileClaims, "done", broken)
    with caplog.at_level(logging.DEBUG):
        assert fetch(world).content == SECRET_BYTES
    assert len(calls) >= 2  # retried
    assert world.file_id not in world.drive.files  # the delete went ahead all the same
    start = time.time()
    monkeypatch.setattr(files_api, "_now", lambda: start + 961)  # the lease has run out
    again = fetch(world)
    assert (again.status_code, again.json()) == (410, DELETED)
    assert len(world.content_reads()) == 1
    assert "SECRET-BODY-BYTES" not in caplog.text


def test_claim_done_is_conditional():
    from server.hosted.store import MemoryStore
    claims = FileClaims(MemoryStore())
    state, owner = claims.take("k", "u", 1, 100.0)
    assert state == "taken"
    claims.done("k", owner + "x", "u", 1)  # not ours
    assert claims.store.get("fileclaims", "k")["state"] == "held"
    claims.delete_for_ask("u", 1)  # a close came first
    claims.done("k", owner, "u", 1)
    assert claims.store.get("fileclaims", "k") is None  # no orphan
    state, owner = claims.take("k", "u", 1, 100.0)
    claims.done("k", owner, "u", 1)
    assert claims.store.get("fileclaims", "k")["state"] == "done"


def test_close_during_a_send_leaves_no_orphan_claim(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    real = files_api.HostedAsks._delete_answer_file
    store = hosted.store

    def claims_dropped_first(self, *args, **kwargs):
        store.all("fileclaims")
        return real(self, *args, **kwargs)
    key = claim_key(DENIS_SUB, 1, world.file_id)
    real_done = FileClaims.done

    def dropped_then_done(self, *args, **kwargs):
        self.delete_for_ask(DENIS_SUB, 1)  # the agent closed the ask while the bytes were going out
        return real_done(self, *args, **kwargs)
    monkeypatch.setattr(FileClaims, "done", dropped_then_done)
    assert fetch(world).status_code == 200
    assert store.get("fileclaims", key) is None


def test_a_reanswer_deletes_the_old_claims(hosted, google):
    from server.hosted.records import HostedAsks
    from server.tests.test_hosted_voice import DENIS
    world = secret_world(hosted, google)
    store = hosted.store
    store.put("fileclaims", "old", {"user": DENIS_SUB, "ask_id": 1, "state": "done"})
    store.put("fileclaims", "other", {"user": DENIS_SUB, "ask_id": 2, "state": "done"})
    asks = HostedAsks(hosted.app.state.pool.records(DENIS_SUB), claims=hosted.app.state.file_claims)
    asks.answer(DENIS, 1, "A different answer")
    assert set(store.all("fileclaims")) == {"other"}
    asks.claims = type("Broken", (), {"delete_for_ask": lambda *a: 1 / 0})()  # never fails the answer
    asks.answer(DENIS, 1, "And another one")
    assert world.ask()["answer"] == "And another one"


def test_cert_fetch_failure_on_the_content_path_is_not_a_500(hosted, google, monkeypatch):
    world = World(hosted, google)
    monkeypatch.setattr(type(hosted.app.state.auth_server.google), "verify_id_token",
                        lambda self, *a, **k: (_ for _ in ()).throw(httpx.ConnectError("certs unreachable")))
    jwtish = "aaa.bbb.ccc"
    response = hosted.get(content_path(), headers=bearer(jwtish))
    assert response.status_code == 401
    assert fetch(world).status_code == 200  # an agent token never reaches the verification


# ---------- Task 10: ask_secret_file, a small secret file inline over MCP (SPEC 7.8) ----------

KIB16 = 16 * 1024
BIG = b"X" * (KIB16 + 1)
SECRET_MARKERS = ("SECRET-BODY-BYTES", "Q3 report", "U0VDUkVU")  # the bytes, the name, the start of their base64


def refusal(response) -> str:
    """The tool error's message without the SDK's "Error executing tool <name>: " prefix."""
    return tool_error(response).removeprefix("Error executing tool ask_secret_file: ")


def secret_call(world, ask_id=1, n=1, token=None):
    return mcp(world.hosted, token or world.token, "ask_secret_file", {"id": ask_id, "n": n})


def test_secret_file_inline_once(hosted, google):
    world = secret_world(hosted, google)
    result = tool_result(secret_call(world))
    assert result == {"name": "Q3 report.PDF", "mime": "application/pdf", "bytes": len(SECRET_BYTES),
                      "content_base64": base64.b64encode(SECRET_BYTES).decode()}
    assert base64.b64decode(result["content_base64"]) == SECRET_BYTES
    assert world.file_id not in world.drive.files  # deleted from Drive, once
    assert len([c for c in world.drive.calls("DELETE", f"/drive/v3/files/{world.file_id}")]) == 1
    stored = world.stored()
    assert stored["file_id"] is None and stored["deleted_reason"] == "sent" and stored["downloaded_at"] is not None
    assert [d["state"] for d in claim_docs(world).values()] == ["done"]
    assert world.kinds()[-1] == "file_downloaded"
    assert world.hosted.app.state.stream_slots.in_use == 0
    again = refusal(secret_call(world))
    assert "already sent" in again
    assert (fetch(world).status_code, fetch(world).json()) == (410, DELETED)  # the bearer route: 410
    assert len(world.content_reads()) == 1


def test_secret_file_inline_after_the_route_says_already_sent(hosted, google):
    world = secret_world(hosted, google)
    assert fetch(world).content == SECRET_BYTES
    assert "already sent" in refusal(secret_call(world))
    assert len(world.content_reads()) == 1


def test_secret_file_inline_done_claim_says_already_sent(hosted, google):
    world = secret_world(hosted, google)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{world.file_id}", 500)
    tool_result(secret_call(world))
    assert world.file_id in world.drive.files  # the sweep retries; the claim is done
    assert "already sent" in refusal(secret_call(world))
    assert len(world.content_reads()) == 1


def test_secret_file_inline_over_1mib_refused_claim_untouched(hosted, google):
    world = World(hosted, google, content=BIG, secret=True)
    text = refusal(secret_call(world))
    assert text == "too big for MCP — fetch it with `askhooman files 1 --secret`"
    assert claim_docs(world) == {} and world.content_reads() == [] and world.file_id in world.drive.files
    assert fetch(world).content == BIG  # the route still sends it (<= 10 MiB)


def test_secret_file_inline_exactly_16_kib_goes(hosted, google):
    world = World(hosted, google, content=b"Y" * KIB16, secret=True)
    assert tool_result(secret_call(world))["bytes"] == KIB16


def test_secret_file_inline_cap_is_16_kib_because_clients_cap_tool_output(hosted, google):
    """Final review: a 1 MiB file is ~1.4 MB of base64; MCP clients cap a tool result (~25k tokens), so a bigger one
    would be marked sent, deleted, and dropped by the client."""
    assert files_api.MCP_MAX_BYTES == 16 * 1024
    world = World(hosted, google, content=b"Z" * (KIB16 + 1), secret=True)
    assert refusal(secret_call(world)) == "too big for MCP — fetch it with `askhooman files 1 --secret`"
    assert claim_docs(world) == {} and world.content_reads() == [] and world.file_id in world.drive.files


def test_secret_file_inline_normal_file_points_to_url(hosted, google):
    world = World(hosted, google)
    assert refusal(secret_call(world)) == "this file has a url — download it with the url"
    assert claim_docs(world) == {} and world.content_reads() == [] and world.file_id in world.drive.files


def test_secret_file_inline_other_agent_404(hosted, google):
    world = secret_world(hosted, google)
    other = agent(hosted, google, name="Feynman")
    assert refusal(secret_call(world, token=other)) == NO_SUCH["detail"]
    assert claim_docs(world) == {} and world.content_reads() == [] and world.file_id in world.drive.files
    assert refusal(secret_call(world, n=9)) == NO_SUCH["detail"]  # no such item: the same text
    assert refusal(secret_call(world, ask_id=77)) == NO_SUCH["detail"]
    route = world.hosted.get(content_path(), headers=bearer(other))
    assert route.json() == NO_SUCH  # the route's text, the same


def test_secret_file_inline_held_claim_busy(hosted, google):
    world = secret_world(hosted, google)
    key = claim_key(DENIS_SUB, 1, world.file_id)
    hosted.store.put("fileclaims", key, {"user": DENIS_SUB, "ask_id": 1, "state": "held", "until": time.time() + 500,
                                         "owner": "someone"})
    assert refusal(secret_call(world)) == "being sent"
    assert world.content_reads() == [] and world.file_id in world.drive.files
    assert claim_docs(world)[key]["owner"] == "someone"


def test_secret_file_inline_no_bytes_in_logs(hosted, google, caplog):
    world = secret_world(hosted, google)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{world.file_id}", 500)  # the noisy path too
    with caplog.at_level(logging.DEBUG):
        tool_result(secret_call(world))
        secret_call(world)
    for marker in SECRET_MARKERS:  # (a space, an upper-case run: no random token holds them)
        assert marker not in caplog.text
    assert base64.b64encode(SECRET_BYTES).decode()[:24] not in caplog.text


def test_secret_file_inline_files_bucket(hosted, google):
    world = secret_world(hosted, google)
    for _ in range(20):
        secret_call(world, n=9)  # refused as no such item, but each counts
    text = refusal(secret_call(world))
    assert text == "too many requests for files: at most 20 a minute per agent"
    assert claim_docs(world) == {} and world.content_reads() == []


def test_secret_file_inline_shares_the_route_bucket(hosted, google):
    world = secret_world(hosted, google)
    for _ in range(20):
        fetch(world, headers={"Range": "bytes=0-"})  # 20 asks of the route (the first sends; then 410s)
    assert "too many requests" in refusal(secret_call(world))


def test_secret_file_inline_drive_failure_releases_the_claim(hosted, google):
    world = secret_world(hosted, google)
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 500, after=1)  # the size check passes first
    assert refusal(secret_call(world)) == "Google Drive is not available right now, try again"
    assert claim_docs(world) == {} or all(d["state"] != "held" for d in claim_docs(world).values())
    assert world.hosted.app.state.stream_slots.in_use == 0
    assert tool_result(secret_call(world))["bytes"] == len(SECRET_BYTES)  # the agent can try again


def test_secret_file_inline_closed_ask_410(hosted, google):
    world = secret_world(hosted, google)
    tool_result(mcp(hosted, world.token, "ask_close", {"id": 1}))
    assert refusal(secret_call(world)) == DELETED["detail"]


def test_secret_file_inline_drive_size_not_bytes_404(hosted, google):
    world = secret_world(hosted, google)
    world.drive.files[world.file_id]["content"] += b"!"  # Drive holds more than the item says
    assert refusal(secret_call(world)) == NO_SUCH["detail"] and claim_docs(world) == {}


ALREADY_SENT_TEXT = "already sent \u2014 if you did not receive it, ask your user to send it again"


def test_secret_file_inline_already_sent_says_what_to_do(hosted, google):
    world = secret_world(hosted, google)
    tool_result(secret_call(world))
    assert refusal(secret_call(world)) == ALREADY_SENT_TEXT


def test_secret_file_inline_done_claim_says_what_to_do_too(hosted, google):
    world = secret_world(hosted, google)
    key = claim_key(DENIS_SUB, 1, world.file_id)
    hosted.store.put("fileclaims", key, {"user": DENIS_SUB, "ask_id": 1, "state": "done", "owner": "x"})
    assert refusal(secret_call(world)) == ALREADY_SENT_TEXT


def test_secret_file_inline_non_agent_identity_refused_before_any_claim(hosted, google):
    from server.tokens import Identity
    world = secret_world(hosted, google)
    state = world.hosted.app.state
    fetch_tool = files_api.secret_file_tool(world.hosted.app, state.accounts, state.pool, state.limiter,
                                            state.file_claims)
    with pytest.raises(files_api.FileRefused) as caught:
        fetch_tool(Identity("user", "denis"), 1, 1)
    assert caught.value.status_code == 403 and "only the agent that asked" in str(caught.value)
    assert claim_docs(world) == {} and world.content_reads() == [] and world.file_id in world.drive.files


# ---------- final review: I1 (sent once means sent once) and I2 (lifetimes by Drive's createdTime) ----------

def test_a_secret_sent_once_is_not_sent_again_when_a_reanswer_keeps_the_item(hosted, google):
    """The delete after the send failed: the item keeps file_id and has downloaded_at. The user changes the answer and
    keeps the item (SPEC 7.3 step 7: re-stamped, downloaded_at kept): a new answered_at is a new claim key, and the
    route used to send the file a second time."""
    world = secret_world(hosted, google)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{world.file_id}", 500)
    assert fetch(world).content == SECRET_BYTES
    kept = world.stored()
    assert kept["file_id"] == world.file_id and kept["downloaded_at"] is not None
    answered_by_files(world.drive, 1, [kept], text="Changed my mind")
    assert world.stored()["answered_at"] != kept["answered_at"]
    again = fetch(world)
    assert (again.status_code, again.json()) == (410, SENT)
    assert "already sent" in refusal(secret_call(world))
    assert len(world.content_reads()) == 1  # Drive was asked for the bytes once


def test_a_secret_with_downloaded_at_is_410_before_any_drive_call_or_claim(hosted, google):
    world = secret_world(hosted, google)
    rewrite(world.drive, 1, answer_files=[{**world.stored(), "downloaded_at": "2026-11-19T09:00:00.000Z"}])
    response = fetch(world)
    assert (response.status_code, response.json()) == (410, SENT)
    assert claim_docs(world) == {} and world.drive.calls("GET", f"/drive/v3/files/{world.file_id}") == []


def shifted(world, seconds: float) -> None:
    """The user's clock is `seconds` off: the ask's answered_at and the item's stamps."""
    from datetime import datetime, timedelta
    ask = world.ask()
    move = lambda stamp: (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat(  # noqa: E731
        timespec="milliseconds").replace("+00:00", "Z")
    at = move(ask["answered_at"])
    rewrite(world.drive, 1, answered_at=at,
            answer_files=[{**world.stored(), "answered_at": at, "uploaded_at": at}])


def created_of(world) -> float:
    from datetime import datetime
    return datetime.fromisoformat(world.drive.files[world.file_id]["createdTime"]).timestamp()


def test_a_clock_two_days_behind_does_not_make_a_secret_expired_early(hosted, google):
    world = secret_world(hosted, google)
    shifted(world, -2 * 86400)
    response = fetch(world)
    assert response.status_code == 200 and response.content == SECRET_BYTES


def test_a_clock_three_days_ahead_does_not_make_a_secret_last_longer(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    shifted(world, 3 * 86400)
    monkeypatch.setattr(files_api, "_now", lambda: created_of(world) + 24 * 3600 + 10 * 60 + 1)
    response = fetch(world)
    assert (response.status_code, response.json()) == (410, {"detail": "expired"})
    assert world.content_reads() == [] and claim_docs(world) == {} and world.file_id in world.drive.files


def test_a_clock_three_days_ahead_still_serves_until_createdTime_plus_24h_and_the_slack(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    shifted(world, 3 * 86400)
    monkeypatch.setattr(files_api, "_now", lambda: created_of(world) + 24 * 3600 + 10 * 60 - 1)
    assert fetch(world).content == SECRET_BYTES


# ---------- final review: I4 (only a reason Drive says is permanent for the file blocks it for good) ----------

NOT_A_BLOCK = ["quotaExceeded", "dailyLimitExceeded", "downloadQuotaExceeded", "sharingRateLimitExceeded",
               "appNotAuthorizedToFile", "domainPolicy", ""]


@pytest.mark.parametrize("reason", NOT_A_BLOCK)
def test_a_403_that_is_not_permanent_is_503_and_stamps_nothing_on_a_normal_file(world, reason):
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason=reason)
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert world.stored()["blocked_at"] is None and "file_blocked" not in world.kinds()
    assert world.hosted.app.state.stream_slots.in_use == 0
    assert world.get().content == BODY  # Drive is fine again: nothing was sticky


@pytest.mark.parametrize("reason", NOT_A_BLOCK)
def test_a_403_that_is_not_permanent_is_503_and_stamps_nothing_on_a_secret_file(hosted, google, reason):
    world = secret_world(hosted, google)
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason=reason)
    response = fetch(world)
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert world.stored()["blocked_at"] is None and "file_blocked" not in world.kinds()
    assert claim_docs(world) == {} and world.hosted.app.state.stream_slots.in_use == 0  # the claim was released
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason=reason)
    assert "Google Drive is not available" in refusal(secret_call(world))  # the tool says the same
    assert world.stored()["blocked_at"] is None
    assert fetch(world).content == SECRET_BYTES


def test_the_abusive_file_403_blocks_a_secret_file_too(hosted, google):
    world = secret_world(hosted, google)
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason="cannotDownloadAbusiveFile")
    response = fetch(world)
    assert (response.status_code, response.json()) == (410, BLOCKED)
    assert world.stored()["blocked_at"] is not None and world.kinds()[-1] == "file_blocked"
    assert claim_docs(world) == {} and world.file_id in world.drive.files


# ---------- final review: M1 (one agent can't take every stream slot) and M4 (done claims expire) ----------

def who_of(world) -> str:
    return f"{DENIS_SUB}:{agent_id(world.hosted, world.google)}"


def test_slots_give_one_owner_at_most_its_share():
    from server.hosted.media_api import Slots
    slots = Slots(4, per_owner=2)
    assert slots.take(1, "a") and slots.take(1, "a") and not slots.take(1, "a")  # a has its two
    assert slots.take(1, "b") and slots.take(1, "b") and not slots.take(1, "c")  # the instance has its four
    slots.give(1, "a")
    assert slots.take(1, "c") and slots.in_use == 4
    assert not slots.take(1, "a")  # a holds one, the instance is full
    for owner in ("a", "b", "b", "c"):
        slots.give(1, owner)
    assert slots.in_use == 0 and slots._owned == {}  # nothing is remembered for an owner that holds none
    assert Slots(4).take(4)  # an owner-less slot set (the media slots) has no such share


def test_one_agent_holding_two_streams_cannot_take_a_third_the_others_still_can(world):
    slots, who = world.hosted.app.state.stream_slots, who_of(world)
    assert slots.per_owner == 2 and slots.size == 4
    assert slots.take(1, who) and slots.take(1, who)  # this agent's two downloads in flight
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert world.content_reads() == []
    assert slots.take(1, "someone else") and slots.take(1, "and another")  # the instance has two for the rest
    slots.give(1, who)
    slots.give(1, "someone else")
    assert world.get().content == BODY
    assert slots.in_use == 2  # a finished download gives its slot back to its own agent's share


def test_a_secret_send_counts_against_the_same_share_and_releases_the_claim(hosted, google):
    world = secret_world(hosted, google)
    slots, who = hosted.app.state.stream_slots, who_of(world)
    assert slots.take(1, who) and slots.take(1, who)
    response = fetch(world)
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert claim_docs(world) == {} and world.file_id in world.drive.files
    slots.give(1, who)
    assert fetch(world).content == SECRET_BYTES


def test_a_done_claim_expires_in_90_days(hosted, google):
    world = secret_world(hosted, google)
    assert fetch(world).content == SECRET_BYTES
    [doc] = claim_docs(world).values()
    assert doc["state"] == "done"
    assert abs(doc["expires_at"] - (time.time() + 90 * 86400)) < 60


# ---------- fix round 2: I-D (a file is sent at most once, whatever answer version names it) ----------

def failing_mark_sent(monkeypatch, times):
    real, calls = files_api.mark_sent, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) <= times:
            raise RuntimeError("the ask could not be written")
        return real(*args, **kwargs)
    monkeypatch.setattr(files_api, "mark_sent", flaky)
    return calls


def test_a_secret_is_not_sent_twice_when_every_write_after_the_send_failed(hosted, google, monkeypatch):
    """The exact sequence: the claim is done, the Drive delete failed, the ask write failed too (no downloaded_at);
    a re-answer then re-stamps answered_at. The claim used to be keyed by answered_at: a new key, sent again."""
    world = secret_world(hosted, google)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{world.file_id}", 500)
    failing_mark_sent(monkeypatch, 99)
    assert fetch(world).content == SECRET_BYTES
    kept = world.stored()
    assert kept["file_id"] == world.file_id and kept["downloaded_at"] is None  # nothing says it was sent but the claim
    answered_by_files(world.drive, 1, [kept], text="Changed my mind")
    assert world.stored()["answered_at"] != kept["answered_at"]
    again = fetch(world)
    assert (again.status_code, again.json()) == (410, SENT)
    assert "already sent" in refusal(secret_call(world))
    assert len(world.content_reads()) == 1
    from server.hosted.sweep import sweep
    app = world.hosted.app.state
    _, stats = sweep(app.store, app.pool, time.time(), None, 10)
    assert stats["deleted_sent"] == 1 and world.file_id not in world.drive.files  # and the sweep finishes the delete


def test_the_ask_write_after_a_send_is_retried_like_the_claim(hosted, google, monkeypatch):
    world = secret_world(hosted, google)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{world.file_id}", 500)
    calls = failing_mark_sent(monkeypatch, 2)
    assert fetch(world).content == SECRET_BYTES
    assert len(calls) == 3 and world.stored()["downloaded_at"] is not None


def test_the_claim_does_not_depend_on_answered_at():
    assert claim_key("u", 7, "fileid-xyz") == claim_key("u", 7, "fileid-xyz")
    assert claim_key("u", 7, "fileid-xyz") != claim_key("u", 7, "fileid-abc")
    assert claim_key("u", 7, "fileid-xyz") != claim_key("u", 8, "fileid-xyz")
    assert claim_key("u", 7, "fileid-xyz") != claim_key("v", 7, "fileid-xyz")


# ---------- fix round 2: M2 (the secret body goes out in 64 KiB messages: «sent» means back-pressure drained) ----------

BIG_SECRET = bytes(range(256)) * (MIB // 256)  # 1 MiB


def asgi_with_first_chunk_event(world, script) -> dict:
    """Like asgi_bearer, and `script(first_chunk)` gets an event set when the first body message has been sent."""
    host = PUBLIC.split("://", 1)[1]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
             "path": content_path(), "raw_path": content_path().encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host.encode()), (b"authorization", f"Bearer {world.token}".encode())],
             "client": ("127.0.0.1", 5000), "server": (host, 443)}
    out, first = {"messages": [], "error": None}, anyio.Event()
    feed = script(first)

    async def receive():
        return await feed.__anext__()

    async def send(message):
        out["messages"].append(message)
        if message["type"] == "http.response.body":
            first.set()
            await anyio.sleep(0)  # a real server's send yields: the client's disconnect can be seen between chunks

    async def run():
        try:
            await world.hosted.app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc.exceptions[0] if isinstance(exc, BaseExceptionGroup) else exc

    anyio.run(run)
    return out


def test_the_secret_body_goes_out_in_64_kib_messages_with_its_content_length(hosted, google):
    world = World(hosted, google, content=BIG_SECRET, secret=True)
    out = asgi_bearer(world, _disconnect_after_final)
    assert out["error"] is None
    start, *bodies = out["messages"]
    assert start["status"] == 200 and dict(start["headers"])[b"content-length"] == str(len(BIG_SECRET)).encode()
    assert len(bodies) == len(BIG_SECRET) // (64 * 1024) and all(len(b["body"]) <= 64 * 1024 for b in bodies)
    assert [b["more_body"] for b in bodies] == [True] * (len(bodies) - 1) + [False]
    assert b"".join(b["body"] for b in bodies) == BIG_SECRET
    assert [d["state"] for d in claim_docs(world).values()] == ["done"] and world.file_id not in world.drive.files


async def _disconnect_after_the_first_chunk(first):
    yield {"type": "http.request", "body": b"", "more_body": False}
    await first.wait()
    yield {"type": "http.disconnect"}
    await anyio.sleep_forever()


def test_a_disconnect_between_chunks_means_not_sent(hosted, google):
    world = World(hosted, google, content=BIG_SECRET, secret=True)
    out = asgi_with_first_chunk_event(world, _disconnect_after_the_first_chunk)
    assert out["error"] is None
    assert claim_docs(world) == {}  # released: the agent can ask again, the file is still there
    assert world.file_id in world.drive.files and world.stored()["downloaded_at"] is None
    assert fetch(world).content == BIG_SECRET


# ---------- fix round 3: M-2 (a short pause between the retries of the writes after a send) ----------

def test_the_retries_after_a_send_pause_between_attempts(hosted, google, monkeypatch):
    pauses = []
    monkeypatch.setattr(files_api, "_pause", pauses.append)
    world = secret_world(hosted, google)
    real_done = FileClaims.done
    tries = []

    def flaky_done(self, *args, **kwargs):
        tries.append(1)
        if len(tries) <= 2:
            raise RuntimeError("store hiccup")
        return real_done(self, *args, **kwargs)
    monkeypatch.setattr(FileClaims, "done", flaky_done)
    failing_mark_sent(monkeypatch, 2)
    assert fetch(world).content == SECRET_BYTES
    assert pauses == [0.2, 0.5, 0.2, 0.5]  # claims.done failed twice, then mark_sent failed twice
    assert [d["state"] for d in claim_docs(world).values()] == ["done"] and world.stored()["downloaded_at"] is not None


# ---------- Task 11: a Content-Length and at most PART_BYTES per response (spike 1); bigger files in parts ----------

import random  # noqa: E402

from server.hosted.files import PART_BYTES  # noqa: E402

LARGE = random.Random(11).randbytes(PART_BYTES + 3 * 4096)  # no period: a part from a wrong offset can't match
TOO_BIG = (f"this file is {len(LARGE)} bytes, more than one response can carry: fetch it in parts with Range: "
           "bytes=START-END, at most 32505856 bytes each (askhooman files does this)")
PART_TOO_BIG = "a part can be at most 32505856 bytes: ask for Range: bytes=START-END with END-START+1 <= 32505856"


@pytest.fixture
def large(hosted, google):
    return World(hosted, google, content=LARGE)


def no_file_calls(world) -> bool:
    """Nothing asked of Drive about the answer file itself (neither its metadata nor its bytes)."""
    return world.drive.calls("GET", f"/drive/v3/files/{world.file_id}") == []


def drive_ranges(world) -> list:
    """The Range headers the bridge sent to Drive, in order."""
    return [h["range"] for h in world.drive.headers if "range" in h]


def ranged(world, header, bearer_route=False):
    if bearer_route:
        return fetch(world, headers={"Range": header})
    return world.get(headers={"Range": header})


def test_part_bytes_is_31_mib():
    assert PART_BYTES == 32_505_856 == 31 * MIB and files_api.PART_BYTES == PART_BYTES


@pytest.mark.parametrize("bearer_route", [False, True])
def test_large_file_without_range_is_409_with_the_part_size(large, bearer_route):
    budget = ByteBudget()
    large.hosted.app.state.file_bytes = budget
    slots = large.hosted.app.state.stream_slots
    assert slots.take(4)  # every slot busy: the 409 needs none
    response = fetch(large) if bearer_route else large.get()
    slots.give(4)
    assert (response.status_code, response.json()) == (409, {"detail": TOO_BIG})
    assert response.headers["cache-control"] == "no-store"
    assert no_file_calls(large)  # decided from the item's bytes: not even its metadata is read
    assert budget._charges == {} and slots.in_use == 0
    assert large.stored()["downloaded_at"] is None


@pytest.mark.parametrize("bearer_route", [False, True])
def test_closed_range_part_is_206_with_its_length_and_drive_range(large, bearer_route):
    start, end = 4096 + 7, 3 * 4096 + 2  # a middle part
    response = ranged(large, f"bytes={start}-{end}", bearer_route)
    assert response.status_code == 206 and response.content == LARGE[start:end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(LARGE)}"
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith('attachment; filename="ask-1-file-1.pdf"')
    assert drive_ranges(large) == [f"bytes={start}-{end}"]
    assert large.hosted.app.state.stream_slots.in_use == 0


def test_part_over_part_bytes_is_416(large):
    unsatisfiable = f"bytes */{len(LARGE)}"
    for header in (f"bytes=0-{PART_BYTES}", f"bytes=100-{PART_BYTES + 100}",  # closed: PART_BYTES + 1 bytes
                   "bytes=0-", f"bytes={len(LARGE) - PART_BYTES - 1}-",  # open: the rest is PART_BYTES + 1
                   f"bytes=0-{10 * len(LARGE)}"):  # clamped to the file, still too big
        response = ranged(large, header)
        assert (response.status_code, response.json()) == (416, {"detail": PART_TOO_BIG}), header
        assert response.headers["content-range"] == unsatisfiable
    assert no_file_calls(large)
    fits = ranged(large, f"bytes=0-{PART_BYTES - 1}")  # exactly PART_BYTES: the largest part
    assert fits.status_code == 206 and fits.headers["content-length"] == str(PART_BYTES)
    assert fits.content == LARGE[:PART_BYTES]
    assert fits.headers["content-range"] == f"bytes 0-{PART_BYTES - 1}/{len(LARGE)}"


def test_open_range_whose_rest_fits_is_206(large):
    start = len(LARGE) - 5000
    response = ranged(large, f"bytes={start}-")
    assert response.status_code == 206 and response.content == LARGE[start:]
    assert response.headers["content-range"] == f"bytes {start}-{len(LARGE) - 1}/{len(LARGE)}"
    assert response.headers["content-length"] == "5000"
    assert drive_ranges(large) == [f"bytes={start}-"]  # an open range goes to Drive as it came


def test_closed_range_end_past_the_file_is_clamped(large):
    start = len(LARGE) - 4096
    response = ranged(large, f"bytes={start}-{len(LARGE) + 10 ** 9}")
    assert response.status_code == 206 and response.content == LARGE[start:]
    assert response.headers["content-range"] == f"bytes {start}-{len(LARGE) - 1}/{len(LARGE)}"
    assert response.headers["content-length"] == "4096"
    assert drive_ranges(large) == [f"bytes={start}-{len(LARGE) - 1}"]  # Drive is asked for the clamped range
    exact = ranged(large, f"bytes={start}-{len(LARGE)}")  # one past the end
    assert exact.status_code == 206 and exact.content == LARGE[start:]


def test_small_file_closed_range_is_206(world):
    response = ranged(world, "bytes=5-9")
    assert response.status_code == 206 and response.content == BODY[5:10]
    assert response.headers["content-range"] == f"bytes 5-9/{len(BODY)}"
    assert response.headers["content-length"] == "5"
    assert drive_ranges(world) == ["bytes=5-9"]
    assert world.stored()["downloaded_at"] is None  # not the end of the file
    whole = ranged(world, f"bytes=0-{len(BODY) + 99}")  # the whole file as one part, clamped
    assert whole.status_code == 206 and whole.content == BODY
    assert whole.headers["content-range"] == f"bytes 0-{len(BODY) - 1}/{len(BODY)}"
    assert world.stored()["downloaded_at"] is not None
    for header in ("bytes=9-5", "bytes=-500", "bytes=0-5,10-20", "bytes=x-", f"bytes={len(BODY)}-9999"):
        refused = ranged(world, header)
        assert refused.status_code == 416 and refused.headers["content-range"] == f"bytes */{len(BODY)}", header
    assert len(world.content_reads()) == 2  # the refused ones never reached Drive: not its metadata either
    assert len(world.drive.calls("GET", f"/drive/v3/files/{world.file_id}")) == 4
    plain = world.get()  # without a Range: unchanged
    assert plain.status_code == 200 and plain.content == BODY and "content-range" not in plain.headers


def test_downloaded_at_only_after_the_last_part(large):
    last = len(LARGE) - 1
    for header in ("bytes=4096-8191", "bytes=0-4095", f"bytes={last - 4096}-{last - 1}"):  # parts before the last byte
        assert ranged(large, header).status_code == 206
        assert large.stored()["downloaded_at"] is None and "file_downloaded" not in large.kinds()
    assert ranged(large, f"bytes={last - 4095}-{last}").status_code == 206  # the last part
    stored = large.stored()
    assert stored["downloaded_at"] is not None and large.kinds().count("file_downloaded") == 1
    rev = large.ask()["rev"]
    assert ranged(large, f"bytes={last - 9}-").status_code == 206  # the last part again: written once
    assert ranged(large, "bytes=0-9").status_code == 206
    assert large.ask()["rev"] == rev and large.kinds().count("file_downloaded") == 1 and large.stored() == stored


def test_budget_charges_the_part_not_the_file(large):
    budget = ByteBudget(limit=3 * 4096)  # far less than the file: its parts still go
    large.hosted.app.state.file_bytes = budget
    assert ranged(large, "bytes=0-4095").status_code == 206
    assert charged(large, budget) == 4096
    assert ranged(large, f"bytes={len(LARGE) - 4000}-").status_code == 206  # an open range: its rest
    assert charged(large, budget) == 8096
    assert ranged(large, "bytes=4096-8191").status_code == 206
    assert charged(large, budget) == 12192
    refused = ranged(large, "bytes=8192-8288")  # 97 bytes: one more than the limit has room for
    assert refused.status_code == 429 and charged(large, budget) == 12192
    assert ranged(large, "bytes=8192-8287").status_code == 206 and charged(large, budget) == budget.limit


@pytest.mark.parametrize("header", ["bytes=2-5", "bytes=9-5", f"bytes=0-{PART_BYTES}", "bytes=10-"])
def test_secret_file_ignores_range_and_is_unchanged(hosted, google, header):
    world = secret_world(hosted, google)
    response = fetch(world, headers={"Range": header})
    assert response.status_code == 200 and response.content == SECRET_BYTES
    assert response.headers["content-length"] == str(len(SECRET_BYTES)) and "content-range" not in response.headers
    assert drive_ranges(world) == []  # read whole from Drive, as before
    assert world.stored()["downloaded_at"] is not None and world.file_id not in world.drive.files


def test_a_part_that_drive_sends_long_or_short_aborts(hosted, google, monkeypatch):
    """_Streamed checks the part's own length: Drive answers the range with more (or fewer) bytes than asked."""
    world = World(hosted, google, content=LARGE)
    start, end = len(LARGE) - 4096, len(LARGE) - 1  # the last part: a complete one would be stamped
    extra = {"bytes": b"EXTRA-BYTES"}

    def media(self, file, request_range):
        body = file["content"][start:end + 1]
        body = body + extra["bytes"] if extra["bytes"] else body[:-100]
        return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(LARGE)}",
                                            "Content-Length": str(len(body))}, content=body)

    original = FakeDrive._media

    def routed(self, file, request_range):  # the ask files are read as they are
        if file["appProperties"].get("kind") != "file":
            return original(file, request_range)
        return media(self, file, request_range)

    monkeypatch.setattr(FakeDrive, "_media", routed)
    failing_download(world, headers={"Range": f"bytes={start}-{end}"})
    extra["bytes"] = b""
    failing_download(world, headers={"Range": f"bytes={start}-{end}"})
    assert world.stored()["downloaded_at"] is None and "file_downloaded" not in world.kinds()
    assert hosted.app.state.stream_slots.in_use == 0


# ---------- Task 11, fix round 2: the span is decided before Drive; long numbers; other range units ----------

BAD_RANGE = {"detail": "a Range served here is bytes=START-END (START <= END) or bytes=START-"}


def test_absurdly_long_numbers_are_416_never_a_500(world):
    long = "9" * 5000  # past Python's int() digit limit: must never reach int()
    for header in (f"bytes={long}-", f"bytes=0-{long}", f"bytes={long}-{long}", "bytes=0-99999999999999999999"):
        response = ranged(world, header)
        assert (response.status_code, response.json()) == (416, BAD_RANGE), header[:30]
        assert response.headers["content-range"] == f"bytes */{len(BODY)}"
    assert no_file_calls(world)
    clamped = ranged(world, "bytes=10-9999999999999999999")  # 19 digits: still a number, clamped to the file
    assert clamped.status_code == 206 and clamped.content == BODY[10:]


def test_a_range_in_another_unit_is_ignored(world):
    for header in ("items=0-5", "x-bytes=0-5", "pages=1"):  # RFC 9110 14.2: an unknown unit is ignored
        response = ranged(world, header)
        assert response.status_code == 200 and response.content == BODY and "content-range" not in response.headers
    assert drive_ranges(world) == []


def test_the_bytes_unit_is_case_insensitive(world):
    for header in ("Bytes=0-4", "BYTES=5-"):
        response = ranged(world, header)
        assert response.status_code == 206, header
    assert drive_ranges(world) == ["bytes=0-4", "bytes=5-"]


def test_a_large_file_with_a_range_in_another_unit_is_409(large):
    response = ranged(large, "items=0-5")
    assert (response.status_code, response.json()) == (409, {"detail": TOO_BIG})
    assert no_file_calls(large)
