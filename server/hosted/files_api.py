"""GET /media/v1/<token> for a normal answer file (SPEC v5 files in answers, 7.4 "The route"): the agent that asked
fetches a file its user attached to the answer with the link `url` carried (files.FileLinks, sealed under the context
"file"; voice_api.add_voice_route hands every token it can't open as a recording to download() here). The token is the
only credential: no Authorization is looked at, and there is no CORS.

Checked in this order, one 404 message for the first group: the token opens (and has not expired); the budget of the
token's user and agent (`files`, 20 a minute: 429); the agent is still connected and the ask is its own; the ask's
CURRENT answer_files has item `n` with this file_id and answered_at (a reopened ask, a newer answer or a secret item:
404; a closed ask or a deleted item: 410); Drive says the file is this ask's (appProperties kind "file", ask_id and
file = n) and its size is `bytes` and within MAX_FILE_BYTES (404); what the response carries (below) is decided
from the item's `bytes` just before that Drive check, so its 409 or 416 asks Drive nothing about the file. Then a
stream slot (503, Retry-After 5), the byte budget (`file_bytes`, 1 GiB an hour per agent, charged with this response's
own bytes: 429), and the bytes are streamed from the user's Drive in 1 MiB chunks (never held whole) with

    Content-Type             application/octet-stream, never what Drive or the user says
    Content-Length           the bytes of this response: the file's `bytes`, or the part's (206 with Content-Range)
    Content-Disposition      attachment; filename="ask-<id>-file-<n>[.<ext>]"; filename*=UTF-8''<name>
    Cache-Control            private, no-store
    X-Content-Type-Options   nosniff
    Content-Security-Policy  sandbox

Every response carries a Content-Length and at most PART_BYTES (31 MiB) of body. Spike 1 (20.11.2026) measured Cloud
Run: a response of 32 MiB or more is refused (500 «Response size was too large», 31 MiB passes), and a body without a
length is no way round it: when the server aborts mid-body or Cloud Run's timeout ends the request, curl and urllib
see a clean end with 200, a silent short file; with a length the same abort is a visible 502. So:
- no Range: the whole file (200) if `bytes` <= PART_BYTES; a larger file is 409 «this file is N bytes, more than one
  response can carry: fetch it in parts with Range: bytes=START-END, at most 32505856 bytes each (askhooman files does
  this)», before any slot, charge or Drive read;
- `Range: bytes=A-B` (a part; also on a small file) and `bytes=A-` (to the end) are 206 with Content-Range
  `bytes A-B/<bytes>`; an end past the file is clamped to its last byte (RFC 9110); Drive is asked for the same
  range (`bytes=0-` of a small file is the whole file, 200); the unit is case-insensitive, and a Range in another
  unit is ignored, as if there were none (RFC 9110 14.2);
- a part of more than PART_BYTES (closed or open, after the clamp) is 416 «a part can be at most 32505856 bytes: ask
  for Range: bytes=START-END with END-START+1 <= 32505856»; a start past the end, an end before the start, a number
  of more than 19 digits or any other bytes form is 416 too; every 416 carries `Content-Range: bytes */<bytes>`.

Content-Length is set, so the body must match it: what is sent is counted against it, and a Drive stream that ends
short or runs on aborts the response (an exception after the headers: the server cuts the connection, the agent sees
a failed download, never a short file). A Drive 403 on the content read with a permanent reason (drive.BLOCK_REASONS:
cannotDownloadAbusiveFile) is 410 "Google blocked this file" and the item gets `blocked_at`; any other 403 (a rate or
download quota, a policy, an unknown reason) and Drive unreachable are 503 with Retry-After: 30, nothing stamped. `HEAD` is 405.

`downloaded_at` (and `blocked_at`) are written after the response has gone, by mark_downloaded (mark_blocked): a
read-modify-write of the ask applied only if it is still answered, item `n` still has this file_id and answered_at,
and the stamp is still null, with a `file_downloaded` ({id}) event. Only a response that ends at the file's last byte
(the whole file, or the last part) writes it, once it has gone out whole; a middle part writes nothing. «Gone out»
means handed to Cloud Run's front end, which takes the whole body at once (spike 1), not that the agent has it.
Nothing of the file, its name or the token is logged: ids only.

GET /api/asks/{id}/files/{n}/content (SPEC 7.4, "The route" and "Secret file") is the same route for the owner agent's
own bearer (an OAuth access token or an agent key; the user's own token is 403, the agent check is app.authenticate's):
the same checks in the same order (_check, shared), then a normal file is streamed exactly as above (Range included),
and a SECRET file is sent once. A secret file must be at most MAX_SECRET_BYTES, without a downloaded_at (it was sent:
410 "already sent", whatever its claim key says), younger than SECRET_TTL (min of its uploaded_at and answered_at,
bounded by Drive's createdTime, files.lifetime_end; else 410 "expired", the sweep deletes it), and is read WHOLE
from Drive and size-checked before the first byte; then it is sent as one response with a Content-Length, its body in 64 KiB messages (a Range header is ignored). The claim
(files.FileClaims, collection fileclaims) is taken before Drive is read: none -> held (lease REQUEST_TIMEOUT + 60 s;
a held, unexpired claim is 409 "being sent", an expired one is taken over); held -> none when the send did not
complete (a Drive error before the first byte, a refused budget, a disconnect seen by _Once, an exception);
held -> done when the final message was sent without an error and with no http.disconnect received before it (on Cloud
Run «sent» means handed to Cloud Run, which takes the 10 MiB in milliseconds, and the disconnect never arrived before
the last send in spike 1: the check matters only behind no buffering proxy; if the agent's download then breaks, the
file is gone and its user has to send it again); a done claim is 410. After the response (and the slot given back)
the claim is made done FIRST, then the Drive file is deleted
(HostedAsks._delete_answer_file: only a file of this ask and item) and the ask is written (downloaded_at, deleted_at,
deleted_reason "sent", file_id null, event file_downloaded) under the guards of mark_downloaded: a closed, reopened or
re-answered ask, or an item that no longer names this file, is skipped. A failed delete leaves the file in Drive, the
claim done (nothing can read it) and the item as it was but downloaded_at: the sweep retries the delete.
"""

import base64
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import anyio
import anyio.lowlevel
import anyio.to_thread
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..app import current_identity
from ..db import utc_now
from ..drive import BLOCK_REASONS, DriveBadFile, DriveBlocked, DriveError, DriveNotFound
from ..rules import ANSWER_FILES, AskError, Change, NotFound, Unavailable
from ..tokens import Identity
from .accounts import Accounts, HostedIdentity
from .files import (
    MAX_FILE_BYTES,
    MAX_SECRET_BYTES,
    PART_BYTES,
    SECRET_TTL,
    FileClaims,
    FileLinks,
    claim_key,
    created_epoch,
    download_name,
    lifetime_end,
)
from .media_api import STREAM_SLOTS, STREAM_SLOTS_PER_AGENT, Slots
from .ratelimit import ByteBudget, RateLimiter
from .records import BadFile, DrivePool, HostedAsks

log = logging.getLogger("asks.hosted.files")

NO_SUCH = "no such file, or the link has expired: read the ask again for a new link"
DELETED = "the file was deleted"
BLOCKED = "Google blocked this file"
UNAVAILABLE = "Google Drive is not available right now, try again"
BUSY = "busy, try again in a few seconds"
NO_STORE = {"Cache-Control": "no-store"}
EXPIRED = "expired"
BEING_SENT = "being sent"
SENT = "the file was deleted after it was sent"
# The forms served: START-END (a part) and START- (to the end). At most 19 digits a number, so int() never sees a
# longer one (Python refuses over 4300 digits: a 500); the unit is case-insensitive (RFC 9110 14.1).
RANGE = re.compile(r"bytes=(\d{1,19})-(\d{0,19})", re.ASCII | re.IGNORECASE)
PAST_END = "the range starts past the end of the file"
BAD_RANGE = "a Range served here is bytes=START-END (START <= END) or bytes=START-"
CONTENT_PATH = re.compile(r"/api/asks/\d+/files/\d+/content")


def is_content_path(path: str) -> bool:
    """The bearer route of a file's bytes (app.authenticate answers the user's own token there with 403)."""
    return CONTENT_PATH.fullmatch(path) is not None


def _now() -> float:
    return time.time()


RETRY_PAUSES = (0.2, 0.5)  # seconds between the attempts of the writes after a send (a store hiccup is short)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _refuse(status: int, detail: str, headers: dict | None = None, background: BackgroundTask | None = None):
    return JSONResponse({"detail": detail}, status_code=status, headers={**NO_STORE, **(headers or {})},
                        background=background)


def _stamp(pool: DrivePool, user: str, ask_id: int, n: int, file_id: str, answered_at: str, at: str, field: str,
           kind: str, extra: dict | None = None) -> bool:
    """Set `field` (downloaded_at or blocked_at) of answer file `n` to `at` and add the event `kind` {id: n}, only if
    the ask is still answered with this answered_at, the item still has this file_id and the field is still null:
    checked again on the ask as it is when written (HostedAsks.items.mutate retries a concurrent write). True if it
    wrote; False for a closed, reopened or re-answered ask, a changed or deleted item, or a stamp already there
    (not an error: a later download writes nothing). `extra` is set on the item with it (the sent secret file's deletion)."""
    asks = HostedAsks(pool.records(user))
    try:
        who = Identity("agent", asks.get(ask_id, with_events=False)["agent"])
    except NotFound:
        return False
    wrote = []

    def plan(load):
        ask = load()
        if ask["status"] != "answered" or ask["answered_at"] != answered_at:
            return ask, None
        items = ask.get(ANSWER_FILES) or []
        found = next((i for i in items if i["id"] == n), None)
        if found is None or found["file_id"] != file_id or found[field] is not None:
            return ask, None
        wrote.append(n)
        return ask, Change({ANSWER_FILES: [{**i, field: at, **(extra or {})} if i is found else i for i in items]}, kind,
                              {"id": n}, at)

    try:
        asks.items.mutate(who, ask_id, plan)
    except NotFound:
        return False
    return bool(wrote)


def mark_downloaded(pool: DrivePool, user: str, ask_id: int, n: int, file_id: str, answered_at: str, at: str) -> bool:
    """Write `downloaded_at` of item `n` (and the `file_downloaded` event) once: see _stamp. True if it wrote."""
    return _stamp(pool, user, ask_id, n, file_id, answered_at, at, "downloaded_at", "file_downloaded")


def mark_blocked(pool: DrivePool, user: str, ask_id: int, n: int, file_id: str, answered_at: str, at: str) -> bool:
    """Write `blocked_at` of item `n` (and a `file_blocked` event) once, as mark_downloaded does."""
    return _stamp(pool, user, ask_id, n, file_id, answered_at, at, "blocked_at", "file_blocked")


def mark_sent(pool: DrivePool, user: str, ask_id: int, n: int, file_id: str, answered_at: str, at: str,
              deleted: bool) -> bool:
    """A secret file has been sent: write downloaded_at, and with `deleted` (it is gone from Drive) deleted_at,
    deleted_reason "sent" and file_id null, with the file_downloaded event, once and under the guards of _stamp (the
    ask still answered with this answered_at, the item still naming this file). True if it wrote."""
    extra = {"deleted_at": at, "deleted_reason": "sent", "file_id": None} if deleted else None
    return _stamp(pool, user, ask_id, n, file_id, answered_at, at, "downloaded_at", "file_downloaded", extra)


class StreamAborted(Exception):
    """The Drive stream did not match the declared length, or broke: the response is cut (no message of the file)."""


class _Blocked(Exception):
    """stream_media's DriveBlocked, kept out of UserRecords.call's DriveError handling."""


def _open(drive, file_id: str, start: int, end: int | None):
    try:
        return drive.stream_media(file_id, start, end)
    except DriveBlocked:
        raise _Blocked from None


class _Streamed(StreamingResponse):
    """The download (the whole file, or one part of it: `length` is this response's own). It closes the Drive stream
    and gives the stream slot back when the response ends however it ends (a body that breaks, a client that went
    away), and calls `done` (the downloaded_at write; None for a part that does not end at the file's last byte) only
    after the whole of it has gone out and matched its length."""

    def __init__(self, stream, length: int, status: int, headers: dict, release: Callable[[], None],
                 done: Callable[[], None] | None, ids: str):
        self.stream, self.length, self.release, self.done, self.ids = stream, length, release, done, ids
        self.complete = False
        super().__init__(self._body(), status_code=status, headers=headers)

    async def _body(self):
        chunks, sent = iter(self.stream), 0
        while True:
            try:
                chunk = await anyio.to_thread.run_sync(next, chunks, None)
            except DriveError as exc:  # mid-body (the file was deleted, Drive broke): cut the response
                log.warning("hosted: download of %s broke off after %d bytes: %s", self.ids, sent, type(exc).__name__)
                raise StreamAborted("Drive stream broke") from None
            if chunk is None:
                break
            room = self.length - sent
            if len(chunk) > room:  # Drive sends more than `bytes` (the file was replaced): what fits, then abort
                if room:
                    yield chunk[:room]
                log.warning("hosted: download of %s: Drive sent more than %d bytes: aborted", self.ids, self.length)
                raise StreamAborted("Drive stream longer than bytes")
            sent += len(chunk)
            yield chunk
        if sent != self.length:
            log.warning("hosted: download of %s: Drive sent %d of %d bytes: aborted", self.ids, sent, self.length)
            raise StreamAborted("Drive stream shorter than bytes")
        self.complete = True

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:  # the slot is free before the stamp is written
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._end)
        if self.complete and self.done is not None:
            try:
                await anyio.to_thread.run_sync(self.done)
            except Exception as exc:  # the file went out; a stamp that did not get written is not an error
                log.warning("hosted: could not write downloaded_at of %s: %s", self.ids, type(exc).__name__)

    def _end(self) -> None:
        release, self.release = self.release, None
        self.stream.close()
        if release is not None:
            release()


def _size(meta: dict) -> int | None:
    try:
        return int(meta.get("size"))
    except (TypeError, ValueError):
        return None


@dataclass
class Target:
    """What _check found: the ask, its item `n` and the checked Drive file; for a normal file asked to be streamed,
    `span` = _span's (start, end, ask_end)."""
    ask: dict
    item: dict
    n: int
    records: object
    ids: str
    span: tuple | None = None

    @property
    def secret(self) -> bool:
        return self.item["secret"]


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _check(pool: DrivePool, user: str, ask_id: int, n: int, slug: str, expect: tuple[str, str] | None, secret_ok: bool,
           sent_detail: str | None = None, streams: bool = False, range_header: str | None = None):
    """The checks both routes share, from the ask to Drive's word on the file: a Target, or the refusal (a response).
    `expect` = (answered_at, file_id) the link carries (None on the bearer route: the item is the current one); a
    secret item is the link route's 404 (`secret_ok` False). `sent_detail` (the MCP tool's) is the 410 for an item
    that was sent and deleted, in place of "the file was deleted". With `streams` (the two routes), a normal file's
    span is decided from the item's `bytes` and `range_header` before Drive is asked anything about the file: its 409
    or 416 needs no Drive call."""
    records = pool.records(user)
    try:
        ask = HostedAsks(records).get(ask_id, with_events=False)
    except (NotFound, BadFile):  # a broken ask file: retrying won't help
        return _refuse(404, NO_SUCH)
    except AskError:
        return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
    item = next((i for i in ask.get(ANSWER_FILES) or [] if i["id"] == n), None)
    if (ask["agent"] != slug or item is None or (expect is not None and ask["answered_at"] != expect[0])
            or (item["secret"] and not secret_ok)):  # reopened, answered again, not this ask's, or a file with no link
        return _refuse(404, NO_SUCH)
    if item["file_id"] is None or ask["status"] == "closed":  # deleted, or went with the close
        return _refuse(410, sent_detail if sent_detail and item["deleted_reason"] == "sent" else DELETED)
    if expect is not None and item["file_id"] != expect[1]:
        return _refuse(404, NO_SUCH)
    if item["secret"] and item["downloaded_at"] is not None:
        # sent already, and the delete after it failed (the sweep retries it); a re-answer that kept the item has a
        # new claim key, so the claim alone can't say so
        return _refuse(410, sent_detail or SENT)
    if item["blocked_at"] is not None:  # Drive refused it before: no Drive call, slot or charge
        return _refuse(410, BLOCKED)
    span = None
    if streams and not item["secret"]:  # what the response may carry is known from `bytes` alone
        span = _span(range_header, item["bytes"])
        if not isinstance(span, tuple):
            return span
    born = None
    if item["secret"]:  # the route refuses by itself past 24 h (checked below against Drive's createdTime)
        try:
            born = min(_epoch(item["uploaded_at"]), _epoch(ask["answered_at"]))
        except (TypeError, ValueError):
            return _refuse(404, NO_SUCH)
    file_id, length = item["file_id"], item["bytes"]
    ids = f"ask #{ask['id']} item {n}"
    try:
        meta = records.call(records.drive.get_meta, file_id)
    except (AskError, DriveError):
        return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
    if meta is None:
        return _refuse(410, DELETED)
    props, size = meta.get("appProperties") or {}, _size(meta)
    cap = MAX_SECRET_BYTES if item["secret"] else MAX_FILE_BYTES
    if (props.get("kind") != "file" or props.get("ask_id") != str(ask["id"]) or props.get("file") != str(n)
            or size != length or size > cap):
        log.warning("hosted: the Drive file named by %s is not its answer file: not sent", ids)
        return _refuse(404, NO_SUCH)
    if born is not None and _now() >= lifetime_end(born, created_epoch(meta), SECRET_TTL):
        return _refuse(410, EXPIRED)  # the sweep deletes it
    return Target(ask, item, n, records, ids, span)


def _headers(ask_id: int, n: int, item: dict, length: int) -> dict:
    ascii_name, starred = download_name(ask_id, n, item["name"])
    return {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(length),
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*={starred}',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }


def _span(range_header: str | None, length: int):
    """What a normal file's response carries (the module text): (start, end, ask_end), the first and last byte sent
    and the `end` to ask Drive for (None: an open range, or the whole file when start is 0); or the refusal (a
    response, before any slot, charge or Drive call). A Range in another unit than bytes is ignored (RFC 9110 14.2)."""
    header = (range_header or "").strip()
    if header.partition("=")[0].lower() != "bytes":
        header = ""
    if not header:
        if length > PART_BYTES:
            return _refuse(409, f"this file is {length} bytes, more than one response can carry: fetch it in parts "
                                f"with Range: bytes=START-END, at most {PART_BYTES} bytes each (askhooman files does "
                                "this)")
        return 0, length - 1, None
    unsatisfiable = {"Content-Range": f"bytes */{length}"}
    found = RANGE.fullmatch(header)
    if found is None or (found.group(2) and int(found.group(2)) < int(found.group(1))):
        return _refuse(416, BAD_RANGE, unsatisfiable)
    start = int(found.group(1))
    if start >= length:
        return _refuse(416, PAST_END, unsatisfiable)
    closed = bool(found.group(2))
    end = min(int(found.group(2)), length - 1) if closed else length - 1  # an end past the file: its last byte
    if end - start + 1 > PART_BYTES:
        return _refuse(416, f"a part can be at most {PART_BYTES} bytes: ask for Range: bytes=START-END with "
                            f"END-START+1 <= {PART_BYTES}", unsatisfiable)
    return start, end, end if closed else None


def _stream(app: FastAPI, pool: DrivePool, user: str, who: str, target: Target, answered_at: str):
    """A normal file: its span (decided in _check: the whole file, or a part), a stream slot, Drive opened, the byte
    budget charged with the span, then streamed (see the module text)."""
    ask, item, n, records, ids = target.ask, target.item, target.n, target.records, target.ids
    file_id, length = item["file_id"], item["bytes"]
    start, end, ask_end = target.span
    size = end - start + 1
    whole = start == 0 and ask_end is None  # 200 the whole file; anything else is a 206 with its Content-Range
    slots: Slots = app.state.stream_slots
    if not slots.take(1, who):  # the instance's four, at most two of them this agent's
        return _refuse(503, BUSY, {"Retry-After": "5"})
    handed_over = False
    try:
        try:
            stream = records.call(_open, records.drive, file_id, start, ask_end)
        except _Blocked:
            log.warning("hosted: Drive refused to serve the file of %s: blocked", ids)
            return _refuse(410, BLOCKED, background=BackgroundTask(
                mark_blocked, pool, user, ask["id"], n, file_id, answered_at, utc_now()))
        except DriveNotFound:
            return _refuse(410, DELETED)
        except (AskError, DriveError):
            return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
        budget: ByteBudget = app.state.file_bytes
        if not budget.charge(who, size):  # this response's bytes, once Drive has opened; a refusal records nothing
            stream.close()
            return _refuse(429, "too many bytes downloaded: at most 1 GiB an hour per agent",
                           {"Retry-After": str(int(budget.retry_after()))})
        headers = _headers(ask["id"], n, item, size)
        if not whole:
            headers["Content-Range"] = f"bytes {start}-{end}/{length}"
        # only a response that ends at the file's last byte (the whole file, the last part) marks it downloaded
        done = ((lambda: mark_downloaded(pool, user, ask["id"], n, file_id, answered_at, utc_now()))
                if end == length - 1 else None)
        response = _Streamed(stream, size, 200 if whole else 206, headers, lambda: slots.give(1, who), done, ids)
        handed_over = True  # the response gives the slot back and closes the stream when it ends
        return response
    finally:
        if not handed_over:
            slots.give(1, who)


SECRET_CHUNK = 64 * 1024  # bytes per message of a secret file's body


class _Once(Response):
    """The secret file's response: the whole body is in memory and checked before the first byte, and goes out in
    SECRET_CHUNK messages. It listens to `receive` for http.disconnect while it
    sends, and calls `finish(sent)` when the response is over however it ended: `sent` is True only if the final
    message went out without an error and no http.disconnect had been received before it (a proxy may still swallow
    the end: the honest residue of SPEC 7.4)."""

    def __init__(self, content: bytes, headers: dict, finish: Callable[[bool], None]):
        super().__init__(content, status_code=200, headers=headers)
        self.finish = finish

    async def _send_in_chunks(self, send) -> None:
        """The start, then the body in SECRET_CHUNK messages (more_body until the last): each `send` returns only when
        the server has taken the chunk, so the final message goes out after back-pressure has drained. The
        Content-Length header is the whole body's, as always."""
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        body = memoryview(self.body)
        for at in range(0, len(body), SECRET_CHUNK):
            await send({"type": "http.response.body", "body": bytes(body[at:at + SECRET_CHUNK]),
                        "more_body": at + SECRET_CHUNK < len(body)})

    async def __call__(self, scope, receive, send):
        gone, sent = False, False

        async def watch():
            nonlocal gone
            while (await receive())["type"] != "http.disconnect":
                pass
            gone = True

        async def guarded(message):
            nonlocal sent
            await anyio.lowlevel.checkpoint()  # a disconnect that is already there is seen before the last byte goes
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                sent = not gone
        try:
            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(watch)
                    await self._send_in_chunks(guarded)
                    tasks.cancel_scope.cancel()
            except BaseExceptionGroup as group:  # the task group wraps the one real error: raise that
                if len(group.exceptions) == 1:
                    raise group.exceptions[0] from None
                raise
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.finish, sent)


def _read_whole(drive, file_id: str) -> bytes | None:
    """A secret file's bytes, at most MAX_SECRET_BYTES; None for one that is larger (or came compressed)."""
    try:
        return drive.download_bytes(file_id, MAX_SECRET_BYTES)
    except DriveBadFile:
        return None
    except DriveError as exc:
        if exc.status == 403 and exc.reason in BLOCK_REASONS:  # Google won't serve this file, for good
            raise _Blocked from None
        raise


@dataclass
class Refusal:
    """Why a secret file was not handed over: the route's response, the MCP tool's error text. `after` runs once the
    refusal has been given (a Drive 403's blocked_at stamp)."""
    status: int
    detail: str
    headers: dict | None = None
    after: Callable[[], None] | None = None

    def response(self):
        return _refuse(self.status, self.detail, self.headers, BackgroundTask(self.after) if self.after else None)


@dataclass
class Taken:
    """A secret file claimed and read whole (and size-checked): `raw`, and `finish(sent)` to call once it has been
    handed over (True) or not (False): the slot given back, then the claim released, or made done, the Drive file
    deleted and the ask written."""
    raw: bytes
    finish: Callable[[bool], None]


def take_secret(app: FastAPI, pool: DrivePool, claims: FileClaims, user: str, who: str, target: Target,
                cap: int | None = None):
    """The one send sequence of a secret file, shared by the bearer route (_Once) and the MCP tool: claim, slot, the
    whole file read and checked, the byte budget; a Refusal, or a Taken whose `finish` the caller MUST call. `cap` (the
    tool's 16 KiB) refuses a larger file before the claim is taken."""
    ask, item, n, records, ids = target.ask, target.item, target.n, target.records, target.ids
    file_id, length, answered_at = item["file_id"], item["bytes"], ask["answered_at"]
    if cap is not None and length > cap:
        return Refusal(413, f"too big for MCP — fetch it with `askhooman files {ask['id']} --secret`")
    key = claim_key(user, ask["id"], file_id)
    try:
        state, owner = claims.take(key, user, ask["id"], _now())
    except Exception as exc:  # the metadata store is down
        log.warning("claim of %s failed: %s", ids, type(exc).__name__)
        return Refusal(503, "temporarily unavailable, try again", {"Retry-After": "5"})
    if state == "done":
        return Refusal(410, SENT)
    if state == "held":
        return Refusal(409, BEING_SENT)
    slots: Slots = app.state.stream_slots
    taken = slots.take(1, who)
    handed_over = False
    try:
        if not taken:
            return Refusal(503, BUSY, {"Retry-After": "5"})
        try:
            raw = records.call(_read_whole, records.drive, file_id)
        except _Blocked:
            log.warning("hosted: Drive refused to serve the file of %s: blocked", ids)
            return Refusal(410, BLOCKED, after=lambda: mark_blocked(pool, user, ask["id"], n, file_id, answered_at,
                                                                      utc_now()))
        except DriveNotFound:
            return Refusal(410, DELETED)
        except (AskError, DriveError):
            return Refusal(503, UNAVAILABLE, {"Retry-After": "30"})
        if raw is None or len(raw) != length:  # before the first byte: what Drive holds is not the item's file
            log.warning("hosted: the Drive file of %s does not match its item: not sent", ids)
            return Refusal(404, NO_SUCH)
        budget: ByteBudget = app.state.file_bytes
        if not budget.charge(who, length):
            return Refusal(429, "too many bytes downloaded: at most 1 GiB an hour per agent",
                           {"Retry-After": str(int(budget.retry_after()))})

        def finish(sent: bool) -> None:
            slots.give(1, who)  # free before the writes
            try:
                if not sent:
                    claims.release(key, owner)
                    return
                for attempt in (1, 2, 3):  # the bytes are out: a claim left held would let a second send happen
                    try:
                        claims.done(key, owner, user, ask["id"], _now())  # before the delete: nothing can read it from here on
                        break
                    except Exception as exc:
                        log.warning("hosted: could not mark the claim of %s done (attempt %d): %s", ids, attempt,
                                    type(exc).__name__)
                        if attempt < 3:
                            _pause(RETRY_PAUSES[attempt - 1])
                # whether or not it got written, the file goes: the next request finds no file (410), never the bytes
                gone = HostedAsks(records)._delete_answer_file(ask["id"], n, file_id, "sent")
                for attempt in (1, 2, 3):  # the ask says «sent» once this is written (the claim is what stopped a resend)
                    try:
                        mark_sent(pool, user, ask["id"], n, file_id, answered_at, utc_now(), gone)
                        break
                    except Exception as exc:
                        log.warning("hosted: could not write the send of %s (attempt %d): %s", ids, attempt,
                                    type(exc).__name__)
                        if attempt < 3:
                            _pause(RETRY_PAUSES[attempt - 1])
            except Exception as exc:  # the file went out (or the claim is left to its lease): ids only
                log.warning("hosted: could not finish the secret file of %s (left to the sweep): %s", ids,
                            type(exc).__name__)

        handed_over = True  # the caller ends the claim and gives the slot back
        return Taken(raw, finish)
    finally:
        if not handed_over:
            if taken:
                slots.give(1, who)
            try:
                claims.release(key, owner)
            except Exception as exc:
                log.warning("hosted: could not release the claim of %s (its lease runs out): %s", ids,
                            type(exc).__name__)


def _secret(app: FastAPI, pool: DrivePool, claims: FileClaims, user: str, who: str, target: Target):
    """A secret file, once (the module text): take_secret, then one response."""
    got = take_secret(app, pool, claims, user, who, target)
    if isinstance(got, Refusal):
        return got.response()
    ask, item = target.ask, target.item
    return _Once(got.raw, _headers(ask["id"], target.n, item, item["bytes"]), got.finish)


def file_download(app: FastAPI, links: FileLinks, accounts: Accounts, pool: DrivePool, limiter: RateLimiter):
    """download(token, range_header) -> a Response, or None if `token` is not a file link (the voice route's 404).
    The stream slots and the byte budget are read from app.state at each request."""

    def download(token: str, range_header: str | None = None):
        sealed = links.open(token)
        if sealed is None:
            return None
        user, agent_id, n = sealed["user"], sealed["agent"], sealed["n"]
        who = f"{user}:{agent_id}"
        if not limiter.allow("files", who):  # per agent: one link can't block the user's others
            return _refuse(429, "too many requests for files: at most 20 a minute per agent",
                           {"Retry-After": str(limiter.retry_after("files"))})
        try:
            agent = accounts.own_agent(user, agent_id)
        except Exception as exc:  # the metadata store is down
            log.warning("file link check failed: %s", type(exc).__name__)
            return _refuse(503, "temporarily unavailable, try again", {"Retry-After": "5"})
        if agent is None:  # revoked, another user's, or no longer there
            return _refuse(404, NO_SUCH)
        target = _check(pool, user, sealed["ask_id"], n, agent["slug"], (sealed["answered_at"], sealed["file_id"]),
                        False, streams=True, range_header=range_header)
        if not isinstance(target, Target):
            return target
        return _stream(app, pool, user, who, target, sealed["answered_at"])

    return download


# ask_secret_file: what goes inline. MCP clients cap a tool result (Claude Code about 25k tokens): a bigger file would
# be marked sent and deleted while the client drops the result, so a bigger one goes through the bearer route.
MCP_MAX_BYTES = 16 * 1024
ALREADY_SENT = "already sent — if you did not receive it, ask your user to send it again"
HAS_URL = "this file has a url — download it with the url"


class FileRefused(AskError):
    """ask_secret_file's refusal: the route's status and text, no label (the MCP tool error is the text alone)."""
    label = ""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status_code = status


def secret_file_tool(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter, claims: FileClaims):
    """fetch(identity, ask_id, n) -> {name, mime, bytes, content_base64} for the MCP tool ask_secret_file (SPEC 7.8):
    the bearer route's checks in the same order (_check) and the same send (take_secret), only for a secret file of
    at most 16 KiB. MCP has no disconnect signal: the file counts as handed over once the result is built and returned
    to the MCP layer (the route's own approximation at its final send). Raises FileRefused with the route's text."""

    def fetch(who: HostedIdentity, ask_id: int, n: int) -> dict:
        if not isinstance(who, HostedIdentity):  # the route's 403: before any claim
            raise FileRefused("only the agent that asked can fetch its answer's files", 403)
        user, key = who.user, f"{who.user}:{who.agent_id}"
        if not limiter.allow("files", key):
            raise FileRefused("too many requests for files: at most 20 a minute per agent", 429)
        try:
            agent = accounts.own_agent(user, who.agent_id)
        except Exception as exc:  # the metadata store is down
            log.warning("secret file tool check failed: %s", type(exc).__name__)
            raise Unavailable("temporarily unavailable, try again") from None
        if agent is None:  # revoked since the token was checked
            raise FileRefused(NO_SUCH, 404)
        target = _check(pool, user, ask_id, n, agent["slug"], None, True, ALREADY_SENT)
        if not isinstance(target, Target):
            raise FileRefused(json.loads(target.body)["detail"], target.status_code)
        if not target.secret:
            raise FileRefused(HAS_URL)
        got = take_secret(app, pool, claims, user, key, target, MCP_MAX_BYTES)
        if isinstance(got, Refusal):
            if got.after is not None:
                try:
                    got.after()
                except Exception as exc:
                    log.warning("hosted: could not stamp %s: %s", target.ids, type(exc).__name__)
            raise FileRefused(ALREADY_SENT if got.detail == SENT else got.detail, got.status)
        try:
            result = {"name": target.item["name"], "mime": target.item["mime"], "bytes": len(got.raw),
                      "content_base64": base64.b64encode(got.raw).decode("ascii")}
        except BaseException:
            got.finish(False)
            raise
        got.finish(True)  # the result goes to the MCP layer now: the claim is done, the file is deleted
        return result

    return fetch


def add_content_route(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter,
                      claims: FileClaims) -> None:
    """GET /api/asks/{id}/files/{n}/content: the owner agent's bearer (RequestGuard has identified it; the user's own
    token never gets here: 403 in app.authenticate), both kinds of file, the same checks in the same order."""

    @app.get("/api/asks/{ask_id}/files/{n}/content", include_in_schema=False)
    def content(ask_id: int, n: int, request: Request, who=Depends(current_identity)):
        if not isinstance(who, HostedIdentity):
            return _refuse(403, "only the agent that asked can fetch its answer's files")
        user, key = who.user, f"{who.user}:{who.agent_id}"
        if not limiter.allow("files", key):
            return _refuse(429, "too many requests for files: at most 20 a minute per agent",
                           {"Retry-After": str(limiter.retry_after("files"))})
        try:
            agent = accounts.own_agent(user, who.agent_id)
        except Exception as exc:  # the metadata store is down
            log.warning("file route check failed: %s", type(exc).__name__)
            return _refuse(503, "temporarily unavailable, try again", {"Retry-After": "5"})
        if agent is None:  # revoked since the token was checked
            return _refuse(404, NO_SUCH)
        target = _check(pool, user, ask_id, n, agent["slug"], None, True, streams=True,
                        range_header=request.headers.get("range"))
        if not isinstance(target, Target):
            return target
        if target.secret:  # one response, no Range
            return _secret(app, pool, claims, user, key, target)
        return _stream(app, pool, user, key, target, target.ask["answered_at"])


def add_file_routes(app: FastAPI, links: FileLinks, accounts: Accounts, pool: DrivePool, limiter: RateLimiter,
                    claims: FileClaims | None = None):
    """The file downloads of the hosted app (create_hosted_app): the stream slots and the byte budget go into
    app.state (one of each per instance); adds the bearer route; returns download(token, range_header), which the
    voice route calls for a token that is not a recording's (both live on /media/v1/)."""
    app.state.stream_slots = Slots(STREAM_SLOTS, STREAM_SLOTS_PER_AGENT)
    app.state.file_bytes = ByteBudget()
    add_content_route(app, accounts, pool, limiter, claims or FileClaims(accounts.store))
    return file_download(app, links, accounts, pool, limiter)
