"""The attachment calls of the hosted bridge (SPEC v5 step 4, "REST" and "Limits"): one MediaService for the agents'
REST (add_media_routes, here) and their MCP tools (mcp_tools), so both go through the same per-instance guard.

  POST   /api/asks                       {…, attachments?}: a create with pictures (add_api_routes calls create)
  POST   /api/asks/{id}/attachments      {url | data, link?, title?, caption?, source_url?, poster_for?} -> 201 ask
  POST   /api/asks/{id}/uploads          {mime, bytes, caption?, source_url?, poster_for?} -> 201 {upload, curl}
  PUT    /api/uploads                    the file itself, with the upload token (uploads.py) -> 201 {ask_id, attachment}
  DELETE /api/asks/{id}/attachments/{n}  -> the ask

The guard (MediaGuard), per instance like every limit here:
- the `media` bucket of ratelimit.py, 60 per hour per agent: each attach, upload request and detach counts one,
  a create one per item; a call over the budget counts nothing and is refused (429);
- 4 slots of media in flight, taken without waiting (busy: 503 with Retry-After: 5): a url fetch or an upload PUT
  holds 1 slot (at most 20 MB), a create that fetches anything 2 (at most 40 MB), so an instance never holds more
  than 80 MB of media; data URLs (at most 180 KB, already in the request) and link cards hold none.

An upload request checks everything it can before a token exists (owner, open ask, the type, the size against its
kind's cap, poster rules, the per-ask count and 40 MB), so an agent learns before it sends the bytes; the PUT then
checks it all again against the bytes and the ask as it is by then (attachments.AskMedia.attach). The PUT reads
exactly the declared size into one buffer once it holds its slot: a longer body is a 413 without reading on, a
shorter one a 422. A client that sends nothing for UPLOAD_IDLE, or takes more than UPLOAD_DEADLINE for the whole
body, gets a 408 and gives the slot back (uvicorn has no body timeout of its own). A PUT has used its token up
whatever the outcome, so a busy or slow one says to request a new upload.
"""

import io
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import anyio
import anyio.to_thread
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..app import StrictBody, current_identity
from ..db import utc_now
from ..mcp_tools import IDENTITY_SCOPE_KEY
from ..rules import URL_MAX, AskError, Invalid, Unavailable, _text, plan_create
from .accounts import HostedIdentity
from .attachments import (
    AskMedia,
    Item,
    attachment_list,
    next_attachment_id,
    plan_attach,
    prepare,
    prepare_all,
)
from .media import (
    CAPTION_MAX,
    CONVERT_HINT,
    IMAGE_MAX_BYTES,
    MEDIA_TYPES,
    POSTER_MAX_BYTES,
    VIDEO_MAX_BYTES,
    VIDEO_TYPE,
    check_media,
    clean_link_url,
)
from .media_fetch import FETCH_DEADLINE
from .ratelimit import RateLimiter
from .records import ATTACHMENTS
from .uploads import UPLOAD_PATH, UploadGrant, UploadGrants

MEDIA_SLOTS = 4  # per instance
FETCH_SLOTS = 1  # a url fetch or an upload PUT: at most 20 MB
CREATE_SLOTS = 2  # an ask_create with any fetched item: at most 40 MB
UPLOAD_IDLE = 20.0  # seconds a PUT may wait for the next piece of its body
UPLOAD_DEADLINE = 120.0  # seconds for the whole body of a PUT
BUSY = "busy, try again in a few seconds"
UPLOAD_BUSY = "busy: request a new upload in a few seconds"  # the PUT's token is used up already
UPLOAD_SLOW = "upload too slow: request a new upload"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}  # an upload request's reply carries a token
_MB = 1024 * 1024


class Busy(Unavailable):
    """Every media slot of this instance is taken (503 with Retry-After over REST). No label: the MCP tool error is
    the message alone, "busy, try again in a few seconds"."""
    label = ""
    headers = {"Retry-After": "5"}


class MediaRateLimited(AskError):
    status_code = 429
    label = "rate limited"

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.headers = {"Retry-After": str(retry_after)}


class Slots:
    """Media held in flight by this instance, counted in slots: take() says no at once instead of waiting."""

    def __init__(self, size: int = MEDIA_SLOTS):
        self.size = size
        self._in_use = 0
        self._lock = threading.Lock()

    @property
    def in_use(self) -> int:
        return self._in_use

    def take(self, n: int) -> bool:
        with self._lock:
            if self._in_use + n > self.size:
                return False
            self._in_use += n
            return True

    def give(self, n: int) -> None:
        with self._lock:
            self._in_use = max(self._in_use - n, 0)


class MediaGuard:
    """The per-instance limits of attachment calls, for REST and MCP alike: the `media` bucket per agent and the
    slots. hold() takes the slots first (a busy call counts nothing), then counts `calls`."""

    def __init__(self, limiter: RateLimiter, slots: Slots):
        self.limiter = limiter
        self.slots = slots

    @contextmanager
    def hold(self, who: HostedIdentity | None, calls: int, slots: int = 0, busy: str = BUSY):
        if not self.slots.take(slots):
            raise Busy(busy)
        try:
            if calls and not self.limiter.allow("media", who.agent_id, calls):
                count = self.limiter.limits["media"][0]
                raise MediaRateLimited(f"too many attachment calls: at most {count} an hour per agent (each attach, "
                                       "upload, detach and each item of an ask_create counts)",
                                       self.limiter.retry_after("media"))
            yield
        finally:
            self.slots.give(slots)


def fetches(item) -> bool:
    """Would preparing this item fetch a url (and hold its bytes)? A data URL or a link card fetches nothing."""
    if hasattr(item, "model_dump"):
        item = item.model_dump()
    if not isinstance(item, dict):
        return False
    url = item.get("url")
    return isinstance(url, str) and bool(url.strip()) and item.get("link") is not True


def upload_reply(public_url: str, token: str, mime: str, expires_at: str) -> dict:
    """What ask_upload returns: where and how to send the file, and the ready curl command (@FILE to replace)."""
    url = f"{public_url}{UPLOAD_PATH}"
    return {"upload": {"url": url, "method": "PUT",
                       "headers": {"Authorization": f"Bearer {token}", "Content-Type": mime},
                       "expires_at": expires_at},
            "curl": f"curl -sS --fail-with-body -X PUT -H 'Authorization: Bearer {token}' -H 'Content-Type: {mime}' "
                    f"--data-binary @FILE {url}"}


def _upload_args(mime, size, caption, source_url, poster_for) -> tuple[str, int, str | None, str | None, int]:
    """ask_upload's arguments, checked before a token is issued: an allowed type, a size within its kind's cap
    (a poster: an image of at most 1 MB, with no caption or source_url of its own)."""
    mime = mime.strip().lower() if isinstance(mime, str) else None
    if mime not in MEDIA_TYPES:
        raise Invalid(f"mime must be image/png, image/jpeg, image/webp or video/mp4: {CONVERT_HINT}")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise Invalid("bytes must be the file's size in bytes: a whole number, 1 or more")
    caption = _text(caption, "caption", CAPTION_MAX)
    source_url = _text(source_url, "source_url", URL_MAX)
    if source_url is not None:
        source_url = clean_link_url(source_url, "source_url")
    if poster_for in (0, None):
        poster_for = 0
    elif isinstance(poster_for, bool) or not isinstance(poster_for, int) or poster_for < 1:
        raise Invalid("poster_for must be the id of one of the ask's attachments (0 or omitted: a new one)")
    if poster_for:
        if mime == VIDEO_TYPE:
            raise Invalid("a poster is a still image (PNG, JPEG or WebP), not a video")
        if size > POSTER_MAX_BYTES:
            raise Invalid(f"the poster must be at most {POSTER_MAX_BYTES // _MB} MB (bytes is {size})")
        if caption is not None or source_url is not None:
            raise Invalid("a poster takes no caption or source_url: they belong to the attachment it is for")
    elif mime == VIDEO_TYPE and size > VIDEO_MAX_BYTES:
        raise Invalid(f"a video may be at most {VIDEO_MAX_BYTES // _MB} MB (bytes is {size}): attach a bigger one as "
                      "a Drive link with a poster")
    elif mime != VIDEO_TYPE and size > IMAGE_MAX_BYTES:
        raise Invalid(f"an image may be at most {IMAGE_MAX_BYTES // _MB} MB (bytes is {size})")
    return mime, size, caption, source_url, poster_for


def _attachment(ask: dict, poster_for: int) -> dict | None:
    """The attachment an upload landed on, in the ask this attach wrote: the one it is the poster of, or the new
    one (plan_attach appends it)."""
    items = ask.get(ATTACHMENTS) or []
    if poster_for:
        return next((item for item in items if item.get("id") == poster_for), None)
    return items[-1] if items else None


class MediaService:
    """The agents' attachment calls on their user's Drive (media_for(identity) -> that user's AskMedia), each inside
    the guard. `http`: the public-only client (safe_http.public_client) every url is fetched with."""

    def __init__(self, media_for: Callable[[HostedIdentity], AskMedia], http: httpx.Client, grants: UploadGrants,
                 guard: MediaGuard, public_url: str):
        self.media_for = media_for
        self.http = http
        self.grants = grants
        self.guard = guard
        self.public_url = public_url

    def create(self, who: HostedIdentity, data: dict) -> dict:
        """POST /api/asks and ask_create: `attachments` absent, null or [] is a plain create. Otherwise the fields
        are checked first (nothing is fetched for a create that can't happen), then every item is prepared within
        one 60 s fetch budget, then the files and the ask are written (AskMedia.create)."""
        data = dict(data)
        items = data.pop(ATTACHMENTS, None)
        if items is None or items == []:
            return self.media_for(who).asks.create(who, data)
        plan_create(who, data)
        items = attachment_list(items)
        with self.guard.hold(who, len(items), CREATE_SLOTS if any(map(fetches, items)) else 0):
            prepared = prepare_all(items, self.http)
            return self.media_for(who).create(who, data, prepared)

    def attach(self, who: HostedIdentity, ask_id, item: dict, poster_for=0) -> dict:
        """POST /api/asks/{id}/attachments and ask_attach: one item ({url | data, link, title, caption,
        source_url}), as the next attachment or as the poster of attachment `poster_for` -> the ask."""
        with self.guard.hold(who, 1, FETCH_SLOTS if fetches(item) else 0):
            prepared = prepare(item, self.http, time.monotonic() + FETCH_DEADLINE)
            return self.media_for(who).attach(who, ask_id, prepared, poster_for)

    def upload(self, who: HostedIdentity, ask_id, mime, size, caption=None, source_url=None, poster_for=0) -> dict:
        """POST /api/asks/{id}/uploads and ask_upload: a one-time upload of `size` bytes of `mime` for the ask,
        after the checks a token can be refused by now -> {upload: {url, method, headers, expires_at}, curl}."""
        with self.guard.hold(who, 1):
            mime, size, caption, source_url, poster_for = _upload_args(mime, size, caption, source_url, poster_for)
            ask = self.media_for(who).asks.get(ask_id)
            draft = {"bytes": size} if poster_for else {
                "id": next_attachment_id(ask), "kind": "video" if mime == VIDEO_TYPE else "image", "bytes": size}
            plan_attach(who, draft, poster_for, lambda: ask, utc_now())  # owner, open, target, count, 40 MB
            token, expires_at = self.grants.issue(who, ask["id"], mime, size, caption, source_url, poster_for)
            return upload_reply(self.public_url, token, mime, expires_at)

    def detach(self, who: HostedIdentity, ask_id, n) -> dict:
        """DELETE /api/asks/{id}/attachments/{n} and ask_detach -> the ask."""
        with self.guard.hold(who, 1):
            return self.media_for(who).detach(who, ask_id, n)

    def receive(self, grant: UploadGrant, raw: bytes) -> dict:
        """The bytes of PUT /api/uploads (exactly grant.bytes, read under a slot the caller holds): checked like any
        attachment against the declared type, then attached to the ask as it is now -> {ask_id, attachment}."""
        checked = check_media(raw, grant.mime)
        item = Item("video" if checked.mime == VIDEO_TYPE else "image", checked, None, None, grant.caption,
                    grant.source_url)
        who = grant.identity
        ask = self.media_for(who).attach(who, grant.ask_id, item, grant.poster_for)
        return {"ask_id": ask["id"], "attachment": _attachment(ask, grant.poster_for)}


# ---------- REST ----------

class AttachIn(StrictBody):
    """The values are checked by attachments.prepare (the same messages as ask_attach)."""
    url: Any = None
    data: Any = None
    link: Any = None
    title: Any = None
    caption: Any = None
    source_url: Any = None
    poster_for: Any = None


class UploadIn(StrictBody):
    mime: Any
    bytes: Any
    caption: Any = None
    source_url: Any = None
    poster_for: Any = None


def _exact_size(size: int, got: int) -> Invalid:
    return Invalid(f"the body must be exactly {size} bytes, the size given to ask_upload (it has {got})")


async def _read_exactly(request: Request, size: int) -> bytes:
    """The body, at most `size` bytes, in one buffer (BytesIO.getvalue() hands it over without a copy): a byte more
    is a 413 at once, without reading on; fewer (or the client gone) a 422; nothing for UPLOAD_IDLE seconds, or
    more than UPLOAD_DEADLINE for all of it, a 408 (the caller holds a media slot meanwhile)."""
    body = io.BytesIO()
    try:
        with anyio.fail_after(UPLOAD_DEADLINE):
            while True:
                with anyio.fail_after(UPLOAD_IDLE):
                    message = await request.receive()
                if message["type"] == "http.disconnect":
                    raise _exact_size(size, body.tell())
                chunk = message.get("body", b"")
                if body.tell() + len(chunk) > size:
                    raise HTTPException(413, f"request body too large: the body must be exactly {size} bytes, the "
                                             "size given to ask_upload")
                body.write(chunk)
                if not message.get("more_body", False):
                    break
    except TimeoutError:
        raise HTTPException(408, UPLOAD_SLOW) from None
    if body.tell() != size:
        raise _exact_size(size, body.tell())
    return body.getvalue()


def add_media_routes(app: FastAPI, media: MediaService) -> None:
    """The attachment routes of the agents' REST, on the MediaService the MCP tools use too (create_hosted_app)."""

    @app.post("/api/asks/{ask_id}/attachments", status_code=201)
    def attach(ask_id: int, body: AttachIn, who: HostedIdentity = Depends(current_identity)):
        item = body.model_dump(exclude_unset=True)
        poster_for = item.pop("poster_for", 0)
        return media.attach(who, ask_id, item, poster_for)

    @app.delete("/api/asks/{ask_id}/attachments/{n}")
    def detach(ask_id: int, n: int, who: HostedIdentity = Depends(current_identity)):
        return media.detach(who, ask_id, n)

    @app.post("/api/asks/{ask_id}/uploads", status_code=201)
    def request_upload(ask_id: int, body: UploadIn, who: HostedIdentity = Depends(current_identity)):
        reply = media.upload(who, ask_id, body.mime, body.bytes, body.caption, body.source_url, body.poster_for)
        return JSONResponse(reply, status_code=201, headers=NO_STORE)

    @app.put(UPLOAD_PATH, status_code=201)
    async def put_upload(request: Request):
        """The file of an upload token (claimed by the bearer check, before this runs). Cheap refusals first, then
        a slot, then the body."""
        grant = request.scope.get(IDENTITY_SCOPE_KEY)
        if not isinstance(grant, UploadGrant):  # the bearer check lets nothing else through
            raise HTTPException(401, "send the upload token from ask_upload", headers={"WWW-Authenticate": "Bearer"})
        declared = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if declared != grant.mime:
            raise Invalid(f"Content-Type must be {grant.mime}, the type given to ask_upload")
        length = int(request.headers["content-length"])  # RequestGuard: present, digits, at most grant.bytes
        if length != grant.bytes:
            raise _exact_size(grant.bytes, length)
        with media.guard.hold(None, 0, FETCH_SLOTS, busy=UPLOAD_BUSY):
            raw = await _read_exactly(request, grant.bytes)
            return await anyio.to_thread.run_sync(media.receive, grant, raw)
