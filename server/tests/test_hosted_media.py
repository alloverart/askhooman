"""Hosted bridge: the checks on pictures and video in asks (SPEC v5 step 4, "Checks"): each type read from its own
bytes, the size caps, the MP4 box walk against damaged and hostile files, data URLs, link URLs and their providers."""

import random
import struct
import time

import pytest

from server.hosted.media import (
    ASK_MEDIA_MAX, ATTACH_MAX, CAPTION_MAX, DATA_MAX_BYTES, IMAGE_MAX_BYTES, IMAGE_TYPES, MEDIA_MAX_SIDE,
    POSTER_MAX_BYTES, VIDEO_MAX_BYTES, VIDEO_MAX_MS, Checked, check_image, check_media, check_video,
    clean_link_url, decode_data, ext_of, mp4_info, provider_of,
)
from server.rules import Invalid

from .media_samples import box, data_url, jpeg, mp4, png, png_of_size, webp_extended, webp_lossless, webp_lossy

HINT = "convert to PNG, JPEG or WebP; send motion as MP4"  # SPEC, verbatim


def invalid(call, *args) -> str:
    """The message of the Invalid that call(*args) raises (any other exception fails the test)."""
    with pytest.raises(Invalid) as raised:
        call(*args)
    return str(raised.value)


def with_free(raw: bytes, total: int) -> bytes:
    """The MP4 grown to exactly `total` bytes by a trailing `free` box."""
    grown = raw + box(b"free", bytes(total - len(raw) - 8))
    assert len(grown) == total
    return grown


def split(buf: bytes) -> list[bytes]:
    """The 32-bit boxes laid end to end in buf (a known-good sample)."""
    boxes, pos = [], 0
    while pos < len(buf):
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        boxes.append(buf[pos:pos + size])
        pos += size
    return boxes


def resized(one_box: bytes, delta: int) -> bytes:
    """The box with its 32-bit size field off by `delta` (the bytes unchanged)."""
    return struct.pack(">I", len(one_box) + delta) + one_box[4:]


def test_limits_are_the_specs():
    assert (IMAGE_MAX_BYTES, POSTER_MAX_BYTES, VIDEO_MAX_BYTES) == (5_242_880, 1_048_576, 20_971_520)
    assert (VIDEO_MAX_MS, MEDIA_MAX_SIDE, DATA_MAX_BYTES) == (180_000, 4096, 184_320)
    assert (CAPTION_MAX, ATTACH_MAX, ASK_MEDIA_MAX) == (300, 6, 41_943_040)
    assert set(IMAGE_TYPES) == {"image/png", "image/jpeg", "image/webp"}


# ---------- images ----------

def test_image_types_by_bytes():
    for raw, mime, width, height in ((png(300, 200), "image/png", 300, 200),
                                     (jpeg(1920, 1080), "image/jpeg", 1920, 1080),
                                     (webp_lossless(640, 480), "image/webp", 640, 480),
                                     (webp_lossy(800, 600), "image/webp", 800, 600),
                                     (webp_extended(4096, 4096), "image/webp", 4096, 4096)):
        checked = check_media(raw)
        assert checked == Checked(mime, width, height, len(raw), None, raw), mime
        assert checked.raw is raw  # the bytes are kept, not copied
        assert check_image(raw) == checked
        assert check_media(raw, declared=mime) == checked
    assert "raw" not in repr(check_media(png(2, 2)))  # a 20 MB payload never lands in a log line


def test_declared_type_must_match():
    for raw, declared, real in ((png(8, 8), "image/jpeg", "image/png"), (jpeg(8, 8), "image/png", "image/jpeg"),
                                (webp_lossless(8, 8), "image/png", "image/webp"),
                                (mp4(640, 360, 12), "image/png", "video/mp4"), (png(8, 8), "video/mp4", "image/png"),
                                (png(8, 8), "image/gif", "image/png")):
        message = invalid(check_media, raw, declared)
        assert declared in message and real in message, message


def test_image_limits():
    assert check_media(png_of_size(IMAGE_MAX_BYTES)).bytes == IMAGE_MAX_BYTES
    assert "5 MB" in invalid(check_media, png_of_size(IMAGE_MAX_BYTES + 1))
    assert check_media(png(4096, 1)).width == 4096
    for raw in (png(4097, 1), png(1, 4097), webp_extended(4097, 10), jpeg(10, 4097), png(0, 5)):
        assert "4096×4096" in invalid(check_media, raw)
    whole = png(4, 4)
    for raw in (whole[:20], whole[:8], jpeg(10, 10)[:30], webp_extended(10, 10)[:24], b"", b"\x89PNG"):
        assert HINT in invalid(check_media, raw), raw


def test_refused_formats():
    heic = box(b"ftyp", b"heic" + bytes(4) + b"mif1heic") + box(b"meta", bytes(32)) + box(b"mdat", bytes(64))
    avif = box(b"ftyp", b"avif" + bytes(4) + b"avifmif1miaf") + box(b"meta", bytes(32)) + box(b"mdat", bytes(64))
    for raw in (b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>",
                b"<?xml version='1.0'?><svg/>", b"GIF89a\x10\x00\x10\x00" + bytes(64), b"GIF87a" + bytes(64),
                heic, avif, b"BM" + bytes(64), b"%PDF-1.7\n" + bytes(64), b"II*\x00" + bytes(64)):
        assert HINT in invalid(check_media, raw), raw[:12]
    assert HINT in invalid(check_image, heic)  # a poster too


def test_poster_cap():
    assert check_image(png_of_size(POSTER_MAX_BYTES), POSTER_MAX_BYTES).bytes == POSTER_MAX_BYTES
    assert "1 MB" in invalid(check_image, png_of_size(POSTER_MAX_BYTES + 1), POSTER_MAX_BYTES)
    assert HINT in invalid(check_image, mp4(640, 360, 12), POSTER_MAX_BYTES)  # a poster is a still


# ---------- MP4 ----------

def test_mp4_moov_first_and_last():
    for raw in (mp4(640, 360, 12), mp4(640, 360, 12, moov_last=True)):
        assert mp4_info(raw) == (640, 360, 12000)
        assert check_media(raw) == Checked("video/mp4", 640, 360, len(raw), 12000, raw)
        assert check_video(raw) == check_media(raw, declared="video/mp4")
    for brand in (b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V "):
        assert check_media(mp4(1920, 1080, 7.5, brand=brand)).duration_ms == 7500, brand
    # a major brand we don't know is fine when a compatible one is an MP4 brand (DASH, iPhone exports)
    _, moov, mdat = split(mp4(1080, 1920, 3))
    dash = box(b"ftyp", b"dash" + bytes(4) + b"iso6avc1mp41") + moov + mdat
    assert check_media(dash).height == 1920


def test_mp4_64bit_sizes():
    for raw in (mp4(640, 360, 12, large_size=True), mp4(640, 360, 12, moov_last=True, large_size=True)):
        assert struct.unpack(">I4sQ", raw[:16])[0] == 1  # really 64-bit
        assert mp4_info(raw) == (640, 360, 12000)
        assert check_media(raw).duration_ms == 12000


def _refusals():
    ftyp, moov, mdat = split(mp4(640, 360, 12))
    mvhd, audio, video = split(moov[8:])
    nested = moov
    for _ in range(6):
        nested = box(b"moov", nested)  # moov in moov, 7 deep, the real movie innermost
    size_0 = struct.pack(">I4s", 0, b"free")
    cases = [
        ("no moov", ftyp + mdat, "movie"),
        ("181 s", mp4(640, 360, 181), "180 s"),
        ("QuickTime", mp4(640, 360, 12, brand=b"qt  "), "remux it to MP4"),
        ("size 0 in the middle", ftyp + size_0 + moov + mdat, "movie"),
        ("size 0 inside moov", ftyp + box(b"moov", mvhd + audio + video + size_0) + mdat, "movie"),
        ("size past the end", ftyp + moov + resized(mdat, 1), "movie"),
        ("child past its parent", ftyp + box(b"moov", mvhd + audio + resized(video, 1)) + mdat, "movie"),
        ("64-bit size past the end", ftyp + moov + mdat + struct.pack(">I4sQ", 1, b"free", 2**64 - 1), "movie"),
        # sizes smaller than their own header: the next box would start inside this one's header
        ("size below the header", ftyp + moov + mdat + struct.pack(">I", 4) + box(b"free"), "movie"),
        ("64-bit size below the header", ftyp + moov + mdat + struct.pack(">I4sQ", 1, b"free", 8), "movie"),
        ("cut mid-header", ftyp + moov + mdat + b"\x00\x00\x00", "movie"),
        ("10 001 boxes", ftyp + moov + mdat + box(b"free") * 10_001, "movie"),
        ("nesting 7", ftyp + nested + mdat, "movie"),
        ("no video track", ftyp + box(b"moov", mvhd + audio) + mdat, "movie"),
        ("timescale 0", ftyp + box(b"moov", mvhd[:20] + bytes(4) + mvhd[24:] + audio + video) + mdat, "movie"),
    ]
    return [pytest.param(raw, words, id=name) for name, raw, words in cases]


@pytest.mark.parametrize("raw, words", _refusals())
def test_mp4_refusals(raw, words):
    started = time.perf_counter()
    message = invalid(check_media, raw)
    info = mp4_info(raw)
    assert time.perf_counter() - started < 0.5
    assert words in message, message
    if words == "movie":  # refused by the box walk itself
        assert info is None


def test_video_limits():
    good = mp4(640, 360, 12)
    assert check_media(with_free(good, VIDEO_MAX_BYTES)).bytes == VIDEO_MAX_BYTES
    assert "20 MB" in invalid(check_media, with_free(good, VIDEO_MAX_BYTES + 1))
    assert check_media(mp4(4096, 4096, 180)).duration_ms == VIDEO_MAX_MS  # exactly at the limits
    assert "180 s" in invalid(check_media, mp4(640, 360, 180.01))
    for raw in (mp4(4097, 360, 12), mp4(640, 4097, 12), mp4(640, 0, 12)):
        assert "4096×4096" in invalid(check_media, raw)
    assert mp4_info(good + box(b"free") * 9_900) == (640, 360, 12000)  # the box budget is no tighter than it says


def test_damaged_mp4_only_ever_raises_invalid():
    rng = random.Random(4)
    samples = [mp4(640, 360, 12), mp4(640, 360, 12, moov_last=True, large_size=True)]
    damaged = [raw[:n] for raw in samples for n in range(len(raw))]
    for _ in range(3000):
        raw = bytearray(rng.choice(samples))
        for _ in range(rng.randint(1, 4)):
            raw[rng.randrange(len(raw))] = rng.choice((0, 1, 0xFF, rng.randrange(256)))
        damaged.append(bytes(raw))
    started = time.perf_counter()
    for raw in damaged:
        try:
            check_media(raw)
        except Invalid:
            pass
        info = mp4_info(raw)
        assert info is None or len(info) == 3
    assert time.perf_counter() - started < 10


# ---------- data URLs ----------

def test_data_urls():
    raw = png_of_size(DATA_MAX_BYTES)
    checked = decode_data(data_url("image/png", raw))
    assert (checked.mime, checked.bytes, checked.raw) == ("image/png", DATA_MAX_BYTES, raw)
    assert decode_data("  " + data_url("image/jpeg", jpeg(20, 10)) + "\n").mime == "image/jpeg"
    assert decode_data(data_url("image/webp", webp_lossy(20, 10))).width == 20
    assert "180 KB" in invalid(decode_data, data_url("image/png", png_of_size(DATA_MAX_BYTES + 1)))
    assert "base64" in invalid(decode_data, "data:image/png;base64,iVBORw0KGgo")  # padding missing
    for value in ("data:image/png;base64,***", "data:image/png;base64,iVBO=Rw0K", "data:image/png;base64"):
        assert invalid(decode_data, value)
    for value in ("data:image/svg+xml;base64,PHN2Zy8+", data_url("image/gif", b"GIF89a" + bytes(40)),
                  "https://example.com/a.png", "data:image/png,rawbytes", data_url("video/mp4", mp4(64, 64, 1)), "",
                  None, 42):
        assert HINT in invalid(decode_data, value), value
    message = invalid(decode_data, data_url("image/png", jpeg(10, 10)))
    assert "image/png" in message and "image/jpeg" in message
    assert HINT in invalid(decode_data, data_url("image/png", b"GIF89a" + bytes(40)))


# ---------- link URLs and providers ----------

def test_link_urls():
    too_long = "https://example.com/" + "a" * 1981
    assert len(too_long) == 2001
    for url in ("https://drive.google.com/file/d/x", "https://www.figma.com/file/abc?node-id=1:2#frame",
                "https://youtu.be/dQw4w9WgXcQ", "https://example.com:443/a", "https://пример.рф/путь"):
        assert clean_link_url(url, "url") == url
    assert clean_link_url("  https://drive.google.com/file/d/x\n", "url") == "https://drive.google.com/file/d/x"
    for url, words in (("http://example.com/x", "https"), ("javascript:alert(1)", "https"),
                       ("https://1.2.3.4/x", "IP address"), ("https://8.8.8.8/x", "IP address"),
                       ("https://[2001:4860:4860::8888]/x", "IP address"), ("https://1.2.3.4./x", "IP address"),
                       ("https://0x7f.1/x", "IP address"), ("https://127.0.0.1/x", "non-public"),
                       ("https://u:p@h.com", "user name"), ("https://u@h.com", "user name"),
                       (too_long, "2000"), ("https://localhost/", "public host"),
                       ("https://intranet/", "public host"), ("https://example.com:8443/", "443"),
                       ("https://example.com/a b", "spaces"), ("https://exa\tmple.com/", "spaces"),
                       ("https://good.com\\.evil.com/", "backslash"), ("", "https"), (None, "string")):
        message = invalid(clean_link_url, url, "source_url")
        assert words in message and "source_url" in message, (url, message)


def test_providers():
    for url, provider in (("https://drive.google.com/file/d/x/view", "drive"),
                          ("https://docs.google.com/document/d/x/edit", "docs"),
                          ("https://docs.google.com/spreadsheets/d/x", "sheets"),
                          ("https://docs.google.com/presentation/d/x", "slides"),
                          ("https://docs.google.com/forms/d/x", "docs"), ("https://docs.google.com/", "docs"),
                          ("https://youtube.com/watch?v=x", "youtube"),
                          ("https://www.youtube.com/watch?v=x", "youtube"),
                          ("https://m.youtube.com/watch?v=x", "youtube"), ("https://youtu.be/x", "youtube"),
                          ("https://figma.com/file/x", "figma"), ("https://www.figma.com/design/x", "figma"),
                          ("https://DRIVE.Google.com/file/d/x", "drive"), ("https://example.com/", "web"),
                          ("https://drive.google.com.evil.com/", "web"), ("https://notyoutube.com/", "web"),
                          ("https://www.docs.google.com/spreadsheets/d/x", "web")):
        assert provider_of(url) == provider, url


def test_extensions():
    types = ("image/png", "image/jpeg", "image/webp", "video/mp4")
    assert [ext_of(mime) for mime in types] == ["png", "jpg", "webp", "mp4"]
    assert "image/gif" in invalid(ext_of, "image/gif")
