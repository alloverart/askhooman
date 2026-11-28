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

A task reply's normal file (SPEC 8.3 «The routes»: 7.4 with the names changed) comes on the same path, with a link
sealed under a context of its own, "taskfile" (files.TaskFileLinks): the download() add_file_routes returns tries an
answer's opener, then a task's, so a token opens as one of them at most. The same checks in the same order
(_check_task), the same parts, slots, Drive refusals and streaming, and the same budgets, shared and not doubled: one
`files` bucket and one `file_bytes` budget for an agent's answers' and replies' files alike. What differs: the task is
read with its events and must be the agent's own; its reply_files must have item `n` of the link's reply_id and
file_id (a finished task or a deleted item: 410; a secret item or anything else: 404, worded for the task,
TASK_NO_SUCH; a reopened task has only deleted items); an id no item has but one of the user's `replied` events names
(an item the user's next reply pruned; through a link, that event is the link's reply_id and names its file_id) is 410
like a deleted item, the «sent» one when that snapshot was a secret file and the history holds its file_downloaded;
Drive must mark the file kind "file" with this task_id and file = n, and no ask_id; the 409 names `askhooman task
files`; the download is task-<id>-file-<n>[.<ext>]. mark_task_downloaded (mark_task_blocked) writes its stamp only if
the task is still open, the item still has this file_id and the stamp is still null, with the owner agent as the
event's actor; `updated_at` is not written, and these events are not the owner's (task_rules.OWNER_KINDS): a download
never clears reply_pending.

GET /api/tasks/{id}/files/{n}/content is the bearer route for the task's own agent (the user's own token: 403 in
app.authenticate, worded for the task), both kinds of file, with _check_task's checks: a normal file is streamed as
above, a SECRET file is sent once by the same sequence as an answer's (take_secret: one claim machine in the one
collection, the same order and retries), and task_secret_file_tool is ask_secret_file's fetch for it. What is the
holder's own comes from _writes by Target.kind: the claim (files.task_claim_key; its document names task_id, never
ask_id), the stamps (mark_task_sent: downloaded_at, deleted_at, deleted_reason "sent", file_id null, event
file_downloaded, only while the task is open and the item names the file), the Drive delete through the task's guard
(HostedTasks._delete_reply_file) and the words. A finish meanwhile deletes the claim (held or done) and the file, and
the send then writes nothing. A reply's file that was sent says so (410 "the file was deleted after it was sent", the
tool's «already sent») whether its item is still listed, finished or pruned, so the words don't change with a reply.
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
from ..task_rules import FINISHED, OPEN, REPLY_FILES
from ..tokens import Identity
from .accounts import Accounts, HostedIdentity
from .files import (
    MAX_FILE_BYTES,
    MAX_SECRET_BYTES,
    PART_BYTES,
    SECRET_TTL,
    FileClaims,
    FileLinks,
    TaskFileLinks,
    claim_key,
    created_epoch,
    download_name,
    is_user_reply,
    lifetime_end,
    task_claim_key,
)
from .media_api import STREAM_SLOTS, STREAM_SLOTS_PER_AGENT, Slots
from .ratelimit import ByteBudget, RateLimiter
from .records import BadFile, DrivePool, HostedAsks, HostedTasks

log = logging.getLogger("asks.hosted.files")

NO_SUCH = "no such file, or the link has expired: read the ask again for a new link"
TASK_NO_SUCH = "no such file, or the link has expired: read the task again for a new link"
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
CONTENT_PATH = re.compile(r"/api/(?:asks|tasks)/\d+/files/\d+/content")


def is_content_path(path: str) -> bool:
    """The bearer route of a file's bytes, an answer's or a reply's (app.authenticate answers the user's own token
    there with 403)."""
    return CONTENT_PATH.fullmatch(path) is not None


def _now() -> float:
    return time.time()


RETRY_PAUSES = (0.2, 0.5)  # seconds between the attempts of the writes after a send (a store hiccup is short)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _refuse(status: int, detail: str, headers: dict | None = None, background: BackgroundTask | None = None):
    return JSONResponse({"detail": detail}, status_code=status, headers={**NO_STORE, **(headers or {})},
                        background=background)


def _stamp_item(holders: HostedAsks | HostedTasks, holder_id: int, list_field: str, current: Callable[[dict], bool],
                n: int, file_id: str, at: str, field: str, kind: str, extra: dict | None = None) -> bool:
    """Set `field` (downloaded_at or blocked_at) of item `n` of the holder's `list_field` to `at` and add the event
    `kind` {id: n} (actor: the holder's agent), only if `current(holder)` (the ask or task is still the one the file
    was fetched from), the item still has this file_id and the field is still null: checked again on the holder as it
    is when written (DriveCollection.mutate retries a concurrent write). Nothing else is written (not updated_at). True
    if it wrote; False when the holder is not current, the item changed or went, or the stamp is already there (not an
    error: a later download writes nothing). `extra` is set on the item with it (the sent secret file's deletion)."""
    try:
        who = Identity("agent", holders.get(holder_id, with_events=False)["agent"])
    except NotFound:
        return False
    wrote = []

    def plan(load):
        holder = load()
        if not current(holder):
            return holder, None
        items = holder.get(list_field) or []
        found = next((i for i in items if i["id"] == n), None)
        if found is None or found["file_id"] != file_id or found[field] is not None:
            return holder, None
        wrote.append(n)
        return holder, Change({list_field: [{**i, field: at, **(extra or {})} if i is found else i for i in items]},
                              kind, {"id": n}, at)

    try:
        holders.items.mutate(who, holder_id, plan)
    except NotFound:
        return False
    return bool(wrote)


def _stamp(pool: DrivePool, user: str, ask_id: int, n: int, file_id: str, answered_at: str, at: str, field: str,
           kind: str, extra: dict | None = None) -> bool:
    """_stamp_item for answer file `n`: only while the ask is still answered with this answered_at (a closed,
    reopened or re-answered ask is skipped)."""
    return _stamp_item(HostedAsks(pool.records(user)), ask_id, ANSWER_FILES,
                       lambda ask: ask["status"] == "answered" and ask["answered_at"] == answered_at,
                       n, file_id, at, field, kind, extra)


def _task_stamp(pool: DrivePool, user: str, task_id: int, n: int, file_id: str, at: str, field: str, kind: str,
                extra: dict | None = None) -> bool:
    """_stamp_item for item `n` of a task's reply_files (SPEC 8.3 «downloaded_at and blocked_at»): only while the task
    is open (a finished task's items are stamped finished, a reopened one's stay so). A reply is never replaced, so
    the item's file_id alone says it is the same file."""
    return _stamp_item(HostedTasks(pool.records(user)), task_id, REPLY_FILES, lambda task: task["status"] in OPEN,
                       n, file_id, at, field, kind, extra)


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


def mark_task_downloaded(pool: DrivePool, user: str, task_id: int, n: int, file_id: str, at: str) -> bool:
    """Write `downloaded_at` of item `n` of the task's reply_files (and the `file_downloaded` event) once: see
    _task_stamp. The event is not the owner's (task_rules.OWNER_KINDS): reply_pending stays. True if it wrote."""
    return _task_stamp(pool, user, task_id, n, file_id, at, "downloaded_at", "file_downloaded")


def mark_task_blocked(pool: DrivePool, user: str, task_id: int, n: int, file_id: str, at: str) -> bool:
    """Write `blocked_at` of item `n` of the task's reply_files (and a `file_blocked` event) once, as
    mark_task_downloaded does."""
    return _task_stamp(pool, user, task_id, n, file_id, at, "blocked_at", "file_blocked")


def mark_task_sent(pool: DrivePool, user: str, task_id: int, n: int, file_id: str, at: str, deleted: bool) -> bool:
    """A reply's secret file has been sent: mark_sent for item `n` of the task's reply_files, under the guards of
    mark_task_downloaded (the task still open, the item still naming this file, no downloaded_at yet). True if it
    wrote."""
    extra = {"deleted_at": at, "deleted_reason": "sent", "file_id": None} if deleted else None
    return _task_stamp(pool, user, task_id, n, file_id, at, "downloaded_at", "file_downloaded", extra)


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
    """What _check (_check_task) found: the holder (the ask; or the task, with its events), its item `n` and the
    checked Drive file; `kind` says which ("ask" | "task"); for a normal file asked to be streamed, `span` = _span's
    (start, end, ask_end)."""
    holder: dict
    item: dict
    n: int
    records: object
    ids: str
    span: tuple | None = None
    kind: str = "ask"

    @property
    def secret(self) -> bool:
        return self.item["secret"]


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _confirm(records, item: dict, ids: str, own: Callable[[dict], bool], born: float | None, no_such: str,
             what: str):
    """Drive's word on the file `item` names, for both kinds: gone is 410; its appProperties must say it is the item's
    own (`own`), and its size must be `bytes` and within the cap (MAX_SECRET_BYTES for a secret file), else 404
    `no_such`, logged by ids; a secret file past its lifetime (from `born`, bounded by Drive's createdTime) is 410
    expired. None if the file may be sent; Drive unreachable is 503."""
    try:
        meta = records.call(records.drive.get_meta, item["file_id"])
    except (AskError, DriveError):
        return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
    if meta is None:
        return _refuse(410, DELETED)
    size = _size(meta)
    cap = MAX_SECRET_BYTES if item["secret"] else MAX_FILE_BYTES
    if not own(meta.get("appProperties") or {}) or size != item["bytes"] or size > cap:
        log.warning("hosted: the Drive file named by %s is not its %s: not sent", ids, what)
        return _refuse(404, no_such)
    if born is not None and _now() >= lifetime_end(born, created_epoch(meta), SECRET_TTL):
        return _refuse(410, EXPIRED)  # the sweep deletes it
    return None


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
    ids = f"ask #{ask['id']} item {n}"
    refused = _confirm(records, item, ids, lambda props: (
        props.get("kind") == "file" and props.get("ask_id") == str(ask["id"]) and props.get("file") == str(n)),
        born, NO_SUCH, "answer file")
    if refused is not None:
        return refused
    return Target(ask, item, n, records, ids, span)


def _is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pruned(task: dict, n: int, expect: tuple[int, str] | None, sent_detail: str | None):
    """No item of the task has id `n` (SPEC 8.3, «An id whose item was pruned»): an id that never was (404), or the
    item of a file that is gone, which the user's next reply pruned from reply_files (410, like a deleted item). Told
    apart by the snapshot in the `files` of one of the user's `replied` events (ids are never reused); through a link
    it must be the event `reply_id`, its snapshot naming this file_id. «Sent» (`sent_detail`, else SENT) when the
    snapshot was a secret file and the history holds its `file_downloaded`, so «a second call says it was already
    sent» holds after the prune."""
    events = task.get("events") or []
    snapshot = None
    for event in events:
        if not is_user_reply(event):  # the replies an item can name (files.clean_reply_files): no other
            continue
        if expect is not None and event["id"] != expect[0]:
            continue
        payload = event.get("payload")
        files = payload.get("files") if isinstance(payload, dict) else None
        for entry in files if isinstance(files, list) else []:
            if (isinstance(entry, dict) and _is_id(entry.get("id")) and entry["id"] == n
                    and (expect is None or entry.get("file_id") == expect[1])):
                snapshot = entry
    if snapshot is None:
        return _refuse(404, TASK_NO_SUCH)
    sent = any(e.get("kind") == "file_downloaded" and isinstance(e.get("payload"), dict)
               and _is_id(e["payload"].get("id")) and e["payload"]["id"] == n for e in events)
    if snapshot.get("secret") is True and sent:
        return _refuse(410, sent_detail or SENT)
    return _refuse(410, DELETED)


def _check_task(pool: DrivePool, user: str, task_id: int, n: int, slug: str, expect: tuple[int, str] | None,
                secret_ok: bool, sent_detail: str | None = None, streams: bool = False,
                range_header: str | None = None):
    """_check for item `n` of a task's reply_files, in SPEC 8.3's order (the module text): a Target(kind="task"), or
    the refusal (a response), every 404 TASK_NO_SUCH. `expect` = (reply_id, file_id) the link carries (None on the
    bearer route); `secret_ok`, `streams` and `range_header` are _check's. A file that was sent is 410 `sent_detail`
    (the MCP tool's), else SENT, whether its item is deleted, finished or pruned (an answer's deleted item says
    DELETED to the route). The task is read with its events, as checked (a task file that is not one: 404; Drive
    unreachable: 503)."""
    records = pool.records(user)
    try:
        task = HostedTasks(records).get(task_id)  # with its events: a pruned id is told apart by them
    except (NotFound, BadFile):  # a broken task file: retrying won't help
        return _refuse(404, TASK_NO_SUCH)
    except AskError:
        return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
    if task["agent"] != slug:
        return _refuse(404, TASK_NO_SUCH)
    item = next((i for i in task.get(REPLY_FILES) or [] if i["id"] == n), None)
    if item is None:  # never was, or pruned by a later reply
        return _pruned(task, n, expect, sent_detail)
    if (expect is not None and item["reply_id"] != expect[0]) or (item["secret"] and not secret_ok):
        return _refuse(404, TASK_NO_SUCH)  # another reply's file, or a file with no link
    # Sent: deleted as sent, or a secret file with downloaded_at (the delete after the send failed, and the finish may
    # have deleted it since). Its 410 says so with or without the item, as _pruned does once a reply pruned it.
    sent = item["deleted_reason"] == "sent" or (item["secret"] and item["downloaded_at"] is not None)
    if item["file_id"] is None or task["status"] in FINISHED:  # deleted, or went with the finish
        return _refuse(410, (sent_detail or SENT) if sent else DELETED)
    if expect is not None and item["file_id"] != expect[1]:
        return _refuse(404, TASK_NO_SUCH)
    if sent:  # a secret file sent already, and the delete after it failed (the sweep retries it)
        return _refuse(410, sent_detail or SENT)
    if item["blocked_at"] is not None:  # Drive refused it before: no Drive call, slot or charge
        return _refuse(410, BLOCKED)
    span = None
    if streams and not item["secret"]:  # what the response may carry is known from `bytes` alone
        span = _span(range_header, item["bytes"], "askhooman task files")
        if not isinstance(span, tuple):
            return span
    born = None
    if item["secret"]:  # the route refuses by itself past 24 h (checked against Drive's createdTime)
        try:
            born = min(_epoch(item["uploaded_at"]), _epoch(item["replied_at"]))
        except (TypeError, ValueError):
            return _refuse(404, TASK_NO_SUCH)
    ids = f"task #{task['id']} item {n}"
    refused = _confirm(records, item, ids, lambda props: (
        props.get("kind") == "file" and props.get("task_id") == str(task["id"]) and props.get("file") == str(n)
        and "ask_id" not in props),  # a file with an ask's mark is never a task's (HostedTasks._delete_reply_file)
        born, TASK_NO_SUCH, "reply file")
    if refused is not None:
        return refused
    return Target(task, item, n, records, ids, span, "task")


def _headers(holder_id: int, n: int, item: dict, length: int, holder: str = "ask") -> dict:
    ascii_name, starred = download_name(holder_id, n, item["name"], holder)
    return {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(length),
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*={starred}',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }


def _span(range_header: str | None, length: int, command: str = "askhooman files"):
    """What a normal file's response carries (the module text): (start, end, ask_end), the first and last byte sent
    and the `end` to ask Drive for (None: an open range, or the whole file when start is 0); or the refusal (a
    response, before any slot, charge or Drive call). A Range in another unit than bytes is ignored (RFC 9110 14.2).
    The 409 names `command`, the CLI command that fetches this kind of file in parts."""
    header = (range_header or "").strip()
    if header.partition("=")[0].lower() != "bytes":
        header = ""
    if not header:
        if length > PART_BYTES:
            return _refuse(409, f"this file is {length} bytes, more than one response can carry: fetch it in parts "
                                f"with Range: bytes=START-END, at most {PART_BYTES} bytes each ({command} does "
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


@dataclass
class Writes:
    """What fetching target's file does to its holder, by its kind (_writes): the stamps, each called with the
    stamp's time and True if it wrote (`sent(at, deleted)`: a secret file handed over, and gone from Drive or not);
    `delete(reason)`, the Drive file deleted through the holder's own guard, True if it is gone; and for a secret
    file, its claim's key and the words of the kind (the 404, the MCP tool's «too big»)."""
    downloaded: Callable[[str], bool]
    blocked: Callable[[str], bool]
    sent: Callable[[str, bool], bool]
    delete: Callable[[str], bool]
    claim_key: str
    too_big: str
    no_such: str


def _writes(pool: DrivePool, user: str, target: Target, answered_at: str | None = None) -> Writes:
    """The holder's own writes for target.kind, "ask" or "task" (anything else is a bug: ValueError). An ask's item
    is stamped under `answered_at` (the answer the link names; by default the ask's own, on the bearer route and in
    the tool), a task's while the task is open (mark_task_downloaded)."""
    holder_id, n, file_id, records = target.holder["id"], target.n, target.item["file_id"], target.records
    if target.kind == "task":
        return Writes(lambda at: mark_task_downloaded(pool, user, holder_id, n, file_id, at),
                      lambda at: mark_task_blocked(pool, user, holder_id, n, file_id, at),
                      lambda at, deleted: mark_task_sent(pool, user, holder_id, n, file_id, at, deleted),
                      lambda reason: HostedTasks(records)._delete_reply_file(holder_id, n, file_id, reason),
                      task_claim_key(user, holder_id, file_id),
                      f"too big for MCP — fetch it with `askhooman task files {holder_id} --secret`", TASK_NO_SUCH)
    if target.kind != "ask":
        raise ValueError(f"no writes for a holder of kind {target.kind!r}")
    if answered_at is None:
        answered_at = target.holder["answered_at"]
    return Writes(lambda at: mark_downloaded(pool, user, holder_id, n, file_id, answered_at, at),
                  lambda at: mark_blocked(pool, user, holder_id, n, file_id, answered_at, at),
                  lambda at, deleted: mark_sent(pool, user, holder_id, n, file_id, answered_at, at, deleted),
                  lambda reason: HostedAsks(records)._delete_answer_file(holder_id, n, file_id, reason),
                  claim_key(user, holder_id, file_id),
                  f"too big for MCP — fetch it with `askhooman files {holder_id} --secret`", NO_SUCH)


def _stream(app: FastAPI, pool: DrivePool, user: str, who: str, target: Target, answered_at: str | None = None):
    """A normal file: its span (decided in _check / _check_task: the whole file, or a part), a stream slot, Drive
    opened, the byte budget charged with the span, then streamed (see the module text). `answered_at` is _writes'."""
    holder, item, n, records, ids = target.holder, target.item, target.n, target.records, target.ids
    file_id, length = item["file_id"], item["bytes"]
    writes = _writes(pool, user, target, answered_at)
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
            return _refuse(410, BLOCKED, background=BackgroundTask(writes.blocked, utc_now()))
        except DriveNotFound:
            return _refuse(410, DELETED)
        except (AskError, DriveError):
            return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
        budget: ByteBudget = app.state.file_bytes
        if not budget.charge(who, size):  # this response's bytes, once Drive has opened; a refusal records nothing
            stream.close()
            return _refuse(429, "too many bytes downloaded: at most 1 GiB an hour per agent",
                           {"Retry-After": str(int(budget.retry_after()))})
        headers = _headers(holder["id"], n, item, size, target.kind)
        if not whole:
            headers["Content-Range"] = f"bytes {start}-{end}/{length}"
        # only a response that ends at the file's last byte (the whole file, the last part) marks it downloaded
        done = (lambda: writes.downloaded(utc_now())) if end == length - 1 else None
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
    deleted and the ask or task written."""
    raw: bytes
    finish: Callable[[bool], None]


def take_secret(app: FastAPI, pool: DrivePool, claims: FileClaims, user: str, who: str, target: Target,
                cap: int | None = None):
    """The one send sequence of a secret file, an answer's or a reply's, shared by the bearer routes (_Once) and the
    MCP tools: claim, slot, the whole file read and checked, the byte budget; a Refusal, or a Taken whose `finish` the
    caller MUST call. `cap` (the tools' 16 KiB) refuses a larger file before the claim is taken. Whatever is the
    holder's own (the claim's key and document, the stamps, the guarded Drive delete, the words) comes from _writes,
    by target.kind; the sequence is the same for both."""
    item, records, ids = target.item, target.records, target.ids
    holder_id, file_id, length = target.holder["id"], item["file_id"], item["bytes"]
    writes = _writes(pool, user, target)
    if cap is not None and length > cap:
        return Refusal(413, writes.too_big)
    key = writes.claim_key
    try:
        state, owner = claims.take(key, user, holder_id, _now(), holder=target.kind)
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
            return Refusal(410, BLOCKED, after=lambda: writes.blocked(utc_now()))
        except DriveNotFound:
            return Refusal(410, DELETED)
        except (AskError, DriveError):
            return Refusal(503, UNAVAILABLE, {"Retry-After": "30"})
        if raw is None or len(raw) != length:  # before the first byte: what Drive holds is not the item's file
            log.warning("hosted: the Drive file of %s does not match its item: not sent", ids)
            return Refusal(404, writes.no_such)
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
                    try:  # before the delete: nothing can read it from here on
                        claims.done(key, owner, user, holder_id, _now(), holder=target.kind)
                        break
                    except Exception as exc:
                        log.warning("hosted: could not mark the claim of %s done (attempt %d): %s", ids, attempt,
                                    type(exc).__name__)
                        if attempt < 3:
                            _pause(RETRY_PAUSES[attempt - 1])
                # whether or not it got written, the file goes: the next request finds no file (410), never the bytes
                gone = writes.delete("sent")
                for attempt in (1, 2, 3):  # the holder says «sent» once this is written (the claim stopped a resend)
                    try:
                        writes.sent(utc_now(), gone)
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
    holder, item = target.holder, target.item
    return _Once(got.raw, _headers(holder["id"], target.n, item, item["bytes"], target.kind), got.finish)


def _link_download(app: FastAPI, links: FileLinks | TaskFileLinks, accounts: Accounts, pool: DrivePool,
                   limiter: RateLimiter, no_such: str, check: Callable[[str, dict, str, str | None], object]):
    """download(token, range_header) -> a Response, or None if `links` does not open `token`. What a link route does
    whatever kind of file it names: the token, the `files` budget of its user and agent (one bucket for both kinds),
    the agent (revoked or not the user's: 404 `no_such`), then `check(user, sealed, slug, range_header)` (a Target or
    the refusal) and the stream. The stream slots and the byte budget are read from app.state at each request."""

    def download(token: str, range_header: str | None = None):
        sealed = links.open(token)
        if sealed is None:
            return None
        user, agent_id = sealed["user"], sealed["agent"]
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
            return _refuse(404, no_such)
        target = check(user, sealed, agent["slug"], range_header)
        if not isinstance(target, Target):
            return target
        # an answer's link names its answer (the stamps' guard); a reply's has no answered_at
        return _stream(app, pool, user, who, target, sealed.get("answered_at"))

    return download


def file_download(app: FastAPI, links: FileLinks, accounts: Accounts, pool: DrivePool, limiter: RateLimiter):
    """download(token, range_header) for the link to an answer's normal file (SPEC 7.4): a Response, or None if
    `token` is not one."""
    return _link_download(app, links, accounts, pool, limiter, NO_SUCH, lambda user, sealed, slug, range_header: _check(
        pool, user, sealed["ask_id"], sealed["n"], slug, (sealed["answered_at"], sealed["file_id"]), False,
        streams=True, range_header=range_header))


def task_file_download(app: FastAPI, links: TaskFileLinks, accounts: Accounts, pool: DrivePool,
                       limiter: RateLimiter):
    """download(token, range_header) for the link to a task reply's normal file (SPEC 8.3): a Response, or None if
    `token` is not one."""
    return _link_download(app, links, accounts, pool, limiter, TASK_NO_SUCH,
                          lambda user, sealed, slug, range_header: _check_task(
                              pool, user, sealed["task_id"], sealed["n"], slug, (sealed["reply_id"], sealed["file_id"]),
                              False, streams=True, range_header=range_header))


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


ASK_ONLY = "only the agent that asked can fetch its answer's files"
TASK_ONLY = "only the agent whose task it is can fetch its replies' files"


def _secret_fetch(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter, claims: FileClaims,
                  only: str, no_such: str, check: Callable):
    """fetch(identity, holder_id, n) for the MCP tool of one kind of holder: not an agent of a hosted user is 403
    `only` (before any claim), the `files` bucket, the agent (revoked: 404 `no_such`), then `check` (_check or
    _check_task) and take_secret, only for a secret file of at most 16 KiB. MCP has no disconnect signal: the file
    counts as handed over once the result is built and returned to the MCP layer (the route's own approximation at
    its final send). Raises FileRefused with the route's text, «already sent» for a file that was."""

    def fetch(who: HostedIdentity, holder_id: int, n: int) -> dict:
        if not isinstance(who, HostedIdentity):  # the route's 403: before any claim
            raise FileRefused(only, 403)
        user, key = who.user, f"{who.user}:{who.agent_id}"
        if not limiter.allow("files", key):
            raise FileRefused("too many requests for files: at most 20 a minute per agent", 429)
        try:
            agent = accounts.own_agent(user, who.agent_id)
        except Exception as exc:  # the metadata store is down
            log.warning("secret file tool check failed: %s", type(exc).__name__)
            raise Unavailable("temporarily unavailable, try again") from None
        if agent is None:  # revoked since the token was checked
            raise FileRefused(no_such, 404)
        target = check(pool, user, holder_id, n, agent["slug"], None, True, ALREADY_SENT)
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


def secret_file_tool(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter, claims: FileClaims):
    """fetch(identity, ask_id, n) -> {name, mime, bytes, content_base64} for the MCP tool ask_secret_file (SPEC 7.8):
    the bearer route's checks in the same order (_check) and the same send (take_secret), only for a secret file of
    at most 16 KiB (_secret_fetch). Raises FileRefused with the route's text."""
    return _secret_fetch(app, accounts, pool, limiter, claims, ASK_ONLY, NO_SUCH, _check)


def task_secret_file_tool(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter,
                          claims: FileClaims):
    """fetch(identity, task_id, n) -> {name, mime, bytes, content_base64} for the MCP tool task_secret_file (SPEC
    8.7): ask_secret_file for a reply's secret file, only the task's own agent, with the task route's checks
    (_check_task) and the same send and claim (take_secret). Raises FileRefused with the route's text."""
    return _secret_fetch(app, accounts, pool, limiter, claims, TASK_ONLY, TASK_NO_SUCH, _check_task)


def _content(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter, claims: FileClaims,
             only: str, no_such: str, check: Callable):
    """content(holder_id, n, request, identity) -> the response of a bearer route for one kind of holder: not an
    agent of a hosted user is 403 `only`, the `files` bucket, the agent (revoked: 404 `no_such`), `check` (_check or
    _check_task), then a secret file once (_secret) and a normal one streamed (_stream, Range included)."""

    def content(holder_id: int, n: int, request: Request, who):
        if not isinstance(who, HostedIdentity):
            return _refuse(403, only)
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
            return _refuse(404, no_such)
        target = check(pool, user, holder_id, n, agent["slug"], None, True, streams=True,
                       range_header=request.headers.get("range"))
        if not isinstance(target, Target):
            return target
        if target.secret:  # one response, no Range
            return _secret(app, pool, claims, user, key, target)
        return _stream(app, pool, user, key, target)  # an ask's stamps: under its own answered_at

    return content


def add_content_route(app: FastAPI, accounts: Accounts, pool: DrivePool, limiter: RateLimiter,
                      claims: FileClaims) -> None:
    """GET /api/asks/{id}/files/{n}/content and GET /api/tasks/{id}/files/{n}/content: the owner agent's bearer
    (RequestGuard has identified it; the user's own token never gets here: 403 in app.authenticate), both kinds of
    file, the same checks in the same order (SPEC 7.4, 8.3)."""
    answers = _content(app, accounts, pool, limiter, claims, ASK_ONLY, NO_SUCH, _check)
    replies = _content(app, accounts, pool, limiter, claims, TASK_ONLY, TASK_NO_SUCH, _check_task)

    @app.get("/api/asks/{ask_id}/files/{n}/content", include_in_schema=False)
    def content(ask_id: int, n: int, request: Request, who=Depends(current_identity)):
        return answers(ask_id, n, request, who)

    @app.get("/api/tasks/{task_id}/files/{n}/content", include_in_schema=False)
    def task_content(task_id: int, n: int, request: Request, who=Depends(current_identity)):
        return replies(task_id, n, request, who)


def add_file_routes(app: FastAPI, links: FileLinks, accounts: Accounts, pool: DrivePool, limiter: RateLimiter,
                    claims: FileClaims | None = None, task_links: TaskFileLinks | None = None):
    """The file downloads of the hosted app (create_hosted_app): the stream slots and the byte budget go into
    app.state (one of each per instance, for both kinds of file); adds the bearer route; returns
    download(token, range_header), which the voice route calls for a token that is not a recording's (all three live
    on /media/v1/). It tries the link to an answer's file, then (with `task_links`) to a task reply's: each is sealed
    under its own context, so a token opens as one at most; None for a token that is neither (the voice route's 404)."""
    app.state.stream_slots = Slots(STREAM_SLOTS, STREAM_SLOTS_PER_AGENT)
    app.state.file_bytes = ByteBudget()
    add_content_route(app, accounts, pool, limiter, claims or FileClaims(accounts.store))
    answers = file_download(app, links, accounts, pool, limiter)
    replies = task_file_download(app, task_links, accounts, pool, limiter) if task_links is not None else None

    def download(token: str, range_header: str | None = None):
        found = answers(token, range_header)
        if found is None and replies is not None:
            return replies(token, range_header)
        return found

    return download
