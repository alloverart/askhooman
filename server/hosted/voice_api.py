"""GET /media/v1/<token> (SPEC v5 "5. Voice answers", "What the agent gets"): the agent that asked fetches its user's
voice answer with the link answer_voice.url carried (voice.VoiceLinks). The token is the only credential: no
Authorization is looked at, and there is no CORS (AppCors opens the App API only).

Checked in this order, every failure a 404 with one message: the token opens (sealed by this bridge, context "media")
and has not expired; then the budget of the token's user and agent (60 a minute each, "voice" in ratelimit.py: 429,
so one leaked or revoked link can't block the user's other agents); the agent is still connected (not revoked) and
the ask is its own; the ask's CURRENT answer_voice names that file; Drive says the file is this ask's recording
(appProperties kind "voice" and ask_id, as the apps upload it) of at most 5 MB (by Drive's size, and while reading when
Drive gives none). A recording that was deleted (file_id null, a closed ask whatever an older app left in it, or gone
from Drive) is 410; a reopened ask has no voice answer, and an ask file the bridge can't read as an ask (an unknown or
missing status, not JSON) has none either (404); Drive unreachable is 503. Then the bytes are read from the user's
Drive with the user's grant and sent with

    Content-Type             answer_voice.mime (audio/mp4 or audio/webm: the allowlist), never Drive's
    Content-Length           the bytes read
    Content-Disposition      attachment; filename="ask-<id>-voice.<m4a|webm>"
    Cache-Control            private, no-store
    X-Content-Type-Options   nosniff
    Content-Security-Policy  sandbox

The bytes pass through memory only while they are read from Drive and sent (at most 5 MB, in one piece: no Range, and
HEAD is 405). They hold one of the instance's media slots from the read until the response has gone out, so the memory
budget of pictures and video holds (busy is 503 with Retry-After). Nothing of the recording, its transcript or the
token is stored or logged; Cloud Run's request log keeps the path, and the token in it is dead within the hour.
"""

import logging
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..drive import DriveBadFile, DriveError, DriveNotFound
from ..rules import ANSWER_VOICE, AskError, NotFound
from .accounts import Accounts
from .media_api import FETCH_SLOTS, Slots
from .ratelimit import RateLimiter
from .records import BadFile, DrivePool, HostedAsks
from .voice import MEDIA_PATH, VOICE_MAX_BYTES, VOICE_TYPES, VoiceLinks

log = logging.getLogger("asks.hosted.voice")

NO_SUCH = "no such recording, or the link has expired: read the ask again for a new link"
DELETED = "the recording was deleted"
UNAVAILABLE = "Google Drive is not available right now, try again"
BUSY = "busy, try again in a few seconds"
NO_STORE = {"Cache-Control": "no-store"}


def _refuse(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers={**NO_STORE, **(headers or {})})


def _read(drive, file_id: str) -> bytes | None:
    """The recording's bytes, at most 5 MB; None for a file that is larger (or came compressed)."""
    try:
        return drive.download_bytes(file_id, VOICE_MAX_BYTES)
    except DriveBadFile:
        return None


class _Held(Response):
    """The recording's response: it gives its media slot back once it has been sent, or once sending failed (a
    background task would not run if the send raised), so the bytes count as in flight until they have left."""

    def __init__(self, content: bytes, headers: dict, release):
        super().__init__(content, status_code=200, headers=headers)
        self._release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            release, self._release = self._release, None
            if release is not None:
                release()


def _size(meta: dict) -> int | None:
    try:
        return int(meta.get("size"))
    except (TypeError, ValueError):
        return None


def add_voice_route(app: FastAPI, links: VoiceLinks, accounts: Accounts, pool: DrivePool, limiter: RateLimiter,
                    slots: Slots, files: Callable[[str, str | None], Response | None] | None = None) -> None:
    """The route, on the hosted app (create_hosted_app): `slots` are the media slots of attachments (media_api).
    `files` (files_api.add_file_routes) answers a token that is a file's link and gives None for any other."""

    @app.get(f"{MEDIA_PATH}{{token:path}}", include_in_schema=False)
    def voice_recording(token: str, request: Request):
        sealed = links.open(token)
        if sealed is None:  # not a recording's link: a file's (SPEC 7.4, own context), else nothing we sealed
            answer = files(token, request.headers.get("range")) if files is not None else None
            return answer if answer is not None else _refuse(404, NO_SUCH)
        user = sealed["user"]
        if not limiter.allow("voice", f"{user}/{sealed['agent']}"):  # per agent: one link can't block the others
            return _refuse(429, "too many requests for recordings: at most 60 a minute per agent",
                           {"Retry-After": str(limiter.retry_after("voice"))})
        try:
            agent = accounts.own_agent(user, sealed["agent"])
        except Exception as exc:  # the metadata store is down
            log.warning("voice link check failed: %s", type(exc).__name__)
            return _refuse(503, "temporarily unavailable, try again", {"Retry-After": "5"})
        if agent is None:  # revoked, another user's, or no longer there
            return _refuse(404, NO_SUCH)
        records = pool.records(user)
        try:
            ask = HostedAsks(records).get(sealed["ask_id"], with_events=False)
        except (NotFound, BadFile):  # a broken ask file: retrying won't help
            return _refuse(404, NO_SUCH)
        except AskError:
            return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
        voice = ask.get(ANSWER_VOICE)
        if ask["agent"] != agent["slug"] or voice is None:
            return _refuse(404, NO_SUCH)
        if voice["file_id"] is None or ask["status"] == "closed":  # closed: the recording went with the close
            return _refuse(410, DELETED)
        if voice["file_id"] != sealed["file_id"]:
            return _refuse(404, NO_SUCH)
        file_id = voice["file_id"]
        try:
            meta = records.call(records.drive.get_meta, file_id)
        except (AskError, DriveError):
            return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
        if meta is None:
            return _refuse(410, DELETED)
        props = meta.get("appProperties") or {}
        size = _size(meta)
        if props.get("kind") != "voice" or props.get("ask_id") != str(ask["id"]) \
                or (size is not None and size > VOICE_MAX_BYTES):
            log.warning("hosted: file %s named by ask #%s is not its voice recording: not sent", file_id, ask["id"])
            return _refuse(404, NO_SUCH)
        if not slots.take(FETCH_SLOTS):
            return _refuse(503, BUSY, {"Retry-After": "5"})
        handed_over = False
        try:
            raw = records.call(_read, records.drive, file_id)
            if raw is None:  # over 5 MB although Drive gave no size (or came compressed): not a recording we send
                log.warning("hosted: file %s named by ask #%s is not its voice recording: not sent", file_id,
                            ask["id"])
                return _refuse(404, NO_SUCH)
            mime = voice["mime"]
            response = _Held(raw, {
                "Content-Type": mime,
                "Content-Length": str(len(raw)),
                "Content-Disposition": f'attachment; filename="ask-{ask["id"]}-voice.{VOICE_TYPES[mime]}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            }, lambda: slots.give(FETCH_SLOTS))
            handed_over = True  # the response gives the slot back once it has been sent
            return response
        except DriveNotFound:
            return _refuse(410, DELETED)
        except (AskError, DriveError):
            return _refuse(503, UNAVAILABLE, {"Retry-After": "30"})
        finally:
            if not handed_over:
                slots.give(FETCH_SLOTS)
