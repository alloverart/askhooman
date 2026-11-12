"""Pictures and video in asks (SPEC v5 step 4, "How the bytes get in"): an attachment given by `url`, fetched once
by the bridge with the SSRF-safe client of client metadata documents and set_my_look's image (safe_http.py).

- The URL follows safe_http.check_url (https on port 443, at most 2000 chars, a public host, no user info); every
  address its host resolves to must be public, checked at connect time by the client's transport.
- Google Drive and Docs URLs are refused before any connection: they answer with a sign-in page or a redirect, never
  the file. The host is compared as httpx would resolve it (media.ascii_host), so a trailing dot, capitals or the
  dots 。．｡ don't get past.
- No redirects, `Accept-Encoding: identity`, a compressed body refused, the body read raw.
- At most 5 s per socket operation and 30 s for the fetch, or less when the caller's budget (60 s for all the
  fetches of one ask_create) ends first: the socket layer's deadline stops a server that stalls, a check between
  chunks one that drips.
- The Content-Type must be an allowed type, read before the body, and the type read from the bytes
  (media.check_media, which then apply every other check). Its kind sets the cap on what is read: 5 MB for an
  image, 20 MB for a video, or less when the caller says how much room is left (`max_bytes`: an ask_create's
  fetches share its 40 MB, which is what its 2 slots allow for); a larger Content-Length is refused before reading.
  The body lives in one buffer that is handed over, not copied, so a fetch holds about its cap (4 slots × 20 MB),
  never twice it.
- A host IDNA refuses (an empty label, one over 63 chars) is refused before connecting, as the resolver would.

Every refusal is rules.Invalid; a body over `max_bytes` but within its kind's cap is NoRoom, so the caller can say
how much its items would take.
"""

import io
import re
import time
from urllib.parse import urlsplit

import httpx

from ..rules import Invalid
from .media import (
    CONVERT_HINT,
    IMAGE_MAX_BYTES,
    MEDIA_TYPES,
    VIDEO_MAX_BYTES,
    VIDEO_TYPE,
    Checked,
    ascii_host,
    check_media,
)
from .safe_http import UnsafeURL, check_url, deadline, raw_chunks, refuse_encoded

DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
DRIVE_REFUSED = ("AskHooman can't open files in Google Drive: download it and use ask_upload, or attach the link "
                 "with link=true")
FETCH_TIMEOUT = 5.0  # per socket operation
FETCH_DEADLINE = 30.0  # one fetch, wall clock
CREATE_FETCH_DEADLINE = 60.0  # all the fetches of one ask_create; its caller passes `until` from it
_ACCEPT = ", ".join(MEDIA_TYPES)
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")  # RFC 6838 names
_LENGTH = re.compile(r"[0-9]{1,18}")  # a Content-Length worth reading; anything else is left to the read cap
_MB = 1024 * 1024


def _host_name(url: str) -> str | None:
    """The URL's host as httpx resolves it (media.ascii_host); None if it has none or IDNA refuses it."""
    try:
        host = urlsplit(url).hostname
        return None if host is None else ascii_host(host)
    except ValueError:  # not a URL (check_url says so), or a host IDNA refuses (UnicodeError)
        return None


def _too_slow() -> Invalid:
    return Invalid(f"url was too slow: a fetch may take {FETCH_DEADLINE:g} s, all the fetches of one ask_create "
                   f"{CREATE_FETCH_DEADLINE:g} s")


class NoRoom(Invalid):
    """The body is larger than the caller's `max_bytes`, not than its kind's cap. `size` is its Content-Length
    (`exact`), or what had come when the read stopped (a lower bound)."""

    def __init__(self, size: int, room: int, exact: bool):
        super().__init__(f"the file at url has {'' if exact else 'at least '}{size} bytes; there is room for {room}")
        self.size = size
        self.exact = exact


def _too_big(mime: str) -> Invalid:
    if mime == VIDEO_TYPE:
        return Invalid(f"the video at url is larger than {VIDEO_MAX_BYTES // _MB} MB: attach a bigger one as a "
                       "Drive link with a poster")
    return Invalid(f"the image at url is larger than {IMAGE_MAX_BYTES // _MB} MB")


def _over(mime: str, size: int, kind_cap: int, room: int | None, exact: bool) -> Invalid:
    """A body of (at least) `size` bytes is too big: over its kind's cap (that message wins), else over `room`."""
    return _too_big(mime) if room is None or size > kind_cap else NoRoom(size, room, exact)


def _read(response: httpx.Response, mime: str, until: float, clock, room: int | None) -> bytes:
    """The raw body, at most the cap of its kind and at most `room` bytes, in one buffer: BytesIO.getvalue() hands
    that buffer over instead of copying it, so a fetch never holds two copies of a 20 MB body."""
    kind_cap = VIDEO_MAX_BYTES if mime == VIDEO_TYPE else IMAGE_MAX_BYTES
    cap = kind_cap if room is None else min(kind_cap, room)
    length = response.headers.get("content-length", "").strip()
    if _LENGTH.fullmatch(length) and int(length) > cap:
        raise _over(mime, int(length), kind_cap, room, exact=True)
    body = io.BytesIO()
    for chunk in raw_chunks(response):
        if body.tell() + len(chunk) > cap:
            raise _over(mime, body.tell() + len(chunk), kind_cap, room, exact=False)
        body.write(chunk)
        if clock() >= until:
            raise _too_slow()
    return body.getvalue()


def fetch_media(http: httpx.Client, url: str, *, until: float, max_bytes: int | None = None,
                clock=time.monotonic) -> Checked:
    """The image or MP4 at `url`, fetched once and checked like any attachment; Invalid otherwise. `until` is when
    the caller's time for fetching ends, on `clock`; one fetch also stops FETCH_DEADLINE after it starts.
    `max_bytes`: the most the caller has room for (None: the kind's cap alone); a body over it is NoRoom."""
    if not isinstance(url, str):
        raise Invalid("url must be a string")
    url = url.strip()
    name = _host_name(url)
    if name in DRIVE_HOSTS:
        raise Invalid(DRIVE_REFUSED)
    try:
        check_url(url, "url")
    except UnsafeURL as exc:
        raise Invalid(str(exc)) from None
    if name is None:  # check_url found a host, so IDNA refused it (the resolver would too)
        raise Invalid("url must have a valid host name")
    until = min(until, clock() + FETCH_DEADLINE)
    left = until - clock()
    if left <= 0:
        raise _too_slow()
    try:
        with deadline(left), http.stream("GET", url, headers={"Accept": _ACCEPT, "Accept-Encoding": "identity"},
                                         timeout=FETCH_TIMEOUT, follow_redirects=False) as response:
            if response.status_code != 200:
                raise Invalid(f"url answered HTTP {response.status_code} (redirects are not followed: give the "
                              "file's own URL)")
            declared = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if declared not in MEDIA_TYPES:
                sent = declared if _MEDIA_TYPE.fullmatch(declared) else "no usable Content-Type"
                raise Invalid(f"url must serve a PNG, JPEG or WebP image or an MP4 video (it sent {sent}): "
                              f"{CONVERT_HINT}")
            try:
                refuse_encoded(response)
            except ValueError as exc:
                raise Invalid(f"url: {exc}") from None
            raw = _read(response, declared, until, clock, max_bytes)
    except httpx.TimeoutException:
        raise _too_slow() from None
    except httpx.HTTPError:
        raise Invalid("url could not be fetched (unreachable, or not a public host)") from None
    except (httpx.InvalidURL, UnicodeError):  # a host httpx can't encode or decode, e.g. a broken xn-- label
        raise Invalid("url is not a valid URL") from None
    return check_media(raw, declared)
