"""Hosted bridge: files in task replies (SPEC section 8.1 "Data model"): the task's `reply_files`, checked against the
file's own `replied` events, the derived `reply.files`, the finish stamp, and the single tenant, which has none."""

import json
import sqlite3

import pytest

from server.db import TASK_FIELDS, task_from_row
from server.hosted import files
from server.hosted.files import (
    MAX_FILE_BYTES,
    MAX_SECRET_BYTES,
    MAX_TASK_FILES,
    REPLY_DELETED_REASONS,
    REPLY_ITEM_KEYS,
    clean_reply_files,
)
from server.hosted.records import HostedTasks
from server.rules import Invalid
from server.task_rules import REPLY_FILES, plan_finish, plan_task_reply, reply_file_ids, reply_state
from server.tokens import Identity

from .conftest import ALLEN as ALLEN_TOKEN
from .conftest import DENIS as DENIS_TOKEN
from .conftest import auth
from .fake_drive import app_task_reply, app_task_reply_files, now_iso
from .hosted_support import DENIS_SUB, mcp, tool_result
from .test_hosted_attachments import tools_list
from .test_hosted_files import AT, item
from .test_hosted_voice import agent, bearer

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
