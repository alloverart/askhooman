"""Hosted bridge: an attachment given by `url`, fetched once (SPEC v5 step 4, "How the bytes get in"): the type
from the bytes and the Content-Type agreeing, Google Drive refused before any connection, no redirects, no
compression, at most 20 MB read, a deadline between chunks, and only public addresses connected to."""

import gzip
import socket
import time
import tracemalloc

import httpcore
import httpx
import pytest

from server.hosted.media import IMAGE_MAX_BYTES, VIDEO_MAX_BYTES
from server.hosted.media_fetch import DRIVE_HOSTS, FETCH_DEADLINE, fetch_media
from server.hosted.safe_http import PublicOnlyTransport, public_client
from server.rules import Invalid

from .media_samples import box, jpeg, mp4, png, webp_lossless

URL = "https://pics.example/cat.png"
DRIVE_REFUSED = ("AskHooman can't open files in Google Drive: download it and use ask_upload, or attach the link "
                 "with link=true")  # Global Constraints, verbatim
HINT = "convert to PNG, JPEG or WebP; send motion as MP4"
MB = 1024 * 1024


def invalid(call, *args, **kwargs) -> str:
    """The message of the Invalid that call(...) raises (any other exception fails the test)."""
    with pytest.raises(Invalid) as raised:
        call(*args, **kwargs)
    return str(raised.value)


def soon() -> float:
    return time.monotonic() + 30


def internet(google, follow_redirects: bool = False) -> tuple[httpx.Client, list[httpx.Request]]:
    """A plain client (none of public_client's headers, so the fetch's own are what is sent) on Fake Google's public
    internet, and the list of requests it sends."""
    seen: list[httpx.Request] = []
    inner = google.public_transport()

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner.handle_request(request)

    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=follow_redirects), seen


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def streaming(chunks, content_type: str = "video/mp4", on_chunk=lambda: None,
              headers: dict | None = None) -> tuple[httpx.Client, list[int]]:
    """A client whose every response streams `chunks()` one by one (calling on_chunk before each), and the sizes
    of the chunks the server got to send."""
    sent: list[int] = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in chunks():
                on_chunk()
                sent.append(len(chunk))
                yield chunk

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": content_type, **(headers or {})}, stream=Body())

    return httpx.Client(transport=httpx.MockTransport(handle)), sent


def test_fetches_png(google):
    raw = png(64, 48)
    google.publish(URL, raw, headers={"Content-Type": "image/png"})
    http, seen = internet(google)
    checked = fetch_media(http, URL, until=soon())
    assert (checked.mime, checked.width, checked.height, checked.bytes, checked.duration_ms) == \
        ("image/png", 64, 48, len(raw), None)
    assert checked.raw == raw
    [request] = seen
    assert request.method == "GET" and request.headers["accept-encoding"] == "identity"
    assert request.headers["accept"] == "image/png, image/jpeg, image/webp, video/mp4"
    assert fetch_media(http, f"  {URL}\n", until=soon()).raw == raw  # an agent's stray whitespace


def test_fetches_each_type(google):
    for path, raw, mime in (("a.jpg", jpeg(30, 20), "image/jpeg"), ("a.webp", webp_lossless(30, 20), "image/webp"),
                            ("a.mp4", mp4(1280, 720, 12.5), "video/mp4")):
        google.publish(f"https://pics.example/{path}", raw, headers={"Content-Type": mime})
        checked = fetch_media(internet(google)[0], f"https://pics.example/{path}", until=soon())
        assert checked.mime == mime and checked.raw == raw
    assert checked.duration_ms == 12_500 and (checked.width, checked.height) == (1280, 720)


def test_content_type_must_match_bytes(google):
    http, seen = internet(google)
    google.publish(URL, jpeg(30, 20), headers={"Content-Type": "image/png"})
    assert "declared image/png but its bytes are image/jpeg" in invalid(fetch_media, http, URL, until=soon())
    # parameters and case don't matter: the bare type is compared
    for content_type in ("image/png; charset=binary", "IMAGE/PNG", " image/png ;q=1"):
        google.publish(URL, png(8, 8), headers={"Content-Type": content_type})
        assert fetch_media(http, URL, until=soon()).mime == "image/png"
    # a type that isn't allowed is refused from the header, with the hint
    for content_type in ("text/html; charset=utf-8", "image/gif", "image/svg+xml", "application/octet-stream", ""):
        google.publish(URL, png(8, 8), headers={"Content-Type": content_type})
        message = invalid(fetch_media, http, URL, until=soon())
        assert HINT in message and message.startswith("url ")
    google.publish(URL, b"<html>", headers={"Content-Type": "text/html; charset=utf-8"})
    assert "(it sent text/html)" in invalid(fetch_media, http, URL, until=soon())
    google.publish(URL, b"<html>", headers={"Content-Type": "text/html\x7f<script>"})
    assert "<script>" not in invalid(fetch_media, http, URL, until=soon())  # only a well-formed type is echoed
    assert len(seen) == 11 and all(r.headers["accept-encoding"] == "identity" for r in seen)
    # damaged bytes under an allowed type: the media checks speak
    google.publish(URL, b"\x89PNG not really", headers={"Content-Type": "image/png"})
    assert HINT in invalid(fetch_media, http, URL, until=soon())


@pytest.mark.parametrize("url", [
    "https://drive.google.com/file/d/1abc/view?usp=sharing",
    "https://docs.google.com/document/d/1abc/edit",
    "https://docs.google.com/uc?export=download&id=1abc",
    "https://DRIVE.Google.COM/file/d/1abc",
    "https://drive.google.com./file/d/1abc",  # trailing dot
    "https://drive.google.com:443/file/d/1abc",
    "https://drive。google。com/file/d/1abc",  # 。 ideographic full stop
    "https://docs．google．com/document/d/1abc",  # ． full-width full stop
    "https://drive｡google｡com/file/d/1abc",  # ｡ half-width ideographic full stop
    "https://drive.google.com。/file/d/1abc",  # a Unicode trailing dot
    "https://ｄｒｉｖｅ.google.com/file/d/1abc",  # full-width letters
    "http://drive.google.com/file/d/1abc",
])
def test_drive_hosts_refused_before_connecting(google, url):
    http, seen = internet(google)
    google.publish(url, png(8, 8), headers={"Content-Type": "image/png"})
    assert invalid(fetch_media, http, url, until=soon()) == DRIVE_REFUSED
    assert seen == []


def test_drive_lookalikes_are_fetched(google):
    assert DRIVE_HOSTS == ("drive.google.com", "docs.google.com")  # exact hosts, no subdomains
    http, seen = internet(google)
    for url in ("https://drive.google.com.pics.example/cat.png", "https://notdrive.google.com/cat.png",
                "https://pics.example/drive.google.com/cat.png"):
        google.publish(url, png(8, 8), headers={"Content-Type": "image/png"})
        assert fetch_media(http, url, until=soon()).mime == "image/png"
    assert len(seen) == 3


def test_unsafe_urls_refused_before_connecting(google):
    http, seen = internet(google)
    for url, said in (("http://pics.example/cat.png", "https"),
                      ("https://pics.example:8443/cat.png", "443"),
                      ("https://10.0.0.1/cat.png", "non-public"),
                      ("https://localhost/cat.png", "public host"),
                      ("https://me:pw@pics.example/cat.png", "user name"),
                      (f"https://{'a' * 2000}.example/", "2000"),
                      ("https://xn--a.example/cat.png", "not a valid URL"),  # httpx's IDNA decoder refuses it
                      ("https://a..example/cat.png", "valid host name"),  # an empty label
                      (f"https://{'a' * 64}.example/cat.png", "valid host name")):  # a label over 63 chars
        assert said in invalid(fetch_media, http, url, until=soon())
    assert "must be a string" in invalid(fetch_media, http, None, until=soon())
    assert seen == []


def test_redirect_refused(google):
    http, seen = internet(google, follow_redirects=True)  # the fetch itself must not follow
    google.publish("https://pics.example/real.png", png(8, 8), headers={"Content-Type": "image/png"})
    google.publish(URL, b"", status=302, headers={"Location": "https://pics.example/real.png"})
    message = invalid(fetch_media, http, URL, until=soon())
    assert "HTTP 302" in message and "redirects are not followed" in message
    assert [str(r.url) for r in seen] == [URL]
    google.publish(URL, b"gone", status=404, headers={"Content-Type": "image/png"})
    assert "HTTP 404" in invalid(fetch_media, http, URL, until=soon())


def test_gzip_refused(google):
    http, _ = internet(google)
    google.publish(URL, gzip.compress(png(8, 8)), headers={"Content-Type": "image/png", "Content-Encoding": "gzip"})
    assert "compressed responses are refused (Content-Encoding: gzip)" in invalid(fetch_media, http, URL,
                                                                                    until=soon())


def test_oversize_body(google):
    http, _ = internet(google)
    google.publish(URL, b"\x00" * (VIDEO_MAX_BYTES + 1), headers={"Content-Type": "video/mp4"})
    assert "larger than 20 MB" in invalid(fetch_media, http, URL, until=soon())
    # exactly the cap is read whole (the media checks then decide)
    raw = mp4(640, 360, 3.0)
    raw += box(b"free", bytes(VIDEO_MAX_BYTES - len(raw) - 8))
    google.publish(URL, raw, headers={"Content-Type": "video/mp4"})
    assert fetch_media(http, URL, until=soon()).bytes == VIDEO_MAX_BYTES
    # a server that would send 64 MB is cut off right after the cap
    http, sent = streaming(lambda: (b"\x00" * MB for _ in range(64)))
    assert "larger than 20 MB" in invalid(fetch_media, http, URL, until=soon())
    assert sum(sent) == VIDEO_MAX_BYTES + MB


def test_image_cap_applies_once_the_type_is_known():
    """A body declared as an image is cut at 5 MB, not at the video's 20 MB."""
    http, sent = streaming(lambda: (b"\x00" * MB for _ in range(6)), content_type="image/png")
    message = invalid(fetch_media, http, URL, until=soon())
    assert "image" in message and "larger than 5 MB" in message
    assert sum(sent) == IMAGE_MAX_BYTES + MB  # the cap and the one chunk that crossed it


@pytest.mark.parametrize("content_type, length, said", [
    ("image/png", IMAGE_MAX_BYTES + 1, "larger than 5 MB"),
    ("image/webp", VIDEO_MAX_BYTES, "larger than 5 MB"),
    ("video/mp4", VIDEO_MAX_BYTES + 1, "larger than 20 MB"),
])
def test_content_length_over_the_cap_refused_before_reading(content_type, length, said):
    http, sent = streaming(lambda: iter([b"\x00"]), content_type, headers={"Content-Length": str(length)})
    assert said in invalid(fetch_media, http, URL, until=soon())
    assert sent == []  # not one chunk read
    # a Content-Length that isn't a plain number is left to the read cap
    http, sent = streaming(lambda: iter([png(8, 8)]), "image/png", headers={"Content-Length": "9" * 5000})
    assert fetch_media(http, URL, until=soon()).mime == "image/png"


def test_a_fetch_holds_one_copy_of_the_body():
    """4 fetch slots × 20 MB is the instance's media budget: the body is not copied once read."""
    raw = mp4(640, 360, 3.0)
    raw += box(b"free", bytes(VIDEO_MAX_BYTES - len(raw) - 8))
    view = memoryview(raw)
    http, _ = streaming(lambda: (bytes(view[i:i + MB]) for i in range(0, len(raw), MB)))
    tracemalloc.start()
    try:
        checked = fetch_media(http, URL, until=soon())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert checked.raw == raw and type(checked.raw) is bytes
    # one buffer (grown by ~1/8) and a chunk: ~23.5 MB; a full copy at the end made it ~42.5 MB
    assert peak < VIDEO_MAX_BYTES * 1.5


def test_max_bytes_caps_the_read_below_the_kind():
    """max_bytes (what an ask_create still has room for) lowers the read cap: a Content-Length over it is refused
    before reading, a body without one is cut where it crosses it; the kind's cap still says its own message."""
    http, sent = streaming(lambda: iter([b"\x00"]), headers={"Content-Length": str(3 * MB)})
    assert "room" in invalid(fetch_media, http, URL, until=soon(), max_bytes=2 * MB)
    assert sent == []  # not one chunk read
    http, sent = streaming(lambda: (b"\x00" * MB for _ in range(20)))
    assert "room" in invalid(fetch_media, http, URL, until=soon(), max_bytes=2 * MB + 1)
    assert sum(sent) == 3 * MB  # the room and the one chunk that crossed it, not the 20 MB
    http, sent = streaming(lambda: iter([b"\x00"]), headers={"Content-Length": str(VIDEO_MAX_BYTES + 1)})
    assert "larger than 20 MB" in invalid(fetch_media, http, URL, until=soon(), max_bytes=MB)
    raw = mp4(64, 48, 1.0)
    http, _ = streaming(lambda: iter([raw]), headers={"Content-Length": str(len(raw))})
    assert fetch_media(http, URL, until=soon(), max_bytes=len(raw)).raw == raw  # exactly the room fits


def test_slow_drip_past_deadline():
    clock = FakeClock()

    def tick():
        clock.now += 1

    http, sent = streaming(lambda: (b"\x00" * 1024 for _ in range(100)), on_chunk=tick)
    assert "too slow" in invalid(fetch_media, http, URL, until=30, clock=clock)
    assert len(sent) == 30  # stopped at the deadline, not at the end of the body
    # whatever the caller's budget, one fetch gets at most 30 s
    clock.now, sent[:] = 1000.0, []
    assert "too slow" in invalid(fetch_media, http, URL, until=float("inf"), clock=clock)
    assert len(sent) == FETCH_DEADLINE == 30
    # a budget already spent: no request at all
    clock.now, sent[:] = 50.0, []
    assert "too slow" in invalid(fetch_media, http, URL, until=30, clock=clock)
    assert sent == []


def test_private_and_mixed_dns(monkeypatch):
    opened = []

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        opened.append(host)
        raise httpcore.ConnectError("no network in tests")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    for addresses in (["10.0.0.1"], ["93.184.216.34", "10.0.0.1"]):
        asked = []

        def resolve(host, port, type=0, addresses=addresses, asked=asked):
            asked.append(host)
            return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]

        http = public_client(transport=PublicOnlyTransport(resolve=resolve))
        assert "could not be fetched" in invalid(fetch_media, http, "https://innocent.example/cat.png", until=soon())
        assert asked == ["innocent.example"]  # resolved at connect time …
    assert opened == []  # … and refused before any socket was opened
