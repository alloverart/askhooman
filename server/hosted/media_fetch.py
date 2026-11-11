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
- At most 20 MB read (the video cap). The Content-Type must be an allowed type and the type read from the bytes
  (media.check_media), which then apply every other check.

Every refusal is rules.Invalid.
"""

import re
import time
from urllib.parse import urlsplit

import httpx

from ..rules import Invalid
from .media import CONVERT_HINT, MEDIA_TYPES, VIDEO_MAX_BYTES, Checked, ascii_host, check_media
from .safe_http import UnsafeURL, check_url, deadline, raw_chunks, refuse_encoded

DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
DRIVE_REFUSED = ("AskHooman can't open files in Google Drive: download it and use ask_upload, or attach the link "
                 "with link=true")
FETCH_TIMEOUT = 5.0  # per socket operation
FETCH_DEADLINE = 30.0  # one fetch, wall clock
CREATE_FETCH_DEADLINE = 60.0  # all the fetches of one ask_create; its caller passes `until` from it
_ACCEPT = ", ".join(MEDIA_TYPES)
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")  # RFC 6838 names


def _on_drive(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
        return host is not None and ascii_host(host) in DRIVE_HOSTS
    except ValueError:  # not a URL, or a host IDNA refuses (UnicodeError): check_url or httpx turns it away
        return False


def _too_slow() -> Invalid:
    return Invalid(f"url was too slow: a fetch may take {FETCH_DEADLINE:g} s, all the fetches of one ask_create "
                   f"{CREATE_FETCH_DEADLINE:g} s")


def _read(response: httpx.Response, until: float, clock) -> bytes:
    body = bytearray()
    for chunk in raw_chunks(response):
        body.extend(chunk)
        if len(body) > VIDEO_MAX_BYTES:
            raise Invalid(f"url sent more than {VIDEO_MAX_BYTES // (1024 * 1024)} MB (an image may be 5 MB, a video "
                          "20 MB); attach a bigger video as a Drive link with a poster")
        if clock() >= until:
            raise _too_slow()
    return bytes(body)


def fetch_media(http: httpx.Client, url: str, *, until: float, clock=time.monotonic) -> Checked:
    """The image or MP4 at `url`, fetched once and checked like any attachment; Invalid otherwise. `until` is when
    the caller's time for fetching ends, on `clock`; one fetch also stops FETCH_DEADLINE after it starts."""
    if not isinstance(url, str):
        raise Invalid("url must be a string")
    url = url.strip()
    if _on_drive(url):
        raise Invalid(DRIVE_REFUSED)
    try:
        check_url(url, "url")
    except UnsafeURL as exc:
        raise Invalid(str(exc)) from None
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
            raw = _read(response, until, clock)
    except httpx.TimeoutException:
        raise _too_slow() from None
    except httpx.HTTPError:
        raise Invalid("url could not be fetched (unreachable, or not a public host)") from None
    except (httpx.InvalidURL, UnicodeError):  # a host httpx can't encode or decode, e.g. a broken xn-- label
        raise Invalid("url is not a valid URL") from None
    return check_media(raw, declared)
