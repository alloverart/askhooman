"""Sample media for tests, built in code: tiny PNG, JPEG and WebP files with real headers, and MP4 files with the box
structure of a real one but no real samples. Shared by the agent look tests and the attachment checks."""

import base64
import struct
import zlib


def png(width: int, height: int, pad: int = 0) -> bytes:
    """A real (tiny, valid) RGBA PNG; `pad` adds an ancillary chunk to make it bigger."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xd0\x60\x9a\xff" * width for _ in range(height))
    extra = chunk(b"tEXt", b"pad\x00" + b"x" * pad) if pad else b""
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def png_of_size(total: int) -> bytes:
    """A 16×16 PNG of exactly `total` bytes (padded with a tEXt chunk: 12 bytes of chunk framing + "pad\\0")."""
    raw = png(16, 16, pad=total - len(png(16, 16)) - 16)
    assert len(raw) == total
    return raw


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    data = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 8
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(data)) + b"WEBP" + b"VP8L" + struct.pack("<I", len(data)) + data


def webp_lossy(width: int, height: int) -> bytes:
    data = b"\x10\x02\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 10
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(data)) + b"WEBP" + b"VP8 " + struct.pack("<I", len(data)) + data


def webp_extended(width: int, height: int) -> bytes:
    data = b"\x10\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(data)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(data)) + data


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + b"\x01" * 64
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + dqt + sof + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x00" * 16 + b"\xff\xd9"


def data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


# ---------- MP4 (ISO BMFF) ----------

MP4_TIMESCALE = 600  # QuickTime's usual movie timescale: durations are in 1/600 s, not milliseconds
_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)  # identity


def box(kind: bytes, payload: bytes = b"", large: bool = False) -> bytes:
    """One ISO BMFF box; `large` writes its size in 64 bits (size field 1, then the size after the type)."""
    if large:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mp4(width: int, height: int, seconds: float, moov_last: bool = False, large_size: bool = False,
        brand: bytes = b"isom") -> bytes:
    """An MP4 laid out like a real one: ftyp; moov with mvhd, an audio trak, then a video trak (its sample
    description nests 8 boxes deep: moov/trak/mdia/minf/stbl/stsd/avc1/avcC); mdat; `moov_last` puts moov after
    mdat, as cameras write it. `large_size`: every box with a 64-bit size, and version 1 (64-bit) mvhd and tkhd.
    `brand` is ftyp's major and only compatible brand."""
    version = 1 if large_size else 0
    duration = round(seconds * MP4_TIMESCALE)

    def b(kind: bytes, *parts: bytes) -> bytes:
        return box(kind, b"".join(parts), large_size)

    def full(flags: int = 0, v: int = 0) -> bytes:  # a FullBox's version and flags
        return struct.pack(">I", (v << 24) | flags)

    if version:
        times = struct.pack(">QQIQ", 0, 0, MP4_TIMESCALE, duration)
    else:
        times = struct.pack(">IIII", 0, 0, MP4_TIMESCALE, duration)
    mvhd = b(b"mvhd", full(v=version), times, struct.pack(">IH10x", 0x10000, 0x100), _MATRIX, bytes(24),
             struct.pack(">I", 3))

    def tkhd(track_id: int, w: int, h: int) -> bytes:
        if version:
            ids = struct.pack(">QQIIQ", 0, 0, track_id, 0, duration)
        else:
            ids = struct.pack(">IIIII", 0, 0, track_id, 0, duration)
        return b(b"tkhd", full(3, version), ids, bytes(8), bytes(8), _MATRIX, struct.pack(">II", w << 16, h << 16))

    def mdia(handler: bytes, name: bytes, minf: bytes) -> bytes:
        mdhd = b(b"mdhd", full(), struct.pack(">IIIIHH", 0, 0, MP4_TIMESCALE, duration, 0x55C4, 0))
        hdlr = b(b"hdlr", full(), bytes(4), handler, bytes(12), name + b"\x00")
        return b(b"mdia", mdhd, hdlr, minf)

    sound = b(b"minf", b(b"smhd", full(), bytes(4)), b(b"stbl", b(b"stsd", full(), struct.pack(">I", 0))))
    avc1 = b(b"avc1", bytes(6), struct.pack(">H", 1), bytes(16), struct.pack(">HH", width, height), bytes(50),
             b(b"avcC", b"\x01\x64\x00\x1f\xff\xe0\x00"))
    picture = b(b"minf", b(b"vmhd", full(1), bytes(8)), b(b"stbl", b(b"stsd", full(), struct.pack(">I", 1), avc1)))
    audio = b(b"trak", tkhd(1, 0, 0), mdia(b"soun", b"SoundHandler", sound))
    video = b(b"trak", tkhd(2, width, height), mdia(b"vide", b"VideoHandler", picture))
    moov = b(b"moov", mvhd, audio, video)
    ftyp = b(b"ftyp", brand, struct.pack(">I", 0x200), brand)
    mdat = b(b"mdat", bytes(64))
    return ftyp + (mdat + moov if moov_last else moov + mdat)
