"""Hosted bridge: files in answers (SPEC section 7.2): the data model of `answer_files`, name cleaning, download names."""

import pytest

from server.db import ask_from_row
from server.hosted import files
from server.hosted.ratelimit import LIMITS, ByteBudget, RateLimiter
from server.hosted.files import (
    ITEM_KEYS,
    MAX_FILE_BYTES,
    MAX_SECRET_BYTES,
    clean_files,
    clean_name,
    download_name,
    validate_files,
)
from server.rules import Invalid

AT = "2026-11-19T10:00:00.000Z"


def item(n=1, **over):
    base = {"id": n, "name": f"report{n}.pdf", "mime": "application/pdf", "bytes": 1234, "file_id": f"fid-{n}",
            "secret": False, "uploaded_at": "2026-11-19T09:59:00.000Z", "answered_at": AT, "downloaded_at": None,
            "blocked_at": None, "deleted_at": None, "deleted_reason": None}
    return {**base, **over}


def test_constants():
    assert files.MAX_FILES == 6 and files.REQUEST_TIMEOUT == 900
    assert MAX_FILE_BYTES == 104_857_600 and MAX_SECRET_BYTES == 10_485_760
    assert (files.SECRET_TTL, files.SECRET_ORPHAN, files.FILE_ORPHAN) == (86400, 3600, 86400)
    assert len(ITEM_KEYS) == 12 and ITEM_KEYS[0] == "id" and ITEM_KEYS[-1] == "deleted_reason"


@pytest.mark.parametrize("raw, expected", [
    ("../x", "x"), ("a/b", "a_b"), ("a\\b", "a_b"), ("...", "file"), ("", "file"), ("a\x00b", "a_b"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("Com1.tar.gz", "_Com1.tar.gz"), ("LPT9", "_LPT9"), ("x. ", "x"),
    (" .hidden", "hidden"), ("a<b>c:d\"e|f?g*h", "a_b_c_d_e_f_g_h"), ("console.txt", "console.txt"),
    ("é.txt", "é.txt"),  # NFC
])
def test_clean_name_cases(raw, expected):
    assert clean_name(raw) == expected


def test_clean_name_bidi_and_format_controls():
    assert "‮" not in clean_name("‮exe.txt")
    assert clean_name("a​b c") == "a_b_c"


def test_clean_name_cuts_before_the_extension():
    cut = clean_name("a" * 300 + ".pdf")
    assert len(cut) == 200 and cut.endswith(".pdf")
    assert len(clean_name("b" * 300)) == 200
    assert len(clean_name("nul" + "x" * 300)) <= 200
    assert len(clean_name("x." + "e" * 300)) == 200


def test_validate_files_accepts_and_normalises():
    out = validate_files([item(1, name="../a/b.PDF", mime="Application/PDF"), item(2, secret=True, bytes=10)], AT)
    assert [f["id"] for f in out] == [1, 2]
    assert out[0]["name"] == "a_b.PDF"
    assert out[0]["mime"] == "application/pdf"
    assert list(out[0]) == list(ITEM_KEYS)
    assert validate_files([], AT) == []
    assert validate_files([item(1, bytes=MAX_FILE_BYTES)], AT)[0]["bytes"] == MAX_FILE_BYTES


def test_validate_files_bad_mime_is_stored_as_octet_stream():
    assert validate_files([item(1, mime="nonsense")], AT)[0]["mime"] == "application/octet-stream"
    assert validate_files([item(1, mime=None)], AT)[0]["mime"] == "application/octet-stream"


@pytest.mark.parametrize("value, path", [
    ([item(1), item(2, bytes=0)], "answer_files[1].bytes"),
    ([item(1, bytes=MAX_FILE_BYTES + 1)], "answer_files[0].bytes"),
    ([item(1), item(2), item(3, secret=True, bytes=MAX_SECRET_BYTES + 1)], "answer_files[2].bytes"),
    ([item(1, bytes=True)], "answer_files[0].bytes"),
    ([item(1, secret="true")], "answer_files[0].secret"),
    ([item(1, file_id="a/b")], "answer_files[0].file_id"),
    ([item(1, file_id="")], "answer_files[0].file_id"),
    ([item(1, file_id="x" * 201)], "answer_files[0].file_id"),
    ([item(1, deleted_at="2026-11-19T11:00:00.000Z")], "answer_files[0].file_id"),
    ([item(1, file_id=None)], "answer_files[0].deleted_at"),
    ([item(1, file_id=None, deleted_at="2026-11-19T11:00:00.000Z", deleted_reason="gone")],
     "answer_files[0].deleted_reason"),
    ([item(1, deleted_reason="sent")], "answer_files[0].deleted_reason"),
    ([item(1, answered_at="2026-11-19T09:00:00.000Z")], "answer_files[0].answered_at"),
    ([item(1, uploaded_at="")], "answer_files[0].uploaded_at"),
    ([item(1, downloaded_at=5)], "answer_files[0].downloaded_at"),
    ([item(0)], "answer_files[0].id"),
    ([item(1), item(1)], "answer_files[1].id"),
    ([item(1, name=5)], "answer_files[0].name"),
    ([item(n) for n in range(1, 8)], "answer_files"),
    ({"id": 1}, "answer_files"),
    (["x"], "answer_files[0]"),
    ([{k: v for k, v in item(1).items() if k != "secret"}], "answer_files[0].secret"),
])
def test_validate_files_fields(value, path):
    with pytest.raises(Invalid) as raised:
        validate_files(value, AT)
    assert str(raised.value).startswith(path)


def test_validate_files_deleted_item_is_fine():
    gone = item(1, file_id=None, deleted_at="2026-11-19T11:00:00.000Z", deleted_reason="sent")
    assert validate_files([gone], AT)[0]["file_id"] is None


def test_stale_and_open_read_as_empty():
    value = [item(1), item(2)]
    assert [f["id"] for f in clean_files(value, AT, "answered")] == [1, 2]
    assert [f["id"] for f in clean_files(value, AT, "closed")] == [1, 2]
    assert clean_files(value, AT, "open") == []
    assert clean_files(value, "2026-11-19T12:00:00.000Z", "answered") == []
    assert clean_files(value, None, "answered") == []
    assert clean_files(None, AT, "answered") == []
    assert clean_files("junk", AT, "answered") == []


def test_hand_edited_item_is_dropped_on_read():
    value = [item(n) for n in range(1, 8)]  # a 7th item
    assert [f["id"] for f in clean_files(value, AT, "answered")] == [1, 2, 3, 4, 5, 6]
    bidi = clean_files([item(1, name="‮gpj.exe")], AT, "answered")
    assert "‮" not in bidi[0]["name"]
    both = item(2, deleted_at="2026-11-19T11:00:00.000Z")  # file_id together with deleted_at
    assert [f["id"] for f in clean_files([item(1), both, item(3, bytes=0), item(1)], AT, "answered")] == [1]
    assert clean_files([item(1, extra="x")], AT, "answered")[0].keys() == set(ITEM_KEYS)


def test_single_tenant_always_empty():
    import sqlite3
    from server.db import ASK_FIELDS
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("select " + ", ".join(f"null as {f}" for f in ASK_FIELDS)).fetchone()
    assert ask_from_row(row)["answer_files"] == []


def test_download_name():
    assert download_name(12, 3, "Q3 report.PDF") == ("ask-12-file-3.pdf", "UTF-8''Q3%20report.PDF")
    assert download_name(12, 3, "x.toolongextension")[0] == "ask-12-file-3"
    assert download_name(1, 2, "noext")[0] == "ask-1-file-2"
    assert download_name(1, 2, "arch.tar.gz")[0] == "ask-1-file-2.gz"
    assert download_name(1, 2, "déjà.téx")[0] == "ask-1-file-2"
    ascii_name, star = download_name(1, 2, "отчёт.pdf")
    assert ascii_name == "ask-1-file-2.pdf" and star == "UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf"
    assert download_name(1, 2, "a/../b\"c")[1] == "UTF-8''a_.._b_c"


NASTY = ["../x", "a/b", "‮exe.txt", "​CON", "​​nul.txt", "a" * 300 + ".pdf", "b" * 300, "...", "",
         "  .x  ", "a\x00b", "CON", "nul.txt", "x. ", " .hidden", "é.txt", "x." + "e" * 300, "nul" + "x" * 300,
         "a<b>c", "COM1.tar.gz", "​" + "a" * 300 + ".pdf", ". . .", "_CON", " CON "]


@pytest.mark.parametrize("raw", NASTY)
def test_clean_name_is_idempotent(raw):
    once = clean_name(raw)
    assert clean_name(once) == once  # the bridge re-cleans on every read


def test_closed_ask_keeps_deleted_items_as_stored():
    gone = item(1, file_id=None, deleted_at="2026-11-19T11:00:00.000Z", deleted_reason="closed")
    kept = item(2)
    out = clean_files([gone, kept], AT, "closed")
    assert out == [gone, kept]


@pytest.mark.parametrize("reason", ["sent", "closed", "reopened", "expired", "replaced"])
def test_each_deleted_reason_is_accepted(reason):
    gone = item(1, file_id=None, deleted_at="2026-11-19T11:00:00.000Z", deleted_reason=reason)
    assert validate_files([gone], AT) == [gone]
    assert clean_files([gone], AT, "closed") == [gone]


def test_device_name_edges():
    assert clean_name("COM10") == "COM10" and clean_name("com0") == "com0"
    assert clean_name("LPT0.txt") == "LPT0.txt"
    assert clean_name("​CON") != "CON" and clean_name("​CON").lstrip("_").upper() == "CON"
    assert clean_name("​CON").startswith("_")


# ---------- answer writes, events, no-op rule, answer text (SPEC 7.2, 7.3) ----------

import copy
import json

from server.rules import ANSWER_FILES, Change, plan_answer, plan_close, plan_reopen, answer_text_for
from server.tokens import Identity

from .fake_drive import app_action, now_iso
from .test_hosted_voice import (
    AUDIO,
    agent,
    answered_by_voice,
    asks_of,
    bearer,
    tool_result,
    mcp,
)
from .hosted_support import DENIS_SUB
from .media_samples import data_url, png

DENIS = Identity("denis")
ALLEN = Identity("agent", "allen")
NOW = "2026-11-19T12:00:00.000Z"
KEYS = ("name", "mime")


def answered_ask(files, answer="Done", **over):
    return {"id": 1, "agent": "allen", "status": "answered", "answer": answer, "answered_at": AT, "resolution": None,
            "closed_by": None, "closed_at": None, "answer_voice": None, ANSWER_FILES: files, **over}


def answered_by_files(drive, ask_id, items, text="Done", **changes):
    """What the apps do (SPEC 7.3 step 5): the answer, answer_files and the `answered` event in one PATCH of the ask
    file. `items` are item() dicts; each gets the new answered_at. Returns the list written."""
    at = now_iso()
    file = drive.file(f"ask-{ask_id}.json")
    doc = json.loads(file["content"])
    written = [{**i, "answered_at": at} for i in items]
    payload = {"answer": text, "previous_answer": doc.get("answer"), "voice": None, "previous_voice": doc.get("answer_voice"),
               "files": written, "previous_files": doc.get(ANSWER_FILES) or []}
    doc.update(status="answered", answer=text, answered_at=at, updated_at=at, answer_files=written, **changes)
    doc["events"].append({"id": max(e["id"] for e in doc["events"]) + 1, "ask_id": ask_id, "at": at,
                          "actor": "denis", "kind": "answered", "payload": payload})
    doc["rev"] += 1
    doc["writer"] = "app"
    drive.write(file["id"], doc, {"ask_id": str(ask_id), "status": "answered", "agent": doc["agent"],
                                  "rev": str(doc["rev"])})
    return written


def test_answer_with_files_in_one_write(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    assert tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_files"] == []
    written = answered_by_files(drive, 1, [item(1), item(2, secret=True, bytes=10)])
    doc = drive.doc(1)
    assert doc["answer_files"] == written and doc["answer"] == "Done"
    event = doc["events"][-1]
    assert event["kind"] == "answered" and event["payload"]["files"] == written
    assert event["payload"]["previous_files"] == []
    for ask in (asks_of(hosted).get(1), tool_result(mcp(hosted, token, "ask_get", {"id": 1})),
                hosted.get("/api/asks/1", headers=bearer(token)).json()):
        shown = list(ask["answer_files"][0])
        assert shown == list(ITEM_KEYS) if "file_id" in shown else shown[:9] == [
            "id", "name", "mime", "bytes", "secret", "downloaded_at", "deleted_at", "deleted_reason", "blocked_at"]
        assert [(f["id"], f["name"], f["secret"]) for f in ask["answer_files"]] == [
            (1, "report1.pdf", False), (2, "report2.pdf", True)]
    # a stale list (an older app re-answered) reads as [] and the bridge's next write drops it
    app_action(drive, 1, "answered", answer="No, wait")
    assert drive.doc(1)["answer_files"] == written
    assert asks_of(hosted).get(1)["answer_files"] == []
    tool_result(mcp(hosted, token, "ask_close", {"id": 1}))
    assert drive.doc(1)["answer_files"] == []


def test_noop_only_if_file_list_same():
    a, b = item(1), item(2, secret=True)
    ask = answered_ask([a, b])
    assert plan_answer(DENIS, "Done", lambda: ask, NOW, files=[a, b]) == (ask, None)
    assert plan_answer(DENIS, " Done ", lambda: ask, NOW, files=[{**a, "answered_at": NOW}, b])[1] is None
    for files in ([a], [a, b, item(3)], [a, item(2, secret=False)], [a, item(2, secret=True, file_id="other")],
                  [item(1, file_id="other"), b], None, []):
        _, change = plan_answer(DENIS, "Done", lambda: ask, NOW, files=files)
        assert isinstance(change, Change) and change.kind == "answered", files
    # no files either side: the old rule (text only) stands
    plain = answered_ask([])
    assert plan_answer(DENIS, "Done", lambda: plain, NOW, files=[]) == (plain, None)
    assert plan_answer(DENIS, "Done", lambda: plain, NOW)[1] is None
    # a new file on the same text is a change
    _, change = plan_answer(DENIS, "Done", lambda: plain, NOW, files=[item(1)])
    assert change.kind == "answered"


def test_kept_item_restamped():
    kept = item(1, uploaded_at="2026-11-18T08:00:00.000Z")
    fresh = item(3, answered_at=AT)
    ask = answered_ask([kept, item(2)])
    _, change = plan_answer(DENIS, "Done", lambda: ask, NOW, files=[kept, fresh])
    out = change.updates[ANSWER_FILES]
    assert out[0] == {**kept, "answered_at": NOW}  # same id, file_id, secret, uploaded_at
    assert (out[0]["id"], out[0]["file_id"], out[0]["secret"], out[0]["uploaded_at"]) == (
        1, kept["file_id"], False, "2026-11-18T08:00:00.000Z")
    assert out[1]["answered_at"] == NOW and out[1]["id"] == 3
    assert all(f["answered_at"] == change.updates["answered_at"] == NOW for f in out)


def test_reanswer_drops_item_and_reports_previous_files():
    a, b, c = item(1), item(2), item(3, file_id=None, deleted_at="2026-11-19T10:30:00.000Z", deleted_reason="sent")
    ask = answered_ask([a, b, c])
    _, change = plan_answer(DENIS, "Done", lambda: ask, NOW, files=[a])
    assert change.payload["files"] == [{**a, "answered_at": NOW}]
    previous = change.payload["previous_files"]
    assert previous[0] == a  # kept: as stored
    assert previous[1] == {**b, "deleted_reason": "replaced"}  # dropped: told to the app and the agent
    assert previous[2] == c  # already deleted (sent): its own reason stays
    assert change.updates[ANSWER_FILES] == [{**a, "answered_at": NOW}]
    assert list(change.payload) == ["answer", "previous_answer", "voice", "previous_voice", "files", "previous_files"]
    # dropping all
    _, change = plan_answer(DENIS, "Done again", lambda: ask, NOW)
    assert change.updates[ANSWER_FILES] == [] and change.payload["files"] == []
    assert [f["deleted_reason"] for f in change.payload["previous_files"]] == ["replaced", "replaced", "sent"]
    # nothing before, nothing now: the single-tenant ask (no such field) gets none written
    _, change = plan_answer(DENIS, "x", lambda: {k: v for k, v in ask.items() if k != ANSWER_FILES}, NOW)
    assert ANSWER_FILES not in change.updates
    assert change.payload["files"] == [] and change.payload["previous_files"] == []


def test_answer_text_counts_files():
    two, one = [item(1), item(2)], [item(1)]
    assert answer_text_for(None, None, None, two) == "2 files attached"
    assert answer_text_for("", None, None, one) == "1 file attached"
    assert answer_text_for(None, None, {"duration_ms": 42000}, two) == "Voice answer (0:42), not transcribed · 2 files attached"
    assert answer_text_for(None, None, {"duration_ms": 42000}, []) == "Voice answer (0:42), not transcribed"
    assert answer_text_for(None, None, {"duration_ms": 65000}, one) == "Voice answer (1:05), not transcribed · 1 file attached"
    assert answer_text_for("Yes", None, None, two) == "Yes"  # typed text wins, nothing more
    assert answer_text_for("", "Option A", None, two) == "Option A"
    assert answer_text_for("more", "Option A", {"duration_ms": 1000}, two) == "Option A\nmore"
    assert answer_text_for(None, None, None, []) == ""
    # a secret file is not mentioned
    assert "secret" not in answer_text_for(None, None, None, [item(1, secret=True)])
    # plan_answer fills an empty answer with it when there are files
    _, change = plan_answer(DENIS, "  ", lambda: answered_ask([], answer="old"), NOW, files=two)
    assert change.updates["answer"] == "2 files attached"


def test_reopen_clears_files(hosted, google):
    a = item(1, answered_at=AT)
    for status, files in (("answered", [a]), ("closed", [{**a, "file_id": None, "deleted_at": NOW, "deleted_reason": "closed"}])):
        ask = answered_ask(files, status=status, resolution=None)
        _, change = plan_reopen(ALLEN, lambda: ask, NOW)
        assert change.updates[ANSWER_FILES] == [] and change.payload[ANSWER_FILES] == files
    _, change = plan_reopen(ALLEN, lambda: answered_ask([]), NOW)
    assert ANSWER_FILES not in change.updates and change.payload[ANSWER_FILES] == []
    # hosted: REST reopen by the agent
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    written = answered_by_files(drive, 1, [item(1), item(2)])
    reopened = hosted.post("/api/asks/1/reopen", headers=bearer(token))
    assert reopened.status_code == 200 and reopened.json()["answer_files"] == []
    doc = drive.doc(1)
    assert doc["answer_files"] == [] and doc["events"][-1]["payload"]["answer_files"] == written


def test_close_keeps_files_marked_deleted():
    a, gone = item(1), item(2, file_id=None, deleted_at="2026-11-19T10:30:00.000Z", deleted_reason="sent")
    _, change = plan_close(ALLEN, "ok", lambda: answered_ask([a, gone]), NOW)
    assert change.updates[ANSWER_FILES] == [{**a, "file_id": None, "deleted_at": NOW, "deleted_reason": "closed"}, gone]
    _, change = plan_close(ALLEN, "ok", lambda: answered_ask([]), NOW)
    assert ANSWER_FILES not in change.updates
    assert change.payload == {"resolution": "ok", "previous_status": "answered"}


def test_no_agent_tool_or_rest_body_writes_answer_files(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    created = tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    assert created["answer_files"] == [] and drive.doc(1)["answer_files"] == []
    for title in ("Paint?", "Print?", "Pack?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))
    for ask_id in (1, 2, 3, 4):
        app_action(drive, ask_id, "answered", answer="Yes")

    def forged(ask_id):
        return [item(1, answered_at=drive.doc(ask_id)["answered_at"])]

    picture = {"data": data_url("image/png", png(8, 8))}
    for name, arguments in (("ask_create", {"title": "Forged?", "answer_files": forged(1)}),
                            ("ask_update", {"id": 1, "title": "Ship today?", "answer_files": forged(1)}),
                            ("ask_attach", {"id": 1, **picture, "answer_files": forged(1)}),
                            ("ask_close", {"id": 1, "resolution": "Shipped", "answer_files": forged(1)})):
        mcp(hosted, token, name, arguments)
    for method, path, body in (("POST", "/api/asks", {"title": "Forged?", "answer_files": forged(2)}),
                               ("PATCH", "/api/asks/2", {"answer_files": forged(2)}),
                               ("POST", "/api/asks/2/answer", {"answer": "Yes", "answer_files": forged(2)}),
                               ("POST", "/api/asks/2/close", {"answer_files": forged(2)})):
        response = hosted.request(method, path, json=body, headers=bearer(token))
        assert response.status_code == 422 and "answer_files" in response.json()["detail"], (method, path)
    reopened = hosted.post("/api/asks/3/reopen", json={"answer_files": forged(3)}, headers=bearer(token))
    assert reopened.status_code == 200 and reopened.json()["answer_files"] == []
    asks = asks_of(hosted)
    for call, args in ((asks.create, (ALLEN, {"title": "Forged?", "answer_files": forged(1)})),
                       (asks.update, (ALLEN, 5, {"answer_files": forged(1)}))):
        with pytest.raises(Invalid) as refused:
            call(*args)
        assert "unknown fields: answer_files" in str(refused.value)
    for name in [f["name"] for f in drive.files.values() if f["name"].startswith("ask-")]:
        doc = json.loads(drive.file(name)["content"])
        assert not doc.get("answer_files") and "report1.pdf" not in json.dumps(doc), name


def test_non_owner_sees_name_and_mime_null(hosted, google):
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "ask_create", {"title": "Ship?"}))
    first = answered_by_files(drive, 1, [item(1, name="q3.pdf"), item(2, name="keys.txt", secret=True, bytes=5)])
    kept = [{**first[0]}]
    answered_by_files(drive, 1, kept, text="Done v2")
    mine = tool_result(mcp(hosted, allen, "ask_get", {"id": 1}))
    assert [f["name"] for f in mine["answer_files"]] == ["q3.pdf"]
    theirs = (tool_result(mcp(hosted, feynman, "ask_get", {"id": 1})),
              hosted.get("/api/asks/1", headers=bearer(feynman)).json(),
              tool_result(mcp(hosted, feynman, "asks_list"))["asks"][0])
    for ask in theirs:
        assert [(f["name"], f["mime"]) for f in ask["answer_files"]] == [(None, None)]
        assert [f["bytes"] for f in ask["answer_files"]] == [1234]
        assert "url" not in ask["answer_files"][0]
    for ask in theirs[:2]:
        text = json.dumps(ask["events"])
        assert "q3.pdf" not in text and "keys.txt" not in text and "application/pdf" not in text
        payloads = [e["payload"] for e in ask["events"] if e["kind"] == "answered"]
        assert [[(f["name"], f["mime"]) for f in p["files"]] for p in payloads] == [[(None, None), (None, None)], [(None, None)]]
        assert [len(p["previous_files"]) for p in payloads] == [0, 2]
    assert "q3.pdf" in json.dumps(mine["events"]) and "keys.txt" in json.dumps(mine["events"])
    tool_result(mcp(hosted, allen, "ask_create", {"title": "Paint?"}))
    answered_by_files(drive, 2, [item(1, name="a.png")])
    hosted.post("/api/asks/2/reopen", headers=bearer(allen)).raise_for_status()
    reopened = [e for e in tool_result(mcp(hosted, feynman, "ask_get", {"id": 2}))["events"] if e["kind"] == "reopened"][0]
    assert [(f["name"], f["mime"]) for f in reopened["payload"]["answer_files"]] == [(None, None)]
    mine = [e for e in tool_result(mcp(hosted, allen, "ask_get", {"id": 2}))["events"] if e["kind"] == "reopened"][0]
    assert mine["payload"]["answer_files"][0]["name"] == "a.png"


def test_plan_answer_refuses_bad_file_lists():
    ask = answered_ask([])
    for files, path in (([item(1), item(1)], "answer_files[1].id"), ([item(n) for n in range(1, 8)], "answer_files"),
                        ([item(1, bytes=0)], "answer_files[0].bytes"), (["x"], "answer_files[0]")):
        with pytest.raises(Invalid) as raised:
            plan_answer(DENIS, "Done", lambda: ask, NOW, files=files)
        assert str(raised.value).startswith(path), files


# ---------- what the owner agent reads: links, for_reader(file_urls=...), the webhook (SPEC 7.4) ----------

import base64
import time
from datetime import datetime, timezone

from server.hosted.crypto import Keyring
from server.hosted.files import FileLinks
from server.hosted.voice import VoiceLinks, for_reader

from .hosted_support import id_token
from .test_hosted_voice import agent_id

PUBLIC = "https://asks.example"
KEY = base64.b64encode(b"k" * 32).decode()
CLAIM_KEYS = {"user", "agent", "ask_id", "answered_at", "file_id", "n", "exp"}


def file_links(clock=time.time):
    return FileLinks(Keyring(KEY), PUBLIC, clock=clock)


def file_claims(url: str) -> dict:
    token = url.removeprefix(f"{PUBLIC}/media/v1/")
    return json.loads(Keyring(KEY).decrypt(token, "file"))


def test_file_links_roundtrip():
    links = file_links()
    url, expires = links.url_for("u1", "ag_1", 7, AT, "fid-1", 2)
    assert url.startswith(f"{PUBLIC}/media/v1/") and expires.endswith("Z")
    opened = links.open(url.removeprefix(f"{PUBLIC}/media/v1/"))
    assert set(opened) == CLAIM_KEYS
    assert {k: v for k, v in opened.items() if k != "exp"} == {
        "user": "u1", "agent": "ag_1", "ask_id": 7, "answered_at": AT, "file_id": "fid-1", "n": 2}
    assert abs(opened["exp"] - time.time() - 3600) < 5


def test_file_token_context():
    """A voice token does not open as a file token and the reverse; voice's claim set is untouched."""
    keyring = Keyring(KEY)
    voice = VoiceLinks(keyring, PUBLIC)
    files_ = file_links()
    vurl, _ = voice.link("u1", "ag_1", 7, "fid-1")
    furl, _ = files_.url_for("u1", "ag_1", 7, AT, "fid-1", 1)
    vtoken, ftoken = vurl.rsplit("/", 1)[1], furl.rsplit("/", 1)[1]
    assert files_.open(vtoken) is None and voice.open(ftoken) is None
    assert voice.open(vtoken) is not None and files_.open(ftoken) is not None
    from server.hosted import voice as voice_module
    assert voice_module.CLAIMS == {"user": str, "agent": str, "ask_id": int, "file_id": str, "exp": int}
    assert voice_module.LINK_CONTEXT == "media"


def sealed(context="file", keyring=None, **change):
    claims = {"user": "u1", "agent": "ag_1", "ask_id": 7, "answered_at": AT, "file_id": "fid-1", "n": 1,
              "exp": int(time.time()) + 600}
    claims = {**claims, **change}
    claims = {k: v for k, v in claims.items() if v is not ...}
    return (keyring or Keyring(KEY)).encrypt(json.dumps(claims), context)


@pytest.mark.parametrize("token", [
    "garbage", "", None, 5, "v1.k1.garbage", "A" * 3000,
])
def test_file_token_junk_opens_as_none(token):
    assert file_links().open(token) is None


def test_file_token_tampered_expired_wrong_shape():
    links = file_links()
    good = sealed()
    assert links.open(good) is not None
    assert links.open(good[:-5] + ("A" if good[-5] != "A" else "B") + good[-4:]) is None
    assert links.open(sealed(exp=int(time.time()) - 1)) is None
    assert links.open(sealed(keyring=Keyring(base64.b64encode(b"z" * 32).decode()))) is None
    assert links.open(sealed(context="media")) is None
    assert links.open(sealed(context="webhook:ag_1")) is None
    for key in CLAIM_KEYS:
        assert links.open(sealed(**{key: ...})) is None, key
    assert links.open(sealed(extra="x")) is None
    for key, bad in (("ask_id", "7"), ("n", "1"), ("n", 0), ("n", True), ("exp", "9"), ("file_id", 5),
                     ("answered_at", ""), ("agent", ""), ("user", None), ("ask_id", False)):
        assert links.open(sealed(**{key: bad})) is None, (key, bad)


def test_file_token_expires_with_clock():
    now = [1_000_000.0]
    links = file_links(clock=lambda: now[0])
    url, expires = links.url_for("u1", "ag_1", 7, AT, "fid-1", 1)
    token = url.rsplit("/", 1)[1]
    assert links.open(token) is not None
    now[0] += 3599
    assert links.open(token) is not None
    now[0] += 2
    assert links.open(token) is None
    assert expires == datetime.fromtimestamp(1_003_600, tz=timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def owner_reader():
    from server.hosted.accounts import HostedIdentity
    return HostedIdentity("agent", "allen", user="u1", agent_id="ag_1")


def stored_ask(items, status="answered"):
    return {"id": 3, "agent": "allen", "status": status, "answered_at": AT, "answer_voice": None,
            "answer_files": items, "events": []}


def test_for_reader_owner_gets_urls_for_normal_files_only():
    links, flinks = VoiceLinks(Keyring(KEY), PUBLIC), file_links()
    ask = stored_ask([item(1), item(2, secret=True, bytes=5), item(3, file_id=None, deleted_at=AT,
                                                                     deleted_reason="closed")])
    got = for_reader(ask, links, owner_reader(), file_links=flinks)["answer_files"]
    assert got[0]["url"].startswith(f"{PUBLIC}/media/v1/") and got[0]["url_expires_at"]
    claims = file_claims(got[0]["url"])
    assert claims["file_id"] == "fid-1" and claims["n"] == 1 and claims["ask_id"] == 3
    assert (claims["user"], claims["agent"], claims["answered_at"]) == ("u1", "ag_1", AT)
    assert "url" not in got[1] and "url_expires_at" not in got[1] and got[1]["secret"] is True
    assert "url" not in got[2]
    assert [f["name"] for f in got] == ["report1.pdf", "report2.pdf", "report3.pdf"]
    # no links object (asks_list, the bridge's own reads): no url, metadata intact
    bare = for_reader(ask, links, owner_reader())["answer_files"]
    assert all("url" not in f for f in bare) and bare[1]["name"] == "report2.pdf"
    # a closed ask gives no link
    closed = for_reader(stored_ask([item(1)], "closed"), links, owner_reader(), file_links=flinks)["answer_files"]
    assert "url" not in closed[0]


def test_for_reader_non_owner_gets_no_url():
    links, flinks = VoiceLinks(Keyring(KEY), PUBLIC), file_links()
    from server.hosted.accounts import HostedIdentity
    other = HostedIdentity("agent", "feynman", user="u1", agent_id="ag_2")
    got = for_reader(stored_ask([item(1)]), links, other, file_links=flinks)["answer_files"]
    assert "url" not in got[0] and got[0]["name"] is None


def test_for_reader_file_urls_false_is_the_webhook_shape():
    links, flinks = VoiceLinks(Keyring(KEY), PUBLIC), file_links()
    ask = stored_ask([item(1), item(2, secret=True, bytes=5)])
    got = for_reader(ask, links, owner_reader(), file_urls=False, file_links=flinks)["answer_files"]
    assert "url" not in got[0] and got[0]["name"] == "report1.pdf" and got[0]["id"] == 1
    assert got[1] == {"id": 2, "secret": True}
    assert "PDF" not in json.dumps(got[1]) and "url" not in json.dumps(got)


def secret_and_normal(drive):
    return answered_by_files(drive, 1, [item(1, name="q3.pdf"), item(2, name="keys.txt", secret=True, bytes=5)])


def test_owner_gets_url_for_normal_files_in_ask_get_and_asks_mine(hosted, google):
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    secret_and_normal(drive)
    reads = (tool_result(mcp(hosted, token, "ask_get", {"id": 1})),
             tool_result(mcp(hosted, token, "asks_mine"))["asks"][0])
    for ask in reads:
        normal, secret = ask["answer_files"]
        assert "/media/v1/" in normal["url"] and normal["url_expires_at"]
        claims = file_claims_of(normal["url"], hosted)
        assert (claims["user"], claims["agent"], claims["ask_id"], claims["n"], claims["answered_at"]) == (
            DENIS_SUB, owner, 1, 1, drive.doc(1)["answered_at"])
        assert claims["file_id"] == "fid-1"
        assert "url" not in secret and secret["secret"] is True
    rest = hosted.get("/api/asks/1", headers=bearer(token)).json()["answer_files"]
    assert "/media/v1/" in rest[0]["url"] and "url" not in rest[1]
    # a new link on each read
    again = tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_files"][0]["url"]
    assert again != reads[0]["answer_files"][0]["url"]


def file_claims_of(url, hosted):
    opened = hosted.app.state.file_links.open(url.rsplit("/", 1)[1])
    assert opened is not None
    return opened


def test_asks_list_has_no_url(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    secret_and_normal(drive)
    listed = tool_result(mcp(hosted, token, "asks_list"))["asks"][0]
    assert [f["name"] for f in listed["answer_files"]] == ["q3.pdf", "keys.txt"]
    assert "url" not in json.dumps(listed["answer_files"]) and "/media/v1/" not in json.dumps(listed)
    rest = hosted.get("/api/asks", headers=bearer(token)).json()["asks"][0]
    assert "/media/v1/" not in json.dumps(rest)


def test_secret_file_has_no_url_anywhere(hosted, google):
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/files"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    answered_by_files(drive, 1, [item(2, name="keys.txt", secret=True, bytes=5)])
    reads = (tool_result(mcp(hosted, token, "ask_get", {"id": 1})),
             tool_result(mcp(hosted, token, "asks_mine"))["asks"][0],
             tool_result(mcp(hosted, token, "asks_list"))["asks"][0],
             hosted.get("/api/asks/1", headers=bearer(token)).json(),
             hosted.get("/api/asks", headers=bearer(token)).json()["asks"][0])
    for ask in reads:
        assert "url" not in ask["answer_files"][0] and "url_expires_at" not in ask["answer_files"][0]
        assert "/media/v1/" not in json.dumps(ask)
    hosted.post("/api/events", json={"ask_id": 1, "kind": "answered"}, headers=id_token(google)).raise_for_status()
    assert hosted.app.state.webhooks.flush(5)
    assert "/media/v1/" not in google.webhook_calls[-1].content.decode()


def test_webhook_body_has_no_file_urls(hosted, google):
    """The ask.answered webhook has no url on any file; a secret file is exactly {id, secret: true}. The agent reads
    asks_mine / ask_get for links."""
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/files"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    secret_and_normal(drive)
    sent = hosted.post("/api/events", json={"ask_id": 1, "kind": "answered"}, headers=id_token(google))
    assert sent.status_code == 202
    assert hosted.app.state.webhooks.flush(5)
    text = google.webhook_calls[-1].content.decode()
    body = json.loads(text)
    normal, secret = body["ask"]["answer_files"]
    assert "url" not in normal and "url_expires_at" not in normal
    assert (normal["id"], normal["name"], normal["secret"]) == (1, "q3.pdf", False)
    assert secret == {"id": 2, "secret": True}
    assert "/media/v1/" not in text and "keys.txt" not in text
    # the agent's own reads still carry the link
    assert tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_files"][0]["url"]


# ---------- fix round 1: where urls appear, the fields an agent sees, blocked files ----------

AGENT_KEYS = ["id", "name", "mime", "bytes", "secret", "downloaded_at", "deleted_at", "deleted_reason", "blocked_at"]


def test_writes_return_no_file_url(hosted, google):
    """SPEC 7.4: url only in ask_get and asks_mine. What a write returns goes through HostedAsks._out, whose default
    carries no link (an answered ask can't be edited, so the writes are reached with a stored ask directly); close
    returns a closed ask, which never has one."""
    from server.hosted.records import HostedAsks
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    answered_by_files(drive, 1, [item(1, name="q3.pdf")])
    state = hosted.app.state
    reader = owner_reader()
    asks = HostedAsks(state.pool.records(DENIS_SUB), VoiceLinks(Keyring(KEY), PUBLIC), reader, file_links())
    ask = asks.items.get(1)
    assert "url" not in asks._out(ask)["answer_files"][0]
    assert "url" in asks._out(ask, True)["answer_files"][0]
    assert "url" in tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_files"][0]
    closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Done"}))
    assert "/media/v1/" not in json.dumps(closed)


def test_agent_sees_only_the_spec_fields_no_file_id(hosted, google):
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/files"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "ask_create", {"title": "Ship?"}))
    answered_by_files(drive, 1, [item(1), item(2, secret=True, bytes=5)])
    answered_by_files(drive, 1, [item(1)], text="v2")
    hosted.post("/api/events", json={"ask_id": 1, "kind": "answered"}, headers=id_token(google)).raise_for_status()
    assert hosted.app.state.webhooks.flush(5)
    seen = []
    for token in (allen, feynman):
        for ask in (tool_result(mcp(hosted, token, "ask_get", {"id": 1})),
                    tool_result(mcp(hosted, token, "asks_mine"))["asks"][0] if token == allen else
                    tool_result(mcp(hosted, token, "asks_list"))["asks"][0],
                    hosted.get("/api/asks/1", headers=bearer(token)).json()):
            seen.append(ask["answer_files"])
            for event in ask.get("events", []):
                for key in ("files", "previous_files", "answer_files"):
                    seen.append(event["payload"].get(key) or [])
    seen.append(json.loads(google.webhook_calls[-1].content)["ask"]["answer_files"])
    flat = [f for files_ in seen for f in files_]
    assert flat
    for f in flat:
        assert "file_id" not in f and "uploaded_at" not in f and "answered_at" not in f, f
        if f.get("secret") and set(f) != {"id", "secret"}:
            assert set(f) - {"url", "url_expires_at"} <= set(AGENT_KEYS)
        assert set(f) <= set(AGENT_KEYS) | {"url", "url_expires_at"}, f
    assert "fid-1" not in json.dumps(seen) and "fid-2" not in json.dumps(seen)
    normal = tool_result(mcp(hosted, allen, "ask_get", {"id": 1}))["answer_files"][0]
    assert [k for k in normal if k not in ("url", "url_expires_at")] == AGENT_KEYS


def test_no_url_for_blocked_file():
    flinks = file_links()
    ask = stored_ask([item(1, blocked_at=AT), item(2)])
    got = for_reader(ask, VoiceLinks(Keyring(KEY), PUBLIC), owner_reader(), file_links=flinks)["answer_files"]
    assert "url" not in got[0] and got[0]["blocked_at"] == AT and "url" in got[1]


def test_files_request_bucket():
    assert LIMITS["files"] == (20, 60.0)
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert all(limiter.allow("files", "u1:a1") for _ in range(20))
    assert not limiter.allow("files", "u1:a1") and limiter.allow("files", "u1:a2")
    now[0] = 61.0
    assert limiter.allow("files", "u1:a1")


def test_file_bytes_bucket_charges_and_refuses():
    GIB = 1024 ** 3
    now = [0.0]
    budget = ByteBudget(clock=lambda: now[0])
    assert budget.limit == GIB and budget.window == 3600.0
    assert budget.charge("a1", 600 * 1024 ** 2)
    assert not budget.charge("a1", 500 * 1024 ** 2)  # refused ...
    assert budget.charge("a1", 424 * 1024 ** 2)  # ... and recorded nothing: exactly the rest still fits
    assert not budget.charge("a1", 1)
    assert budget.charge("a2", GIB) and not budget.charge("a2", 1)  # per agent
    assert not budget.charge("a3", GIB + 1)
    now[0] = 3599.0
    assert not budget.charge("a1", 1)
    now[0] = 3600.5  # the first charge leaves the window, the second is still in it
    assert budget.charge("a1", 600 * 1024 ** 2) and not budget.charge("a1", 1024 ** 3)


# ---------- Task 7: the agent's close and reopen delete the answer's files ----------

import logging

from server.hosted.records import HostedAsks


def drive_file(drive, ask_id: int, n: int, kind: str = "file", of_ask: int | None = None) -> str:
    """A file in the user's appDataFolder as the apps store an answer's file (SPEC 7.2), with chosen appProperties."""
    return drive.add(f"file-{ask_id}-{n}.bin", b"BODY-OF-THE-USER", {
        "ask_id": str(ask_id if of_ask is None else of_ask), "kind": kind, "file": str(n), "secret": "false"},
        mime="application/octet-stream")


def test_agent_close_deletes_files_and_claims(hosted, google, caplog, monkeypatch):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))
    own1, own2 = drive_file(drive, 1, 1), drive_file(drive, 1, 2)
    foreign_kind = drive_file(drive, 1, 3, kind="voice")  # named by the ask, but not a file of the answer
    foreign_ask = drive_file(drive, 1, 4, of_ask=2)  # another ask's file
    other = drive_file(drive, 2, 1)
    answered_by_files(drive, 1, [item(1, file_id=own1), item(2, file_id=own2), item(3, file_id=foreign_kind),
                                 item(4, file_id=foreign_ask), item(5, file_id=None, deleted_at=AT,
                                                                    deleted_reason="sent")])
    answered_by_files(drive, 2, [item(1, file_id=other)])
    claimed = []
    monkeypatch.setattr(HostedAsks, "_drop_claims", lambda self, ask_id: claimed.append(ask_id))
    drive.fail_next("DELETE", f"/drive/v3/files/{own1}", 500)  # the first delete fails: logged, the rest goes on
    with caplog.at_level(logging.DEBUG):
        closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    assert closed["status"] == "closed" and drive.doc(1)["status"] == "closed"  # the close stands
    assert own1 in drive.files  # left to the sweep
    assert own2 not in drive.files
    assert foreign_kind in drive.files and foreign_ask in drive.files and other in drive.files
    assert [(f["file_id"], f["deleted_reason"]) for f in drive.doc(1)["answer_files"][:4]] == [(None, "closed")] * 4
    log = " ".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("hosted:"))
    assert own1 in log and foreign_kind in log and foreign_ask in log
    assert "report" not in log and "BODY" not in log and "HTTP 500" not in log
    assert claimed == [1]
    # an ask without files deletes nothing more and still ends the claims
    tool_result(mcp(hosted, token, "ask_create", {"title": "Print?"}))
    deletes = len(drive.calls("DELETE"))
    tool_result(mcp(hosted, token, "ask_close", {"id": 3}))
    assert len(drive.calls("DELETE")) == deletes and claimed == [1, 3]


def test_agent_reopen_deletes_files(hosted, google, caplog, monkeypatch):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    own, foreign = drive_file(drive, 1, 1), drive_file(drive, 1, 2, kind="voice")
    written = answered_by_files(drive, 1, [item(1, file_id=own), item(2, file_id=foreign)])
    claimed = []
    monkeypatch.setattr(HostedAsks, "_drop_claims", lambda self, ask_id: claimed.append(ask_id))
    with caplog.at_level(logging.DEBUG):
        reopened = hosted.post("/api/asks/1/reopen", headers=bearer(token))
    assert reopened.status_code == 200 and reopened.json()["status"] == "open"
    assert own not in drive.files and foreign in drive.files
    doc = drive.doc(1)
    assert doc["answer_files"] == [] and doc["events"][-1]["payload"]["answer_files"] == written
    assert claimed == [1]
    # a closed ask's files are gone already (file_id null): reopening deletes nothing
    tool_result(mcp(hosted, token, "ask_create", {"title": "Paint?"}))
    answered_by_files(drive, 2, [item(1, file_id=drive_file(drive, 2, 1))])
    tool_result(mcp(hosted, token, "ask_close", {"id": 2}))
    deletes = len(drive.calls("DELETE"))
    hosted.post("/api/asks/2/reopen", headers=bearer(token)).raise_for_status()
    assert len(drive.calls("DELETE")) == deletes


def test_file_of_another_item_is_not_deleted(hosted, google, caplog):
    """Same ask, same kind, but the file's own `file` property names another item: not this item's file."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    mine, swapped = drive_file(drive, 1, 1), drive_file(drive, 1, 9)  # the second says it is item 9
    answered_by_files(drive, 1, [item(1, file_id=mine), item(2, file_id=swapped)])
    with caplog.at_level(logging.DEBUG):
        tool_result(mcp(hosted, token, "ask_close", {"id": 1}))
    assert mine not in drive.files and swapped in drive.files
    assert any(swapped in r.getMessage() and "not deleted" in r.getMessage() for r in caplog.records)


def test_failing_claim_cleanup_does_not_fail_close_or_reopen(hosted, google, caplog, monkeypatch):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    for title in ("Ship?", "Paint?"):
        tool_result(mcp(hosted, token, "ask_create", {"title": title}))

    def boom(self, ask_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(HostedAsks, "_drop_claims", boom)
    with caplog.at_level(logging.DEBUG):
        assert tool_result(mcp(hosted, token, "ask_close", {"id": 1}))["status"] == "closed"
        answered_by_files(drive, 2, [item(1, file_id=drive_file(drive, 2, 1))])
        assert hosted.post("/api/asks/2/reopen", headers=bearer(token)).status_code == 200
    assert drive.doc(1)["status"] == "closed" and drive.doc(2)["status"] == "open"
    assert any("claims of ask #1" in r.getMessage() for r in caplog.records)
