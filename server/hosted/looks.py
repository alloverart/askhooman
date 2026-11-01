"""An agent's look in the apps (SPEC v4 "Agent look"): its colour and its icon, so a user can tell their agents apart.

- `color`: a key of the agent palette (the hexes live with the apps, in the design system of the AskHooman apps,
  `design/system/README.md`). A new agent gets the colour least used among its user's active agents (ties go in
  palette order), so the first twelve all differ; agents connected before colours existed get one the first time
  the user's agents are listed (accounts.ensure_colors).
- `avatar`: the robot (stored as None), one emoji, or a small image kept inline as a data URL (PNG, WebP or JPEG,
  at most 256×256 pixels and 48 KB). Images are checked by their bytes (type, header, dimensions), never by what
  the sender says they are; SVG and anything else is refused.

The user changes the look in the app (PATCH /api/agents/{id}); an agent changes its own with the MCP tool
set_my_look (own_look below), which can also fetch an image from an https URL with the same SSRF-safe client as
client metadata documents and webhooks (safe_http.py).
"""

import base64
import binascii
import re
import struct
import unicodedata
from collections import Counter
from collections.abc import Iterable

import httpx

from ..rules import AskError, Invalid, NotFound
from .safe_http import UnsafeURL, check_url, deadline, read_capped

# Order matters: least_used() breaks ties by it, and the apps show the swatches in it. Append only.
AGENT_COLORS = ("mint", "sky", "peach", "pink", "lavender", "cocoa", "halo",
                "leaf", "ocean", "cherry", "plum", "slate")
AVATAR_KINDS = ("robot", "emoji", "image")
ROBOT = {"kind": "robot"}
EMOJI_MAX_BYTES = 16  # UTF-8; enough for skin tones and short ZWJ sequences ("🧑🏽‍💻"), not for family groups
IMAGE_MAX_BYTES = 48 * 1024
IMAGE_MAX_SIDE = 256
IMAGE_TYPES = ("image/png", "image/webp", "image/jpeg")
FETCH_TIMEOUT = 3.0  # per socket operation
FETCH_DEADLINE = 5.0  # the whole fetch of an image_url, wall clock

_DATA_URL = re.compile(r"data:(image/(?:png|webp|jpeg));base64,([A-Za-z0-9+/]*={0,2})")
_KEYCAP = re.compile("[0-9#*]️?⃣")
# What may follow the pictograph of one emoji (or of one part of a ZWJ sequence): a variation selector and/or a
# skin tone modifier.
_EMOJI_TAIL = re.compile("[︎️]?[\U0001f3fb-\U0001f3ff]?️?")
_JPEG_FRAMES = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # SOF0..SOF15 (not DHT, JPG, DAC)


class TooManyFetches(AskError):
    status_code = 429
    label = "rate limited"


# ---------- colours ----------

def color_key(value) -> str | None:
    """A stored colour if it is a palette key (anything else counts as none)."""
    return value if isinstance(value, str) and value in AGENT_COLORS else None


def least_used(colors: Iterable) -> str:
    """The palette colour fewest of these agents have; ties go in palette order."""
    counts = Counter(c for c in map(color_key, colors) if c)
    return min(AGENT_COLORS, key=lambda c: counts[c])  # min() keeps the first of equals: palette order


def clean_color(value) -> str:
    key = color_key(value)
    if key is None:
        raise Invalid(f"color must be one of: {', '.join(AGENT_COLORS)}")
    return key


# ---------- emoji ----------

def _pictograph(ch: str) -> bool:
    cp = ord(ch)
    if 0x1F1E6 <= cp <= 0x1F1FF:  # a regional indicator counts only as half of a flag
        return False
    category = unicodedata.category(ch)
    # Emoji newer than this Python's Unicode database are still unassigned to it: accept those in the emoji blocks.
    return category == "So" or (category == "Cn" and 0x1F000 <= cp <= 0x1FAFF)


def is_one_emoji(text: str) -> bool:
    """One emoji grapheme: a pictograph (+ variation selector, skin tone), a ZWJ sequence of those, a flag
    (two regional indicators) or a keycap. Letters, digits alone, text and several emoji are not."""
    if len(text) == 2 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in text):
        return True
    if _KEYCAP.fullmatch(text):
        return True
    return all(part and _pictograph(part[0]) and _EMOJI_TAIL.fullmatch(part[1:]) for part in text.split("‍"))


def clean_emoji(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("emoji must be one emoji, e.g. 🦊")
    text = value.strip()
    if len(text.encode("utf-8")) > EMOJI_MAX_BYTES or not is_one_emoji(text):
        raise Invalid(f"emoji must be exactly one emoji (at most {EMOJI_MAX_BYTES} bytes), e.g. 🦊")
    return text


# ---------- images ----------

def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    """(width, height) from the first frame header (SOFn) before the scan data."""
    i = 2
    while i + 4 <= len(raw):
        if raw[i] != 0xFF:
            return None
        marker = raw[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image, or scan data before any frame header
            return None
        length = int.from_bytes(raw[i + 2:i + 4], "big")
        if length < 2:
            return None
        if marker in _JPEG_FRAMES:
            if i + 9 > len(raw):
                return None
            height, width = struct.unpack(">HH", raw[i + 5:i + 9])
            return width, height
        i += 2 + length
    return None


def image_info(raw: bytes) -> tuple[str, int, int] | None:
    """(MIME type, width, height) read from the image's own header, or None if it is not a PNG, WebP or JPEG."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(raw) >= 24 and raw[12:16] == b"IHDR":
            width, height = struct.unpack(">II", raw[16:24])
            return "image/png", width, height
        return None
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
        chunk = raw[12:16]
        if chunk == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":  # lossy: 14-bit sizes after the start code
            width, height = struct.unpack("<HH", raw[26:30])
            return "image/webp", width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L" and raw[20] == 0x2F:  # lossless: 14 bits each, minus one
            bits = int.from_bytes(raw[21:25], "little")
            return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X":  # extended: the canvas, 24 bits each, minus one
            return "image/webp", int.from_bytes(raw[24:27], "little") + 1, int.from_bytes(raw[27:30], "little") + 1
        return None
    if raw.startswith(b"\xff\xd8\xff"):
        size = _jpeg_size(raw)
        return ("image/jpeg", *size) if size else None
    return None


def check_image(raw: bytes) -> str:
    """The image's real MIME type if it may be an avatar; Invalid otherwise."""
    if len(raw) > IMAGE_MAX_BYTES:
        raise Invalid(f"the image must be at most {IMAGE_MAX_BYTES // 1024} KB (it is {len(raw) // 1024} KB)")
    info = image_info(raw)
    if info is None:
        raise Invalid("the image must be a PNG, WebP or JPEG (SVG and other formats are not accepted)")
    mime, width, height = info
    if not (0 < width <= IMAGE_MAX_SIDE and 0 < height <= IMAGE_MAX_SIDE):
        raise Invalid(f"the image must be at most {IMAGE_MAX_SIDE}×{IMAGE_MAX_SIDE} pixels (it is {width}×{height})")
    return mime


def data_url(raw: bytes) -> str:
    return f"data:{check_image(raw)};base64,{base64.b64encode(raw).decode('ascii')}"


def clean_image(value) -> str:
    """A data:image/(png|webp|jpeg);base64 URL whose bytes are that type, ≤ 48 KB and ≤ 256×256; re-encoded
    canonically (so what is stored is exactly what was checked)."""
    match = _DATA_URL.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise Invalid("avatar.data must be a data:image/png, data:image/webp or data:image/jpeg URL in base64")
    if len(match.group(2)) > (IMAGE_MAX_BYTES + 2) // 3 * 4:
        raise Invalid(f"the image must be at most {IMAGE_MAX_BYTES // 1024} KB")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("avatar.data is not valid base64") from None
    url = data_url(raw)
    if not url.startswith(f"data:{match.group(1)};"):
        raise Invalid(f"avatar.data says {match.group(1)} but the image is {url[5:url.index(';')]}")
    return url


def fetch_image(http: httpx.Client, url: str) -> str:
    """An avatar from an https URL, as a data URL: same rules as a client metadata document fetch (public hosts
    only, port 443, no redirects, no compression, 5 s wall clock), at most 48 KB read, then checked like an upload."""
    try:
        check_url(url, "image_url")
    except UnsafeURL as exc:
        raise Invalid(str(exc)) from None
    try:
        headers = {"Accept": ", ".join(IMAGE_TYPES), "Accept-Encoding": "identity"}
        with deadline(FETCH_DEADLINE), http.stream("GET", url, headers=headers, timeout=FETCH_TIMEOUT) as response:
            if response.status_code != 200:
                raise Invalid(f"image_url answered HTTP {response.status_code} (redirects are not followed)")
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in IMAGE_TYPES:
                raise Invalid("image_url must serve a PNG, WebP or JPEG image (SVG is not accepted)")
            raw = read_capped(response, IMAGE_MAX_BYTES)
    except ValueError as exc:  # compressed, or larger than the cap
        raise Invalid(f"image_url: {exc}") from None
    except httpx.HTTPError:
        raise Invalid("image_url could not be fetched (unreachable, too slow, or not a public host)") from None
    return data_url(raw)


# ---------- the avatar as stored and as shown ----------

def clean_avatar(value) -> dict | None:
    """The avatar to store from an App API body {kind, emoji? | data?}: None for the robot."""
    if not isinstance(value, dict) or value.get("kind") not in AVATAR_KINDS:
        raise Invalid('avatar must be {"kind": "robot"}, {"kind": "emoji", "emoji": …} '
                      'or {"kind": "image", "data": …}')
    kind = value["kind"]
    fields = {"robot": set(), "emoji": {"emoji"}, "image": {"data"}}[kind]
    extra = sorted(set(value) - fields - {"kind"})
    if extra:
        raise Invalid(f"avatar of kind {kind} has no field {extra[0]}")
    if kind == "emoji":
        return {"kind": "emoji", "emoji": clean_emoji(value.get("emoji"))}
    if kind == "image":
        return {"kind": "image", "data": clean_image(value.get("data"))}
    return None


def avatar_json(stored) -> dict:
    """The avatar as the App API returns it (always an object; the robot when none is set)."""
    if isinstance(stored, dict):
        if stored.get("kind") == "emoji" and isinstance(stored.get("emoji"), str):
            return {"kind": "emoji", "emoji": stored["emoji"]}
        if stored.get("kind") == "image" and isinstance(stored.get("data"), str):
            return {"kind": "image", "data": stored["data"]}
    return dict(ROBOT)


def look_brief(agent: dict) -> dict:
    """The look as an agent sees it from set_my_look: an image is described, not sent back (it can be 64 KB)."""
    avatar = avatar_json(agent.get("avatar"))
    if avatar["kind"] == "image":
        data = avatar.pop("data")
        avatar["type"] = data[5:data.index(";")]
        avatar["bytes"] = len(base64.b64decode(data.split(",", 1)[1]))
    return {"name": agent.get("name"), "color": color_key(agent.get("color")), "avatar": avatar}


# ---------- set_my_look (MCP): an agent changes its own look ----------

def own_look(accounts, http: httpx.Client, limiter, who, changes: dict) -> dict:
    """set_my_look for the calling agent only (who = its HostedIdentity: the agent comes from the token, never from
    the arguments). changes: color, emoji, image_url, robot (None / "" / False = not given). Nothing given: the
    current look. Image URLs are fetched at most 10 times per 10 minutes per agent ("look_fetch")."""
    agent = accounts.own_agent(who.user, who.agent_id)
    if agent is None:
        raise NotFound("this agent was disconnected")
    color, emoji, image_url, robot = (changes.get(k) for k in ("color", "emoji", "image_url", "robot"))
    icons = [name for name, given in (("emoji", emoji), ("image_url", image_url), ("robot", robot)) if given]
    if len(icons) > 1:
        raise Invalid(f"pass only one of emoji, image_url and robot (got {' and '.join(icons)})")
    fields: dict = {}
    if color:
        fields["color"] = clean_color(color)
    if emoji:
        fields["avatar"] = {"kind": "emoji", "emoji": clean_emoji(emoji)}
    elif robot:
        fields["avatar"] = None
    elif image_url:
        if not isinstance(image_url, str):
            raise Invalid("image_url must be an https URL")
        if not limiter.allow("look_fetch", who.agent_id):
            raise TooManyFetches("too many image_url fetches; try again in 10 minutes")
        fields["avatar"] = {"kind": "image", "data": fetch_image(http, image_url.strip())}
    if fields:
        agent = accounts.set_look(agent, **fields)
    return look_brief(agent)
