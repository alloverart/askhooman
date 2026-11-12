"""Pictures and video in asks (SPEC v5 step 4, "Checks"): what an attachment's bytes may be, read from the bytes
themselves, never from a file name or the sender's word.

- image: PNG, JPEG or WebP by its own header (looks.image_info, the agent look's reader), at most 5 MB and 4096 px
  per side. A poster is an image of at most 1 MB. SVG, GIF, HEIC, AVIF and anything else are refused with a hint.
- video: MP4 only, an ISO BMFF file whose ftyp lists an MP4 brand (QuickTime's `qt  ` alone is refused), at most
  20 MB. A walk over the box headers finds `moov` wherever it is: `mvhd` gives the duration (at most 180 s), the
  first video track's `tkhd` the size (at most 4096 px per side). The walk reads headers in place (no copies of the
  payload), trusts no size that runs past the box around it, reads at most 10 000 boxes and descends only
  moov → trak → mdia (never deeper than 6). Codecs are not checked.
- `data:` URLs (at most 180 KB decoded), link card and source URLs (https, a DNS name, never an IP address), the
  link card's provider, and the file extension of a stored file.

All pure; every refusal is rules.Invalid (422 over REST, a tool error over MCP).
"""

import base64
import binascii
import re
import struct
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..rules import Invalid
from .looks import image_info
from .safe_http import UnsafeURL, check_url

IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
VIDEO_TYPE = "video/mp4"
MEDIA_TYPES = (*IMAGE_TYPES, VIDEO_TYPE)
IMAGE_MAX_BYTES = 5 * 1024 * 1024
POSTER_MAX_BYTES = 1024 * 1024
VIDEO_MAX_BYTES = 20 * 1024 * 1024
VIDEO_MAX_MS = 180_000
MEDIA_MAX_SIDE = 4096
DATA_MAX_BYTES = 180 * 1024  # decoded; keeps a request with one data URL under the 256 KB body cap
CAPTION_MAX = 300
ATTACH_MAX = 6  # per ask; posters don't count
ASK_MEDIA_MAX = 40 * 1024 * 1024  # stored per ask, posters included
CONVERT_HINT = "convert to PNG, JPEG or WebP; send motion as MP4"

MP4_BRANDS = frozenset((b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V "))
MP4_MAX_BOXES = 10_000  # read in one walk, at every level
MP4_MAX_DEPTH = 6  # top-level boxes are depth 1
_FTYP_BRANDS_READ = 64  # compatible brands looked at; real files list a handful
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", VIDEO_TYPE: "mp4"}
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})")
_URL_REFUSED_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f\\]")  # Python's urlsplit and browsers read these differently
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")  # a host ending in one is an IPv4 address to a browser
_DOCS_KINDS = {"document": "docs", "spreadsheets": "sheets", "presentation": "slides"}
_YOUTUBE = frozenset(("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"))
_FIGMA = frozenset(("figma.com", "www.figma.com"))


@dataclass(frozen=True)
class Checked:
    """A file that passed the checks: its type and size as read from its bytes, and the bytes."""
    mime: str
    width: int
    height: int
    bytes: int
    duration_ms: int | None  # videos only
    raw: bytes = field(repr=False)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):g} MB"


def _check_sides(what: str, width: int, height: int) -> None:
    if not (0 < width <= MEDIA_MAX_SIDE and 0 < height <= MEDIA_MAX_SIDE):
        raise Invalid(f"the {what} must be at most {MEDIA_MAX_SIDE}×{MEDIA_MAX_SIDE} pixels (it is {width}×{height})")


def _same_type(declared: str, checked: Checked) -> Checked:
    if declared != checked.mime:
        raise Invalid(f"the file was declared {declared} but its bytes are {checked.mime}")
    return checked


# ---------- images ----------

def check_image(raw: bytes, max_bytes: int = IMAGE_MAX_BYTES) -> Checked:
    """An image under the attachment rules (a poster: max_bytes=POSTER_MAX_BYTES); Invalid otherwise."""
    if len(raw) > max_bytes:  # first, so a hostile header is never parsed past the cap
        raise Invalid(f"the image must be at most {_mb(max_bytes)} (it is {len(raw)} bytes)")
    info = image_info(raw)
    if info is None:
        raise Invalid(f"the image is not a PNG, JPEG or WebP, or its header is damaged: {CONVERT_HINT}")
    mime, width, height = info
    _check_sides("image", width, height)
    return Checked(mime, width, height, len(raw), None, raw)


# ---------- MP4 ----------

class _Damaged(Exception):
    """The box structure can't be trusted."""


class _Boxes:
    """Box headers of one ISO BMFF buffer, read in place, with one budget of boxes for the whole walk."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.left = MP4_MAX_BOXES

    def each(self, start: int, end: int, depth: int):
        """(type, payload start, box end) of each box in raw[start:end]; the boxes must fill the range exactly."""
        if depth > MP4_MAX_DEPTH:
            raise _Damaged
        pos = start
        while pos < end:
            if self.left == 0:
                raise _Damaged
            self.left -= 1
            if end - pos < 8:
                raise _Damaged
            size, kind = struct.unpack_from(">I4s", self.raw, pos)
            header = 8
            if size == 1:  # the 64-bit size follows the type
                if end - pos < 16:
                    raise _Damaged
                (size,) = struct.unpack_from(">Q", self.raw, pos + 8)
                header = 16
            elif size == 0:  # "to the end of the file": only the last top-level box may say so
                if depth != 1:
                    raise _Damaged
                size = end - pos
            if not header <= size <= end - pos:
                raise _Damaged
            yield kind, pos + header, pos + size
            pos += size


def _duration_ms(raw: bytes, start: int, end: int) -> int:
    """From an mvhd payload: version 0 has 32-bit times, version 1 64-bit."""
    version = raw[start] if start < end else None
    if version == 0 and end - start >= 20:
        timescale, duration = struct.unpack_from(">II", raw, start + 12)
    elif version == 1 and end - start >= 32:
        timescale, duration = struct.unpack_from(">IQ", raw, start + 20)
    else:
        raise _Damaged
    if timescale == 0:
        raise _Damaged
    return duration * 1000 // timescale


def _track_size(raw: bytes, start: int, end: int) -> tuple[int, int]:
    """From a tkhd payload: width and height are 16.16 fixed point after the matrix."""
    version = raw[start] if start < end else None
    offset = {0: 76, 1: 88}.get(version)
    if offset is None or end - start < offset + 8:
        raise _Damaged
    width, height = struct.unpack_from(">II", raw, start + offset)
    return width >> 16, height >> 16


def _handler(boxes: _Boxes, start: int, end: int) -> bytes | None:
    """The handler type ("vide", "soun", …) from the hdlr box of an mdia payload."""
    for kind, body, box_end in boxes.each(start, end, 4):
        if kind == b"hdlr":
            if box_end - body < 12:
                raise _Damaged
            return boxes.raw[body + 8:body + 12]
    return None


def _video_size(boxes: _Boxes, start: int, end: int) -> tuple[int, int] | None:
    """(width, height) if this trak payload is a video track, else None."""
    tkhd = handler = None
    for kind, body, box_end in boxes.each(start, end, 3):
        if kind == b"tkhd" and tkhd is None:
            tkhd = (body, box_end)
        elif kind == b"mdia" and handler is None:
            handler = _handler(boxes, body, box_end)
    if handler != b"vide" or tkhd is None:
        return None
    return _track_size(boxes.raw, *tkhd)


def mp4_info(raw: bytes) -> tuple[int, int, int] | None:
    """(width, height, duration_ms) of an MP4's movie, or None if its boxes don't fill the file exactly, or it has no
    movie (`moov` with `mvhd`) or no video track. The brands and the limits are check_video's."""
    boxes = _Boxes(raw)
    try:
        moov = None
        for kind, body, end in boxes.each(0, len(raw), 1):
            if kind == b"moov" and moov is None:
                moov = (body, end)
        if moov is None:
            return None
        duration_ms = size = None
        for kind, body, end in boxes.each(*moov, 2):
            if kind == b"mvhd" and duration_ms is None:
                duration_ms = _duration_ms(raw, body, end)
            elif kind == b"trak" and size is None:
                size = _video_size(boxes, body, end)
    except (_Damaged, struct.error):
        return None
    if duration_ms is None or size is None:
        return None
    return (*size, duration_ms)


def _brands(raw: bytes) -> set[bytes]:
    """The major and compatible brands of the ftyp box that starts an ISO BMFF file; empty if there is none."""
    if raw[4:8] != b"ftyp":
        return set()
    size, start = int.from_bytes(raw[:4], "big"), 8
    if size == 1:
        size, start = int.from_bytes(raw[8:16], "big"), 16
    if not start + 8 <= size <= len(raw):
        return set()
    end = min(size, start + 8 + 4 * _FTYP_BRANDS_READ)
    return {raw[start:start + 4], *(raw[i:i + 4] for i in range(start + 8, end - 3, 4))}


def check_video(raw: bytes) -> Checked:
    """An MP4 under the attachment rules; Invalid otherwise."""
    brands = _brands(raw)
    if not brands & MP4_BRANDS:
        if b"qt  " in brands:
            raise Invalid("QuickTime .mov is not accepted: remux it to MP4")
        raise Invalid(f"the file is not an MP4 video: {CONVERT_HINT}")
    if len(raw) > VIDEO_MAX_BYTES:
        raise Invalid(f"the video must be at most {_mb(VIDEO_MAX_BYTES)} (it is {len(raw)} bytes); "
                      "attach a bigger one as a Drive link with a poster")
    info = mp4_info(raw)
    if info is None:
        raise Invalid("the MP4 can't be read: no movie header or video track, or its boxes are damaged")
    width, height, duration_ms = info
    if duration_ms == 0:  # fragmented MP4s leave it to the fragments, so the 180 s cap couldn't hold
        raise Invalid("the video's duration is missing: export a regular (not fragmented) MP4")
    if duration_ms > VIDEO_MAX_MS:
        raise Invalid(f"the video must be at most {VIDEO_MAX_MS // 1000} s long (it is {duration_ms / 1000:g} s)")
    _check_sides("video", width, height)
    return Checked(VIDEO_TYPE, width, height, len(raw), duration_ms, raw)


# ---------- any attachment's bytes ----------

def check_media(raw: bytes, declared: str | None = None) -> Checked:
    """An image or an MP4, told apart by its bytes; `declared` (a Content-Type, an upload's mime) must be the type
    the bytes are."""
    checked = check_video(raw) if raw[4:8] == b"ftyp" else check_image(raw)
    return checked if declared is None else _same_type(declared, checked)


def decode_data(value: str) -> Checked:
    """An image sent inline as a data:image/(png|jpeg|webp);base64 URL, at most 180 KB decoded, of the type it says."""
    match = _DATA_URL.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise Invalid(f"data must be a data:image/png, data:image/jpeg or data:image/webp URL in base64 "
                      f"({CONVERT_HINT})")
    if len(match.group(2)) > (DATA_MAX_BYTES + 2) // 3 * 4:
        raise Invalid(f"data must be at most {DATA_MAX_BYTES // 1024} KB decoded; send a bigger file by url or "
                      "upload")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("data is not valid base64") from None
    return _same_type(match.group(1), check_image(raw))


def ext_of(mime: str) -> str:
    """The extension of a stored media file of this type (Drive names media-<ask>-<n>.<ext>)."""
    try:
        return _EXTENSIONS[mime]
    except KeyError:
        raise Invalid(f"unsupported media type {mime!r}") from None


# ---------- links ----------

def ascii_host(host: str) -> str:
    """A host name as browsers and httpx resolve it: IDNA maps full-width characters to ASCII and the dots 。．｡
    to ".", then lowercase, without the trailing dot. UnicodeError if IDNA refuses it (an empty label, a label over
    63 chars, a character IDNA prohibits)."""
    return host.encode("idna").decode("ascii").lower().rstrip(".")


def clean_link_url(url: str, what: str) -> str:
    """A link card's url or an attachment's source_url, which the apps open: safe_http.check_url's rules (https on
    port 443, at most 2000 chars, a public DNS name, no user info), never an IP address even a public one, and no
    character that URL parsers read differently. Returns it stripped.

    The IP check runs on the host as browsers resolve it, after IDNA mapping: full-width digits and the dots
    。．｡ become ASCII, so "１９２.１６８.１.１" is 192.168.1.1; a real IDN host (пример.рф) stays a name."""
    if not isinstance(url, str):
        raise Invalid(f"{what} must be a string")
    url = url.strip()
    if _URL_REFUSED_CHARS.search(url):
        raise Invalid(f"{what} must not contain spaces, backslashes or control characters")
    try:
        host = check_url(url, what)
    except UnsafeURL as exc:
        raise Invalid(str(exc)) from None
    try:
        name = ascii_host(host)
    except UnicodeError:
        raise Invalid(f"{what} must have a valid host name") from None
    if ":" in name or _NUMERIC_LABEL.fullmatch(name.rsplit(".", 1)[-1]):
        raise Invalid(f"{what} must name a host, not an IP address")
    return url


def provider_of(url: str) -> str:
    """Which service a link card points to, by its host as browsers resolve it (ascii_host: a trailing dot, capitals
    or the dots 。．｡ still name Drive) and, for docs.google.com, its first path segment."""
    try:
        parts = urlsplit(url)
        host = ascii_host(parts.hostname or "")
    except (ValueError, UnicodeError):
        return "web"
    if host == "drive.google.com":
        return "drive"
    if host == "docs.google.com":
        return _DOCS_KINDS.get(parts.path.lstrip("/").split("/", 1)[0], "docs")
    if host in _YOUTUBE:
        return "youtube"
    if host in _FIGMA:
        return "figma"
    return "web"
