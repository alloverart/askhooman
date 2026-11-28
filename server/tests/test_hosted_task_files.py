"""Hosted bridge: files in task replies (SPEC section 8.1 "Data model"): the task's `reply_files`, checked against the
file's own `replied` events, the derived `reply.files`, the finish stamp, and the single tenant, which has none. And
what an agent sees of them (8.3): the reader's view, the `taskfile` links of the owner agent, the task.replied body."""

import base64
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from server.db import TASK_FIELDS, task_from_row
from server.hosted import files
from server.hosted.accounts import HostedIdentity
from server.hosted.crypto import Keyring
from server.hosted.files import (
    MAX_FILE_BYTES,
    MAX_SECRET_BYTES,
    MAX_TASK_FILES,
    REPLY_DELETED_REASONS,
    REPLY_ITEM_KEYS,
    TASK_CLAIMS,
    TASK_LINK_CONTEXT,
    FileLinks,
    TaskFileLinks,
    clean_reply_files,
)
from server.hosted.records import HostedTasks
from server.hosted.voice import AGENT_TASK_FILE_KEYS, VoiceLinks, for_task_reader
from server.rules import Invalid
from server.task_rules import REPLY_FILES, plan_finish, plan_task_reply, reply_file_ids, reply_state
from server.tokens import Identity

from .conftest import ALLEN as ALLEN_TOKEN
from .conftest import DENIS as DENIS_TOKEN
from .conftest import auth
from .fake_drive import app_task_reply, app_task_reply_files, now_iso
from .hosted_support import DENIS_SUB, id_token, mcp, tool_result
from .test_hosted_attachments import tools_list
from .test_hosted_files import AT, KEY, PUBLIC, item, owner_reader
from .test_hosted_voice import agent, agent_id, bearer

DENIS = Identity("denis")
ALLEN = Identity("agent", "allen")
NOW = "2026-11-28T12:00:00.000Z"
BY_ALLEN = "2026-11-19T10:30:00.000Z"  # a `replied` event whose actor is not the user
BODY = bytes(range(256)) * 20  # 5120 bytes


def task_item(n=1, **over) -> dict:
    """An item of `reply_files` (SPEC 8.1): test_hosted_files.item with reply_id 2 and replied_at (its answered_at) in
    place of answered_at, in REPLY_ITEM_KEYS order."""
    asked = {("replied_at" if key == "answered_at" else key): value for key, value in item(n).items()}
    return {**{key: asked.get(key) for key in REPLY_ITEM_KEYS}, "reply_id": 2, **over}


def put_task_file(drive, task_id=1, n=1, content=BODY, secret=False, **props) -> str:
    """A file in the user's appDataFolder as the apps store a reply's file (SPEC 8.1): tfile-<task id>-<n>-<unix ms>,
    appProperties task_id, kind "file", file (= n) and secret ("1" / "0"); `props` change or add appProperties.
    Returns its Drive id."""
    app_properties = {"task_id": str(task_id), "kind": "file", "file": str(n), "secret": "1" if secret else "0",
                      **props}
    return drive.add(f"tfile-{task_id}-{n}-{1700000000000 + len(drive.files)}", content, app_properties,
                     mime="application/octet-stream")


def sent_item(drive, n, task_id=1, secret=False, **over) -> dict:
    """The item of a file the app has just uploaded (put_task_file): its Drive id and size."""
    return task_item(n, file_id=put_task_file(drive, task_id, n, secret=secret), secret=secret, bytes=len(BODY), **over)


def created(at="2026-11-19T09:00:00.000Z") -> dict:
    return {"id": 1, "task_id": 1, "at": at, "actor": "allen", "kind": "created", "payload": {}}


def replied(event_id=2, at=AT, actor="denis", files=()) -> dict:
    return {"id": event_id, "task_id": 1, "at": at, "actor": actor, "kind": "replied",
            "payload": {"text": "x", "preset": None, "files": list(files)}}


def open_task(**over) -> dict:
    return {"id": 1, "agent": "allen", "status": "active", "resolution": None, "finished_at": None, **over}


def tasks_of(hosted) -> HostedTasks:
    """Denis's tasks as the bridge reads them (no agent: the list as checked, nothing of a reader's view)."""
    return HostedTasks(hosted.app.state.pool.records(DENIS_SUB))


def rewrite_task(drive, task_id: int, drop: tuple = (), **fields) -> None:
    """Change top-level fields of the task file (or `drop` some), as any client of the user's Drive could."""
    file = drive.file(f"task-{task_id}.json")
    doc = json.loads(file["content"])
    for key in drop:
        doc.pop(key, None)
    doc.update(fields)
    drive.write(file["id"], doc, {})


GOOD = task_item(1)
EVENTS = [created(), replied(2), replied(3, BY_ALLEN, actor="allen")]


# ---------- the list (pure) ----------

def test_constants():
    assert MAX_TASK_FILES == 30 and files.MAX_FILES == 6
    assert REPLY_ITEM_KEYS == ("id", "name", "mime", "bytes", "file_id", "secret", "uploaded_at", "reply_id",
                               "replied_at", "downloaded_at", "blocked_at", "deleted_at", "deleted_reason")
    assert REPLY_DELETED_REASONS == ("sent", "finished", "expired")
    assert REPLY_FILES == "reply_files"
    assert list(GOOD) == list(REPLY_ITEM_KEYS)


def test_clean_reply_files_keeps_the_items_of_several_replies():
    later = "2026-11-20T10:00:00.000Z"
    events = [created(), replied(2), {"id": 3, "actor": "allen", "kind": "answered", "at": BY_ALLEN, "payload": {}},
              replied(4, later)]
    value = [task_item(1, name="../a/b.PDF", mime="Application/PDF"), task_item(2, mime="nonsense"),
             task_item(3, reply_id=4, replied_at=later)]
    out = clean_reply_files(value, events)
    assert [(f["id"], f["reply_id"], f["replied_at"]) for f in out] == [(1, 2, AT), (2, 2, AT), (3, 4, later)]
    assert all(list(f) == list(REPLY_ITEM_KEYS) for f in out)
    assert out[0]["name"] == "a_b.PDF" and out[0]["mime"] == "application/pdf"
    assert out[1]["mime"] == "application/octet-stream"
    assert out[2] == task_item(3, reply_id=4, replied_at=later)
    assert clean_reply_files([task_item(1, extra="x")], events)[0].keys() == set(REPLY_ITEM_KEYS)


@pytest.mark.parametrize("bad", [
    pytest.param(task_item(2, bytes=0), id="bytes 0"),
    pytest.param(task_item(2, bytes=MAX_FILE_BYTES + 1), id="bytes over"),
    pytest.param(task_item(2, secret=True, bytes=MAX_SECRET_BYTES + 1), id="secret bytes over"),
    pytest.param(task_item(2, secret="true"), id="secret a string"),
    pytest.param(task_item(2, file_id="a/b"), id="file_id a path"),
    pytest.param(task_item(2, deleted_at=NOW), id="file_id with deleted_at"),
    pytest.param(task_item(2, file_id=None), id="file_id null without deleted_at"),
    pytest.param(task_item(2, file_id=None, deleted_at=NOW, deleted_reason="closed"), id="reason closed"),
    pytest.param(task_item(2, file_id=None, deleted_at=NOW, deleted_reason="replaced"), id="reason replaced"),
    pytest.param(task_item(2, file_id=None, deleted_at=NOW, deleted_reason="reopened"), id="reason reopened"),
    pytest.param(task_item(2, file_id=None, deleted_at=NOW, deleted_reason="gone"), id="reason gone"),
    pytest.param(task_item(2, deleted_reason="sent"), id="reason without deleted_at"),
    pytest.param(task_item(2, reply_id=9), id="reply_id names no event"),
    pytest.param(task_item(2, reply_id=1, replied_at=created()["at"]), id="reply_id names created"),
    pytest.param(task_item(2, reply_id=3, replied_at=BY_ALLEN), id="reply_id names allen's replied"),
    pytest.param(task_item(2, replied_at="2026-11-19T10:00:00.001Z"), id="replied_at not the reply's at"),
    pytest.param(task_item(2, reply_id=True), id="reply_id True"),
    pytest.param(task_item(2, reply_id="2"), id="reply_id a string"),
    pytest.param(task_item(2, reply_id=0), id="reply_id 0"),
    pytest.param(task_item(2, replied_at=""), id="replied_at empty"),
    pytest.param({key: value for key, value in task_item(2).items() if key != "reply_id"}, id="a key missing"),
    pytest.param("x", id="not an object"),
    pytest.param(task_item(1, name="other.pdf"), id="a repeated id"),
])
def test_clean_reply_files_drops(bad):
    assert clean_reply_files([GOOD, bad], EVENTS) == [GOOD]


def test_seventh_item_of_one_reply_and_the_31st_item_are_dropped():
    seven = [task_item(n) for n in range(1, 8)]
    assert [f["id"] for f in clean_reply_files(seven, EVENTS)] == [1, 2, 3, 4, 5, 6]
    # kept items are counted: a dropped one takes no place
    assert [f["id"] for f in clean_reply_files([task_item(99, bytes=0), *seven], EVENTS)] == [1, 2, 3, 4, 5, 6]
    # six replies of six items: the first 30 are kept
    stamps = {reply: f"2026-11-19T1{reply}:00:00.000Z" for reply in range(2, 8)}
    events = [created(), *(replied(reply, at) for reply, at in stamps.items())]
    value = [task_item((reply - 2) * 6 + k, reply_id=reply, replied_at=at) for reply, at in stamps.items()
             for k in range(1, 7)]
    out = clean_reply_files(value, events)
    assert len(value) == 36 and [f["id"] for f in out] == list(range(1, MAX_TASK_FILES + 1))
    assert [f["reply_id"] for f in out][-1] == 6


@pytest.mark.parametrize("reason", ["sent", "finished", "expired"])
def test_each_reply_reason_is_accepted(reason):
    gone = task_item(1, file_id=None, deleted_at=NOW, deleted_reason=reason)
    assert clean_reply_files([gone], EVENTS) == [gone]


@pytest.mark.parametrize("value", [None, {}, "x"])
def test_not_a_list_reads_as_empty(value):
    assert clean_reply_files(value, EVENTS) == []
    for events in (None, {}, "x"):
        assert clean_reply_files([GOOD], events) == []
    # junk in the events is no event: an id True is not reply 1, a junk `at` names nothing
    junk = [created(), "junk", {**replied(2), "id": True}, {**replied(3), "at": ["x"]}]
    assert clean_reply_files([task_item(1, reply_id=1), task_item(2, reply_id=3)], junk) == []


def test_reply_state_files():
    first = replied(2, files=[task_item(1), task_item(2)])
    answered = {"id": 3, "actor": "allen", "kind": "answered", "at": BY_ALLEN, "payload": {"text": "ok"}}
    second = replied(4, "2026-11-20T10:00:00.000Z", files=[task_item(3, reply_id=4)])
    assert reply_state([created(), first])["reply"]["files"] == [1, 2]
    assert reply_state([created(), first, answered, second]) == {
        "reply": {"id": 4, "at": "2026-11-20T10:00:00.000Z", "text": "x", "preset": None, "files": [3]},
        "reply_pending": True}
    old = {"id": 5, "actor": "denis", "kind": "replied", "at": "e", "payload": {"text": "Thanks", "preset": None}}
    assert reply_state([created(), first, old])["reply"]["files"] == []  # an event without files
    # whatever the file holds, `files` is ids and nothing raises
    for junk, ids in (("x", []), ([1, "a", None, {"id": "3"}, {"id": True}, {"id": 4}], [4]), (None, []),
                      ({"id": 1}, [])):
        event = {"id": 6, "actor": "denis", "kind": "replied", "at": "f", "payload": {"text": "t", "files": junk}}
        assert reply_state([created(), event])["reply"]["files"] == ids, junk
    assert reply_state([{"id": 6, "actor": "denis", "kind": "replied", "at": "f", "payload": "junk"}])["reply"][
        "files"] == []
    assert reply_file_ids(None) == reply_file_ids("x") == reply_file_ids({}) == []


def test_plan_finish_stamps_live_items_only():
    live, secret = task_item(1), task_item(3, secret=True, bytes=10)
    gone = task_item(2, file_id=None, deleted_at="2026-11-19T11:00:00.000Z", deleted_reason="sent")
    for status in ("done", "dropped"):
        _, change = plan_finish(ALLEN, status, "ok", lambda: open_task(reply_files=[live, gone, secret]), NOW)
        assert change.updates[REPLY_FILES] == [
            {**live, "file_id": None, "deleted_at": NOW, "deleted_reason": "finished"}, gone,
            {**secret, "file_id": None, "deleted_at": NOW, "deleted_reason": "finished"}]
        assert change.payload == {"resolution": "ok", "previous_status": "active"}
    for stored in ([], [gone]):
        _, change = plan_finish(ALLEN, "done", None, lambda stored=stored: open_task(reply_files=stored), NOW)
        assert REPLY_FILES not in change.updates
    _, change = plan_finish(ALLEN, "dropped", None, open_task, NOW)  # the single tenant's task dict: no such key
    assert set(change.updates) == {"status", "resolution", "finished_at", "updated_at"}


# ---------- the hosted bridge ----------

def test_new_hosted_task_is_written_with_an_empty_list(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    made = tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    assert made[REPLY_FILES] == [] and drive.task_doc(1)[REPLY_FILES] == []
    got = tool_result(mcp(hosted, token, "task_get", {"id": 1}))
    assert got[REPLY_FILES] == [] and got["reply"] is None


def test_an_older_task_file_reads_as_empty(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    rewrite_task(drive, 1, drop=(REPLY_FILES,))
    assert REPLY_FILES not in drive.task_doc(1)
    assert tool_result(mcp(hosted, token, "task_get", {"id": 1}))[REPLY_FILES] == []
    assert tasks_of(hosted).get(1)[REPLY_FILES] == []
    updated = tool_result(mcp(hosted, token, "task_update", {"id": 1, "next_check": "2026-12-01"}))
    assert updated[REPLY_FILES] == [] and updated["next_check"] == "2026-12-01"
    assert REPLY_FILES not in drive.task_doc(1)  # the bridge adds nothing to an older file
    app_task_reply(drive, 1, "Status?")  # an app that doesn't know files
    assert tasks_of(hosted).get(1)["reply"]["files"] == []
    rewrite_task(drive, 1, reply_files=None)
    assert tasks_of(hosted).get(1)[REPLY_FILES] == []
    assert tool_result(mcp(hosted, token, "task_done", {"id": 1}))["status"] == "done"


def test_several_replies_coexist_and_a_later_reply_drops_none(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    first = app_task_reply_files(drive, 1, [sent_item(drive, 1), sent_item(drive, 2, secret=True)])
    assert first["events"][-1]["id"] == 2 and first["events"][-1]["payload"]["text"] == "2 files attached"
    task = tasks_of(hosted).get(1)
    assert [(f["id"], f["reply_id"]) for f in task[REPLY_FILES]] == [(1, 2), (2, 2)]
    assert task["reply"]["files"] == [1, 2] and task["reply_pending"] is True
    tool_result(mcp(hosted, token, "task_answer", {"id": 1, "text": "Got them"}))
    second = app_task_reply_files(drive, 1, [sent_item(drive, 3)], text="One more")
    task = tasks_of(hosted).get(1)
    assert task[REPLY_FILES] == [*first[REPLY_FILES], *second["events"][-1]["payload"]["files"]]
    assert [(f["id"], f["reply_id"]) for f in task[REPLY_FILES]] == [(1, 2), (2, 2), (3, 4)]
    assert task["reply"] == {"id": 4, "at": second["events"][-1]["at"], "text": "One more", "preset": None,
                             "files": [3]}
    app_task_reply(drive, 1, "Any news?")  # a reply without files
    task = tasks_of(hosted).get(1)
    assert [f["id"] for f in task[REPLY_FILES]] == [1, 2, 3]
    assert (task["reply"]["text"], task["reply"]["files"]) == ("Any news?", [])
    assert tool_result(mcp(hosted, token, "task_get", {"id": 1}))["reply"]["files"] == []


def test_the_list_is_as_stored_on_a_finished_task(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    stored = app_task_reply_files(drive, 1, [sent_item(drive, 1), sent_item(drive, 2, secret=True)])[REPLY_FILES]
    tool_result(mcp(hosted, token, "task_done", {"id": 1, "resolution": "Shipped"}))
    doc = drive.task_doc(1)
    assert doc[REPLY_FILES] == [{**f, "file_id": None, "deleted_at": doc["finished_at"], "deleted_reason": "finished"}
                                for f in stored]
    task = tasks_of(hosted).get(1)
    assert task["status"] == "done" and task[REPLY_FILES] == doc[REPLY_FILES]
    assert [(f["id"], f["file_id"], f["deleted_reason"]) for f in task[REPLY_FILES]] == [
        (1, None, "finished"), (2, None, "finished")]
    assert task["reply"]["files"] == [1, 2]
    assert doc["events"][-1]["kind"] == "done"
    assert doc["events"][-1]["payload"] == {"resolution": "Shipped", "previous_status": "active"}


def test_the_bridges_writes_drop_bad_items(hosted, google):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    good = app_task_reply_files(drive, 1, [sent_item(drive, 1)])[REPLY_FILES][0]
    stray = {**good, "id": 2, "reply_id": 9}  # names no event of the file
    rewrite_task(drive, 1, reply_files=[good, stray])
    assert tasks_of(hosted).get(1)[REPLY_FILES] == [good]
    assert drive.task_doc(1)[REPLY_FILES] == [good, stray]  # a read writes nothing
    tool_result(mcp(hosted, token, "task_update", {"id": 1, "waiting_on": "Supplier"}))
    assert drive.task_doc(1)[REPLY_FILES] == [good]


def test_no_tool_or_rest_body_writes_reply_files(client, hosted, google):
    for body in ({"text": "x", "files": []}, {"text": "x", REPLY_FILES: []}):
        with pytest.raises(Invalid) as refused:
            plan_task_reply(DENIS, body, open_task, NOW)
        assert "unknown fields" in str(refused.value)
    # the single tenant's REST, the human's token
    task_id = client.post("/api/tasks", json={"title": "Chase"}, headers=auth(ALLEN_TOKEN)).json()["id"]
    for body in ({"text": "x", "files": []}, {"text": "x", REPLY_FILES: [GOOD]}):
        response = client.post(f"/api/tasks/{task_id}/reply", json=body, headers=auth(DENIS_TOKEN))
        assert response.status_code == 422, body
    # the hosted bridge: REST bodies and the tools
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    for method, path, body in (("PATCH", "/api/tasks/1", {REPLY_FILES: []}),
                               ("POST", "/api/tasks", {"title": "Forged", REPLY_FILES: [GOOD]})):
        response = hosted.request(method, path, json=body, headers=bearer(token))
        assert response.status_code == 422 and REPLY_FILES in response.text, (method, path)
    tools = tools_list(hosted, token)
    for name in ("task_create", "task_update", "task_answer", "task_ask", "task_unask", "task_done", "task_drop"):
        assert REPLY_FILES not in tools[name]["inputSchema"].get("properties", {}), name
    mcp(hosted, token, "task_update", {"id": 1, "title": "Chase them", REPLY_FILES: [GOOD]})
    mcp(hosted, token, "task_create", {"title": "Forged", REPLY_FILES: [GOOD]})
    for name in [f["name"] for f in drive.files.values() if f["name"].startswith("task-")]:
        assert not json.loads(drive.file(name)["content"]).get(REPLY_FILES), name


def test_single_tenant_always_empty(client, settings):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("select " + ", ".join(f"null as {f}" for f in TASK_FIELDS)).fetchone()
    assert task_from_row(row)[REPLY_FILES] == []
    task_id = client.post("/api/tasks", json={"title": "Chase"}, headers=auth(ALLEN_TOKEN)).json()["id"]
    reply = client.post(f"/api/tasks/{task_id}/reply", json={"text": "Status?"}, headers=auth(DENIS_TOKEN))
    assert reply.status_code == 200
    shown = (reply.json(), client.get("/api/tasks", headers=auth(DENIS_TOKEN)).json()["tasks"][0],
             client.get(f"/api/tasks/{task_id}", headers=auth(DENIS_TOKEN)).json())
    for task in shown:
        assert task[REPLY_FILES] == [] and task["reply"]["files"] == []
    done = client.post(f"/api/tasks/{task_id}/done", json={"resolution": "ok"}, headers=auth(ALLEN_TOKEN))
    assert done.status_code == 200 and (done.json()["status"], done.json()[REPLY_FILES]) == ("done", [])
    with sqlite3.connect(settings.db_path) as db:
        assert REPLY_FILES not in [column[1] for column in db.execute("PRAGMA table_info(tasks)")]


def test_helpers_write_what_the_apps_write(hosted, google):
    """app_task_reply_files and put_task_file, which later tests build on (SPEC 8.1, 8.2 step 3)."""
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    file_id = put_task_file(drive, 1, 1, secret=True)
    meta = drive.files[file_id]
    assert meta["name"].startswith("tfile-1-1-") and meta["mimeType"] == "application/octet-stream"
    assert meta["appProperties"] == {"task_id": "1", "kind": "file", "file": "1", "secret": "1"}
    before = drive.file("task-1.json")["appProperties"]
    gone = task_item(7, file_id=None, deleted_at=now_iso(), deleted_reason="sent")
    rewrite_task(drive, 1, reply_files=[gone, "junk", GOOD])
    doc = app_task_reply_files(drive, 1, [task_item(8, file_id=file_id, secret=True, bytes=len(BODY))],
                               preset="status")
    event = doc["events"][-1]
    assert (event["id"], event["actor"], event["kind"]) == (2, "denis", "replied")
    assert event["payload"]["text"] == "What's the status?" and event["payload"]["preset"] == "status"
    written = event["payload"]["files"]
    assert [(f["id"], f["reply_id"], f["replied_at"]) for f in written] == [(8, 2, event["at"])]
    assert doc[REPLY_FILES] == ["junk", GOOD, *written]  # the deleted item pruned, the rest untouched
    assert (doc["updated_at"], doc["writer"]) == (event["at"], "app")
    assert drive.file("task-1.json")["appProperties"] == {**before, "rev": str(doc["rev"])}


# ---------- what an agent sees (SPEC 8.3): the links ----------

TEN = list(AGENT_TASK_FILE_KEYS)  # what an agent sees of a reply's file
LINKED = [*TEN, "url", "url_expires_at"]


def task_file_links(clock=time.time) -> TaskFileLinks:
    return TaskFileLinks(Keyring(KEY), PUBLIC, clock=clock)


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def task_sealed(context=TASK_LINK_CONTEXT, keyring=None, **change) -> str:
    """A token sealed here with a task file link's claims, `change`d (`...` drops a claim)."""
    claims = {"user": "u1", "agent": "ag_1", "task_id": 7, "reply_id": 4, "file_id": "fid-1", "n": 1,
              "exp": int(time.time()) + 600, **change}
    claims = {key: value for key, value in claims.items() if value is not ...}
    return (keyring or Keyring(KEY)).encrypt(json.dumps(claims), context)


def test_task_file_links_roundtrip():
    assert TASK_LINK_CONTEXT == "taskfile"
    assert TASK_CLAIMS == {"user": str, "agent": str, "task_id": int, "reply_id": int, "file_id": str, "n": int,
                           "exp": int}
    assert AGENT_TASK_FILE_KEYS == ("id", "reply_id", "name", "mime", "bytes", "secret", "downloaded_at", "deleted_at",
                                    "deleted_reason", "blocked_at")
    links = task_file_links()
    url, expires = links.url_for("u1", "ag_1", 7, 4, "fid-1", 2)
    assert url.startswith(f"{PUBLIC}/media/v1/") and expires.endswith("Z")
    opened = links.open(token_of(url))
    assert set(opened) == set(TASK_CLAIMS)
    assert {k: v for k, v in opened.items() if k != "exp"} == {
        "user": "u1", "agent": "ag_1", "task_id": 7, "reply_id": 4, "file_id": "fid-1", "n": 2}
    assert abs(opened["exp"] - time.time() - 3600) < 5
    assert json.loads(Keyring(KEY).decrypt(token_of(url), "taskfile")) == opened  # sealed under its own context
    assert links.url_for("u1", "ag_1", 7, 4, "fid-1", 2)[0] != url  # a new link each time


def test_task_file_token_tampered_expired_wrong_shape():
    links = task_file_links()
    good = task_sealed()
    assert links.open(good) is not None
    assert links.open(good[:-5] + ("A" if good[-5] != "A" else "B") + good[-4:]) is None
    for junk in ("garbage", "", None, 5, "v1.k1.garbage", "A" * 3000):
        assert links.open(junk) is None, junk
    assert links.open(task_sealed(keyring=Keyring(base64.b64encode(b"z" * 32).decode()))) is None  # another key
    for key in TASK_CLAIMS:
        assert links.open(task_sealed(**{key: ...})) is None, key  # a claim missing
    assert links.open(task_sealed(extra="x")) is None
    assert links.open(task_sealed(ask_id=7)) is None  # an answer's claim besides
    for key, bad in (("n", 0), ("n", -1), ("n", "1"), ("reply_id", True), ("reply_id", 0), ("reply_id", "4"),
                     ("task_id", True), ("task_id", "7"), ("file_id", ""), ("file_id", 5), ("agent", ""),
                     ("user", None), ("exp", "9"), ("exp", 1.5)):
        assert links.open(task_sealed(**{key: bad})) is None, (key, bad)
    # expired by the clock: valid for LINK_TTL, not at its exp
    now = [1_000_000.0]
    clocked = task_file_links(clock=lambda: now[0])
    url, expires = clocked.url_for("u1", "ag_1", 7, 4, "fid-1", 1)
    assert clocked.open(token_of(url)) is not None
    now[0] += 3599
    assert clocked.open(token_of(url)) is not None
    now[0] += 1
    assert clocked.open(token_of(url)) is None
    assert expires == datetime.fromtimestamp(1_003_600, tz=timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def test_ask_file_and_task_file_tokens_open_nothing_of_each_other():
    keyring = Keyring(KEY)
    voice, answers, replies = VoiceLinks(keyring, PUBLIC), FileLinks(keyring, PUBLIC), TaskFileLinks(keyring, PUBLIC)
    media = token_of(voice.link("u1", "ag_1", 7, "fid-1")[0])
    answer = token_of(answers.url_for("u1", "ag_1", 7, AT, "fid-1", 1)[0])
    reply = token_of(replies.url_for("u1", "ag_1", 7, 4, "fid-1", 1)[0])
    assert replies.open(answer) is None and replies.open(media) is None
    assert answers.open(reply) is None and voice.open(reply) is None
    assert voice.open(media) and answers.open(answer) and replies.open(reply)
    # the context decides, not the claims: a task link's claims sealed as `file` or `media` open nothing either
    for context in ("file", "media"):
        assert replies.open(task_sealed(context)) is None and answers.open(task_sealed(context)) is None


# ---------- the reader's view (pure) ----------

def stored_task(items, status="active", events=None) -> dict:
    """A task as the bridge reads it, with `items` in reply_files and in the `replied` event that carried them."""
    return {"id": 3, "agent": "allen", "status": status, "resolution": None, REPLY_FILES: items,
            "events": [created(), replied(2, files=items)] if events is None else events}


def no_stored_keys(shown) -> None:
    text = json.dumps(shown)
    for key in ("file_id", "uploaded_at", "replied_at"):
        assert f'"{key}"' not in text, key
    assert "fid-" not in text


FOUR = [task_item(1), task_item(2, secret=True, bytes=5),
        task_item(3, file_id=None, deleted_at=NOW, deleted_reason="expired"), task_item(4, blocked_at=NOW)]


def test_for_task_reader_owner():
    flinks = task_file_links()
    for status in ("active", "waiting"):
        shown = for_task_reader(stored_task(FOUR, status), owner_reader(), file_links=flinks)
        live, secret, gone, blocked = shown[REPLY_FILES]
        assert list(live) == LINKED
        assert live["url"].startswith(f"{PUBLIC}/media/v1/") and live["url_expires_at"].endswith("Z")
        claims = flinks.open(token_of(live["url"]))
        assert {k: v for k, v in claims.items() if k != "exp"} == {
            "user": "u1", "agent": "ag_1", "task_id": 3, "reply_id": 2, "file_id": "fid-1", "n": 1}
        for other in (secret, gone, blocked):
            assert list(other) == TEN, other
        assert (secret["name"], secret["secret"], blocked["blocked_at"]) == ("report2.pdf", True, NOW)
        assert [f["name"] for f in shown["events"][1]["payload"]["files"]] == [f["name"] for f in FOUR]
        assert all(list(f) == TEN for f in shown["events"][1]["payload"]["files"])  # no url in the history
        no_stored_keys({**shown, REPLY_FILES: [{k: v for k, v in f.items() if k != "url"} for f in shown[REPLY_FILES]]})
    for status in ("done", "dropped"):  # a finished task links nothing
        shown = for_task_reader(stored_task(FOUR, status), owner_reader(), file_links=flinks)
        assert [list(f) for f in shown[REPLY_FILES]] == [TEN] * 4
        no_stored_keys(shown)
    # no links object (what a write returns): the owner's metadata, no url
    shown = for_task_reader(stored_task(FOUR), owner_reader())
    assert [list(f) for f in shown[REPLY_FILES]] == [TEN] * 4 and shown[REPLY_FILES][1]["name"] == "report2.pdf"
    no_stored_keys(shown)
    # the stored task is left as it was
    task = stored_task(FOUR)
    before = json.dumps(task)
    for_task_reader(task, owner_reader(), file_links=flinks)
    for_task_reader(task, owner_reader(), file_urls=False)
    assert json.dumps(task) == before


def test_for_task_reader_other_agent():
    flinks = task_file_links()
    others = (HostedIdentity("agent", "feynman", user="u1", agent_id="ag_2"),
              Identity("agent", "allen"),  # the owner's slug without a user and an agent id: not the owner
              None)
    for reader in others:
        shown = for_task_reader(stored_task(FOUR), reader, file_links=flinks)
        assert [f["id"] for f in shown[REPLY_FILES]] == [1, 2, 3, 4]
        for f in (*shown[REPLY_FILES], *shown["events"][1]["payload"]["files"]):
            assert list(f) == TEN and (f["name"], f["mime"]) == (None, None), (reader, f)
        assert shown[REPLY_FILES][1]["secret"] is True
        assert "/media/v1/" not in json.dumps(shown) and "report" not in json.dumps(shown)
        no_stored_keys(shown)


def test_for_task_reader_webhook_shape():
    items = [task_item(1), task_item(2, secret=True, bytes=5)]
    shown = for_task_reader(stored_task(items), owner_reader(), file_urls=False, file_links=task_file_links())
    normal, secret = shown[REPLY_FILES]
    assert list(normal) == TEN and normal == {key: items[0][key] for key in TEN}
    assert secret == {"id": 2, "reply_id": 2, "secret": True}
    assert "/media/v1/" not in json.dumps(shown) and "report2" not in json.dumps(shown[REPLY_FILES])
    no_stored_keys(shown)


def test_events_files_go_through_the_view_for_everyone():
    one, other = task_item(1), task_item(2, file_id="fid-9")
    events = [created(),
              replied(2, files=[one, "fid-1", 5, None, ["fid-1"]]),
              {"id": 3, "task_id": 1, "at": BY_ALLEN, "actor": "allen", "kind": "updated",
               "payload": {"title": "x", "files": [other]}},
              {"id": 4, "task_id": 1, "at": BY_ALLEN, "actor": "allen", "kind": "answered", "payload": {"text": "ok"}},
              {"id": 5, "task_id": 1, "at": BY_ALLEN, "actor": "denis", "kind": "replied", "payload": "junk"},
              {"id": 6, "task_id": 1, "at": BY_ALLEN, "actor": "denis", "kind": "replied",
               "payload": {"text": "t", "files": "fid-1"}}]
    task = stored_task([], events=events)
    before = json.dumps(task)
    for reader, named in ((owner_reader(), True), (HostedIdentity("agent", "feynman", user="u1", agent_id="ag_2"),
                                                    False)):
        shown = for_task_reader(task, reader, file_links=task_file_links())["events"]
        assert [f["id"] for f in shown[1]["payload"]["files"]] == [1]  # what is not an object is dropped
        assert [f["id"] for f in shown[2]["payload"]["files"]] == [2]  # another kind's files: viewed too
        for f in (*shown[1]["payload"]["files"], *shown[2]["payload"]["files"]):
            assert list(f) == TEN and (f["name"] is not None) is named, f
        assert shown[2]["payload"]["title"] == "x" and shown[1]["payload"]["text"] == "x"
        assert shown[5]["payload"] == {"text": "t", "files": []}  # files that are not a list are none
        assert (shown[0], shown[3], shown[4]) == (events[0], events[3], events[4])
        no_stored_keys(shown)
    assert json.dumps(task) == before  # the stored events are left as they were


# ---------- the reader's view on the hosted bridge ----------

def two_files(drive) -> list:
    """A reply with a normal file (report1.pdf) and a secret one (keys.txt), as the apps write it: reply_files."""
    return app_task_reply_files(drive, 1, [sent_item(drive, 1), sent_item(drive, 2, secret=True, name="keys.txt")])[
        REPLY_FILES]


def test_reply_files_ids_are_the_same_for_every_reader(hosted, google):
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "task_create", {"title": "Chase the supplier"}))
    two_files(drive)
    replies = [tool_result(mcp(hosted, allen, "task_get", {"id": 1}))["reply"],
               tool_result(mcp(hosted, feynman, "task_get", {"id": 1}))["reply"],
               tasks_of(hosted).get(1)["reply"]]
    assert replies[0]["files"] == [1, 2] and replies[0] == replies[1] == replies[2]


def test_owner_gets_url_in_tasks_mine_task_get_and_rest_get_only(hosted, google):
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    stored = two_files(drive)
    reads = (tool_result(mcp(hosted, token, "task_get", {"id": 1})),
             tool_result(mcp(hosted, token, "tasks_mine"))["tasks"][0],
             hosted.get("/api/tasks/1", headers=bearer(token)).json())
    for task in reads:
        normal, secret = task[REPLY_FILES]
        assert list(normal) == LINKED and "/media/v1/" in normal["url"] and list(secret) == TEN
        claims = hosted.app.state.task_file_links.open(token_of(normal["url"]))
        assert {k: v for k, v in claims.items() if k != "exp"} == {
            "user": DENIS_SUB, "agent": owner, "task_id": 1, "reply_id": 2, "file_id": stored[0]["file_id"], "n": 1}
    again = tool_result(mcp(hosted, token, "task_get", {"id": 1}))[REPLY_FILES][0]["url"]
    assert again != reads[0][REPLY_FILES][0]["url"]  # a new link on every read
    for listed in (tool_result(mcp(hosted, token, "tasks_list"))["tasks"][0],
                   hosted.get("/api/tasks", headers=bearer(token)).json()["tasks"][0]):
        assert [f["name"] for f in listed[REPLY_FILES]] == ["report1.pdf", "keys.txt"]
        assert [list(f) for f in listed[REPLY_FILES]] == [TEN] * 2 and "/media/v1/" not in json.dumps(listed)
    tool_result(mcp(hosted, token, "task_done", {"id": 1}))
    for task in (tool_result(mcp(hosted, token, "task_get", {"id": 1})),
                 tool_result(mcp(hosted, token, "tasks_mine", {"status": "done"}))["tasks"][0],
                 hosted.get("/api/tasks/1", headers=bearer(token)).json()):
        assert task["status"] == "done" and "/media/v1/" not in json.dumps(task)


WRITES = {  # tool: (a write before it, its arguments)
    "task_answer": (None, {"text": "Got them"}),
    "task_update": (None, {"waiting_on": "Supplier"}),
    "task_ask": (None, {"question": "Which colour?"}),
    "task_unask": (("task_ask", {"question": "Which colour?"}), {"note": "Answered in the chat"}),
    "task_done": (None, {"resolution": "Shipped"}),
    "task_drop": (None, {}),
    "task_reopen": (("task_done", {}), {}),
}


@pytest.mark.parametrize("tool", list(WRITES))
def test_what_a_write_returns_goes_through_the_view(hosted, google, tool):
    token = agent(hosted, google)
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    stored = two_files(drive)
    before, arguments = WRITES[tool]
    if before is not None:
        tool_result(mcp(hosted, token, before[0], {"id": 1, **before[1]}))
    result = tool_result(mcp(hosted, token, tool, {"id": 1, **arguments}))
    assert [f["id"] for f in result[REPLY_FILES]] == [1, 2]
    assert [list(f) for f in result[REPLY_FILES]] == [TEN] * 2
    assert [f["name"] for f in result[REPLY_FILES]] == ["report1.pdf", "keys.txt"]
    text = json.dumps(result)
    assert "/media/v1/" not in text and all(f["file_id"] not in text for f in stored)


def test_a_secret_file_has_no_url_anywhere(hosted, google):
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/files"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    app_task_reply_files(drive, 1, [sent_item(drive, 1, secret=True, name="keys.txt")])
    reads = (tool_result(mcp(hosted, token, "task_get", {"id": 1})),
             tool_result(mcp(hosted, token, "tasks_mine"))["tasks"][0],
             tool_result(mcp(hosted, token, "tasks_list"))["tasks"][0],
             hosted.get("/api/tasks/1", headers=bearer(token)).json(),
             hosted.get("/api/tasks", headers=bearer(token)).json()["tasks"][0])
    for task in reads:
        (secret,) = task[REPLY_FILES]
        assert list(secret) == TEN and secret["secret"] is True and "/media/v1/" not in json.dumps(task)
    hosted.post("/api/events", json={"task_id": 1, "kind": "replied"}, headers=id_token(google)).raise_for_status()
    assert hosted.app.state.webhooks.flush(5)
    text = google.webhook_calls[-1].content.decode()
    assert json.loads(text)["task"][REPLY_FILES] == [{"id": 1, "reply_id": 2, "secret": True}]
    assert "/media/v1/" not in text and "keys.txt" not in text


def test_another_agent_sees_no_name_no_mime_no_url(hosted, google):
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "task_create", {"title": "Chase the supplier"}))
    two_files(drive)
    got = tool_result(mcp(hosted, feynman, "task_get", {"id": 1}))
    rest = hosted.get("/api/tasks/1", headers=bearer(feynman)).json()
    reads = (got, rest, tool_result(mcp(hosted, feynman, "tasks_list"))["tasks"][0],
             hosted.get("/api/tasks", headers=bearer(feynman)).json()["tasks"][0])
    for task in reads:
        assert [f["id"] for f in task[REPLY_FILES]] == [1, 2] and task["reply"]["files"] == [1, 2]
        for f in task[REPLY_FILES]:
            assert list(f) == TEN and (f["name"], f["mime"]) == (None, None), f
        text = json.dumps(task)
        assert "/media/v1/" not in text and "report1.pdf" not in text and "keys.txt" not in text
    for task in (got, rest):  # the history too
        (event,) = [e for e in task["events"] if e["kind"] == "replied"]
        assert [(f["id"], f["name"], f["mime"]) for f in event["payload"]["files"]] == [(1, None, None),
                                                                                       (2, None, None)]


def test_no_file_id_in_any_agents_view(hosted, google):
    allen = agent(hosted, google)
    feynman = agent(hosted, google, "Feynman")
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, allen, "task_create", {"title": "Chase the supplier"}))
    two_files(drive)
    app_task_reply_files(drive, 1, [sent_item(drive, 3)], text="One more")
    file_ids = [f["file_id"] for f in tasks_of(hosted).get(1)[REPLY_FILES]]
    assert len(file_ids) == 3 and all(file_ids)
    seen = []
    for token in (allen, feynman):
        seen += [tool_result(mcp(hosted, token, "tasks_mine")), tool_result(mcp(hosted, token, "tasks_list")),
                 tool_result(mcp(hosted, token, "task_get", {"id": 1})),
                 hosted.get("/api/tasks", headers=bearer(token)).json(),
                 hosted.get("/api/tasks/1", headers=bearer(token)).json()]
    assert [f["id"] for f in seen[0]["tasks"][0][REPLY_FILES]] == [1, 2, 3]
    text = json.dumps(seen)
    for file_id in file_ids:
        assert file_id not in text, file_id
    for key in ("file_id", "uploaded_at", "replied_at"):
        assert f'"{key}"' not in text, key


def test_task_webhook_body_has_no_file_urls_or_file_ids(hosted, google):
    """The task.replied webhook (SPEC 8.3): the task as its owner gets it without links, a secret file only as {id,
    reply_id, secret: true}, reply with the ids of its files. The agent reads tasks_mine / task_get for links."""
    token = agent(hosted, google)
    owner = agent_id(hosted, google)
    hosted.patch(f"/api/agents/{owner}", json={"webhook_url": "https://hooks.example/files"},
                 headers=id_token(google)).raise_for_status()
    drive = google.drive(DENIS_SUB)
    tool_result(mcp(hosted, token, "task_create", {"title": "Chase the supplier"}))
    stored = two_files(drive)
    sent = hosted.post("/api/events", json={"task_id": 1, "kind": "replied"}, headers=id_token(google))
    assert sent.status_code == 202 and sent.json() == {"queued": 1, "fed": 1}
    assert hosted.app.state.webhooks.flush(5)
    text = google.webhook_calls[-1].content.decode()
    body = json.loads(text)
    normal, secret = body["task"][REPLY_FILES]
    assert list(normal) == TEN and normal == {key: stored[0][key] for key in TEN}
    assert secret == {"id": 2, "reply_id": 2, "secret": True}
    assert body["reply"] == {"text": "2 files attached", "preset": None, "files": [1, 2]}
    assert body["task"]["reply"]["files"] == [1, 2] and "events" not in body["task"]
    assert "/media/v1/" not in text and "keys.txt" not in text
    for f in stored:
        assert f["file_id"] not in text
    for key in ("file_id", "uploaded_at", "replied_at"):
        assert f'"{key}"' not in text, key
    (fed,) = hosted.app.state.feed.read(owner, "0")["events"]
    assert {key: value for key, value in fed.items() if key not in ("id", "at")} == {
        "type": "task.replied", "task_id": 1, "event": body["event"]}
    # the agent's own read still carries the link
    assert "/media/v1/" in tool_result(mcp(hosted, token, "task_get", {"id": 1}))[REPLY_FILES][0]["url"]
