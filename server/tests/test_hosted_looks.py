"""Hosted bridge: agents' looks (SPEC v4 "Agent look"): palette colours given at connection and to older agents,
the App API to change colour and icon, and set_my_look for an agent's own look."""

import base64
import struct
import zlib

import pytest

from server.hosted.looks import AGENT_COLORS, image_info, is_one_emoji

from .hosted_support import DENIS_SUB, OLGA_EMAIL, OLGA_SUB, connect, id_token, mcp, register, tool_error, tool_result

IMAGE_URL = "https://img.example.com/fox.png"


def png(width: int, height: int, pad: int = 0) -> bytes:
    """A real (tiny, valid) RGBA PNG; `pad` adds an ancillary chunk to make it bigger."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xd0\x60\x9a\xff" * width for _ in range(height))
    extra = chunk(b"tEXt", b"pad\x00" + b"x" * pad) if pad else b""
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


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


def agents(hosted, google, sub=DENIS_SUB) -> list[dict]:
    response = hosted.get("/api/agents", headers=id_token(google, sub=sub))
    assert response.status_code == 200, response.text
    return response.json()["agents"]


def new_key(hosted, google, name: str) -> dict:
    response = hosted.post("/api/agents/keys", json={"name": name}, headers=id_token(google))
    assert response.status_code == 201, response.text
    return response.json()


def patch(hosted, google, agent_id: str, body: dict, sub=DENIS_SUB):
    return hosted.patch(f"/api/agents/{agent_id}", json=body, headers=id_token(google, sub=sub))


# ---------- pure parts ----------

def test_image_headers_give_type_and_size():
    assert image_info(png(256, 200)) == ("image/png", 256, 200)
    assert image_info(webp_lossless(256, 128)) == ("image/webp", 256, 128)
    assert image_info(webp_lossy(64, 48)) == ("image/webp", 64, 48)
    assert image_info(webp_extended(300, 20)) == ("image/webp", 300, 20)
    assert image_info(jpeg(120, 90)) == ("image/jpeg", 120, 90)
    for raw in (b"GIF89a\x10\x00\x10\x00", b"<svg xmlns='http://www.w3.org/2000/svg'/>", png(4, 4)[:20],
                b"\xff\xd8\xff\xda\x00\x08", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b""):
        assert image_info(raw) is None, raw


def test_one_emoji():
    for text in ("🦊", "🤖", "👍🏽", "🧑🏽‍💻", "❤️", "❤️‍🔥", "🏳️‍🌈", "🇺🇸", "1️⃣", "#⃣", "☕", "🫎"):
        assert is_one_emoji(text), text
    for text in ("a", "AB", "1", "🦊🦊", "🇺", "🦊 ", "\u200d🦊", "🦊\u200d", "🙂x", "\u200b"):
        assert not is_one_emoji(text), repr(text)


# ---------- colours ----------

def test_new_agents_get_every_colour_before_any_repeats(hosted, google):
    assert connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]
    colors = [agents(hosted, google)[0]["color"]] + [new_key(hosted, google, f"Bot {n}")["agent"]["color"]
                                                     for n in range(12)]
    assert colors == [*AGENT_COLORS, "mint"]  # the 13th repeats the first
    listed = agents(hosted, google)
    assert sorted(a["color"] for a in listed) == sorted(colors)
    assert all(a["avatar"] == {"kind": "robot"} for a in listed)
    sky = next(a for a in listed if a["color"] == "sky")
    assert hosted.delete(f"/api/agents/{sky['id']}", headers=id_token(google)).status_code == 200
    assert new_key(hosted, google, "After revoke")["agent"]["color"] == "sky"  # only active agents count
    # another user's agents start from the first colour
    olga = connect(hosted, google, register(hosted)["client_id"], agent="Ogilvy", sub=OLGA_SUB, email=OLGA_EMAIL)
    assert olga["access_token"] and [a["color"] for a in agents(hosted, google, OLGA_SUB)] == ["mint"]


def test_agents_from_before_colours_get_one_when_listed(hosted, google):
    ids = [new_key(hosted, google, name)["agent"]["id"] for name in ("Allen", "Ogilvy", "Hamilton", "Sonya")]
    store = hosted.store
    for n, agent_id in enumerate(ids):  # as stored before this change: no color, no avatar
        doc = store.get("agents", agent_id)
        doc.pop("color")
        doc.pop("avatar")
        doc["created_at"] = f"2026-10-0{n + 1}T12:00:00.000Z"
        store.put("agents", agent_id, doc)
    store.update("agents", ids[1], {"color": "mint"})  # one already has a colour: it stays and counts
    store.update("agents", ids[3], {"color": "no-such-colour"})  # an unknown key counts as none
    listed = agents(hosted, google)
    assert [a["color"] for a in listed] == ["sky", "mint", "peach", "pink"]
    assert [store.get("agents", i)["color"] for i in ids] == ["sky", "mint", "peach", "pink"]  # kept
    assert [a["color"] for a in agents(hosted, google)] == ["sky", "mint", "peach", "pink"]  # once
    assert all(a["avatar"] == {"kind": "robot"} for a in listed)


# ---------- the App API ----------

def test_user_sets_colour_and_icon(hosted, google):
    agent = new_key(hosted, google, "Allen")["agent"]
    agent_id = agent["id"]
    assert agent["color"] == "mint"
    changed = patch(hosted, google, agent_id, {"color": "plum", "avatar": {"kind": "emoji", "emoji": " 🦊 "}})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["agent"]["color"], changed.json()["agent"]["avatar"]) == (
        "plum", {"kind": "emoji", "emoji": "🦊"})
    for mime, raw in (("image/png", png(256, 256)), ("image/webp", webp_lossless(200, 256)),
                      ("image/jpeg", jpeg(64, 64))):
        url = data_url(mime, raw)
        response = patch(hosted, google, agent_id, {"avatar": {"kind": "image", "data": url}})
        assert response.status_code == 200, (mime, response.text)
        assert response.json()["agent"]["avatar"] == {"kind": "image", "data": url}
    listed = agents(hosted, google)[0]
    assert listed["color"] == "plum" and listed["avatar"]["data"].startswith("data:image/jpeg;base64,")
    robot = patch(hosted, google, agent_id, {"avatar": {"kind": "robot"}})
    assert robot.json()["agent"]["avatar"] == {"kind": "robot"}
    assert hosted.store.get("agents", agent_id)["avatar"] is None  # the robot is the default: nothing stored
    assert patch(hosted, google, agent_id, {"color": "sky"}, sub=OLGA_SUB).status_code == 404  # not hers


@pytest.mark.parametrize("body, words", [
    ({"color": "teal"}, "color must be one of"),
    ({"color": None}, "color must be one of"),
    ({"avatar": None}, "avatar must be"),
    ({"avatar": {"kind": "svg"}}, "avatar must be"),
    ({"avatar": {"kind": "robot", "emoji": "🦊"}}, "has no field emoji"),
    ({"avatar": {"kind": "emoji", "emoji": "fox"}}, "exactly one emoji"),
    ({"avatar": {"kind": "emoji", "emoji": "🦊🦊"}}, "exactly one emoji"),
    ({"avatar": {"kind": "emoji", "emoji": "👨‍👩‍👧‍👦"}}, "at most 16 bytes"),
    ({"avatar": {"kind": "emoji"}}, "one emoji"),
    ({"avatar": {"kind": "image", "data": "data:image/svg+xml;base64,PHN2Zy8+"}}, "data:image/png"),
    ({"avatar": {"kind": "image", "data": "https://example.com/a.png"}}, "data:image/png"),
    ({"avatar": {"kind": "image", "data": "data:image/png;base64,***"}}, "data:image/png"),
    ({"avatar": {"kind": "image", "data": "data:image/png;base64,iVBORw0KGgo"}}, "base64"),
    ({"avatar": {"kind": "image", "data": data_url("image/png", b"GIF89a" + b"\x00" * 40)}}, "PNG, WebP or JPEG"),
    ({"avatar": {"kind": "image", "data": data_url("image/png", jpeg(10, 10))}}, "but the image is image/jpeg"),
    ({"avatar": {"kind": "image", "data": data_url("image/png", png(257, 10))}}, "256×256"),
    ({"avatar": {"kind": "image", "data": data_url("image/webp", webp_extended(10, 300))}}, "256×256"),
    ({"avatar": {"kind": "image", "data": data_url("image/png", png(16, 16, pad=49 * 1024))}}, "48 KB"),
    ({"look": "fox"}, "look"),
])
def test_bad_looks_are_refused(hosted, google, body, words):
    agent_id = new_key(hosted, google, "Allen")["agent"]["id"]
    before = hosted.store.get("agents", agent_id)
    response = patch(hosted, google, agent_id, body)
    assert response.status_code == 422, response.text
    assert words in response.json()["detail"]
    assert hosted.store.get("agents", agent_id) == before


def test_a_refused_change_writes_no_part_of_the_look(hosted, google):
    agent_id = new_key(hosted, google, "Allen")["agent"]["id"]
    refused = patch(hosted, google, agent_id, {"color": "plum", "webhook_url": "http://10.0.0.1/hook"})
    assert refused.status_code == 422 and hosted.store.get("agents", agent_id)["color"] == "mint"
    refused = patch(hosted, google, agent_id, {"name": "Allen 2", "avatar": {"kind": "emoji", "emoji": "no"}})
    assert refused.status_code == 422 and hosted.store.get("agents", agent_id)["name"] == "Allen"


# ---------- set_my_look ----------

def test_an_agent_sets_its_own_look_only(hosted, google):
    allen = connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]
    ogilvy = connect(hosted, google, register(hosted)["client_id"], agent="Ogilvy")["access_token"]
    assert tool_result(mcp(hosted, allen, "set_my_look")) == {"name": "Allen", "color": "mint",
                                                              "avatar": {"kind": "robot"}}
    look = tool_result(mcp(hosted, allen, "set_my_look", {"color": "cherry", "emoji": "🦊"}))
    assert look == {"name": "Allen", "color": "cherry", "avatar": {"kind": "emoji", "emoji": "🦊"}}
    # extra arguments can't point it at another agent: the agent comes from the token
    tool_result(mcp(hosted, allen, "set_my_look", {"color": "leaf", "agent": "ogilvy", "id": "x"}))
    by_slug = {a["slug"]: a for a in agents(hosted, google)}
    assert (by_slug["allen"]["color"], by_slug["allen"]["avatar"]["emoji"]) == ("leaf", "🦊")
    assert (by_slug["ogilvy"]["color"], by_slug["ogilvy"]["avatar"]) == ("sky", {"kind": "robot"})
    assert tool_result(mcp(hosted, allen, "set_my_look", {"robot": True}))["avatar"] == {"kind": "robot"}
    assert "only one of" in tool_error(mcp(hosted, allen, "set_my_look", {"emoji": "🦊", "robot": True}))
    assert "exactly one emoji" in tool_error(mcp(hosted, allen, "set_my_look", {"emoji": "fox"}))
    assert tool_error(mcp(hosted, ogilvy, "set_my_look", {"color": "teal"}))  # not a palette key (schema)
    tools = hosted.post("/mcp", headers={"Authorization": f"Bearer {ogilvy}", "MCP-Protocol-Version": "2025-06-18",
                                         "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    schema = next(t for t in tools if t["name"] == "set_my_look")["inputSchema"]["properties"]["color"]
    assert list(AGENT_COLORS) == next(s for s in schema["anyOf"] if "enum" in s)["enum"]


def test_set_my_look_fetches_a_small_image(hosted, google):
    allen = connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]
    raw = png(128, 128)
    google.publish(IMAGE_URL, raw, headers={"Content-Type": "image/png"})
    look = tool_result(mcp(hosted, allen, "set_my_look", {"image_url": IMAGE_URL}))
    assert look["avatar"] == {"kind": "image", "type": "image/png", "bytes": len(raw)}  # not the data itself
    assert agents(hosted, google)[0]["avatar"] == {"kind": "image", "data": data_url("image/png", raw)}

    as_png = {"Content-Type": "image/png"}
    google.publish("https://img.example.com/big.png", png(512, 512), headers=as_png)
    google.publish("https://img.example.com/huge.png", png(16, 16, pad=60 * 1024), headers=as_png)
    google.publish("https://img.example.com/logo.svg", b"<svg/>", headers={"Content-Type": "image/svg+xml"})
    for url, words in (("https://img.example.com/big.png", "256×256"),
                       ("https://img.example.com/huge.png", "larger than"),
                       ("https://img.example.com/logo.svg", "SVG is not accepted"),
                       ("https://img.example.com/missing.png", "HTTP 404"),
                       ("http://img.example.com/fox.png", "https"),
                       ("https://127.0.0.1/fox.png", "non-public"),
                       ("https://localhost/fox.png", "public host")):
        error = tool_error(mcp(hosted, allen, "set_my_look", {"image_url": url}))
        assert words in error, (url, error)
    assert agents(hosted, google)[0]["avatar"]["kind"] == "image"  # unchanged by the failures


def test_image_fetches_are_limited_per_agent(hosted, google):
    allen = connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]
    ogilvy = connect(hosted, google, register(hosted)["client_id"], agent="Ogilvy")["access_token"]
    google.publish(IMAGE_URL, png(8, 8), headers={"Content-Type": "image/png"})
    for _ in range(10):
        tool_result(mcp(hosted, allen, "set_my_look", {"image_url": IMAGE_URL}))
    assert "too many image_url fetches" in tool_error(mcp(hosted, allen, "set_my_look", {"image_url": IMAGE_URL}))
    tool_result(mcp(hosted, allen, "set_my_look", {"emoji": "🦊"}))  # the rest still works
    tool_result(mcp(hosted, ogilvy, "set_my_look", {"image_url": IMAGE_URL}))  # per agent


def test_set_my_look_after_disconnect_is_refused(hosted, google):
    allen = connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]
    agent_id = agents(hosted, google)[0]["id"]
    hosted.delete(f"/api/agents/{agent_id}", headers=id_token(google))
    assert mcp(hosted, allen, "set_my_look", {"color": "sky"}).status_code == 401
