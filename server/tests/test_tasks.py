"""Tasks (SPEC.md "Tasks and iPhone tab bar (v3)"): REST, permissions, state machine, ordering, migration."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from server.app import MAX_BODY, create_app
from server.db import Database
from server.tasks import TaskService
from server.tokens import Identity

from .conftest import ALLEN, DENIS, HAMILTON, auth

TASK_KEYS = {
    "id", "agent", "title", "context", "status", "waiting_on", "next_check", "links", "ref", "resolution",
    "created_at", "updated_at", "finished_at", "reply", "reply_pending",
}


def create(client, token=ALLEN, **fields):
    fields.setdefault("title", "Get the supplier to confirm the fabric")
    response = client.post("/api/tasks", json=fields, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


def patch(client, task_id, token=ALLEN, **fields):
    return client.patch(f"/api/tasks/{task_id}", json=fields, headers=auth(token))


def finish(client, task_id, action="done", token=ALLEN, resolution=None):
    body = {} if resolution is None else {"resolution": resolution}
    return client.post(f"/api/tasks/{task_id}/{action}", json=body, headers=auth(token))


def reopen(client, task_id, token=ALLEN):
    return client.post(f"/api/tasks/{task_id}/reopen", headers=auth(token))


def get(client, task_id, token=DENIS):
    return client.get(f"/api/tasks/{task_id}", headers=auth(token)).json()


# ---------- create / get ----------

def test_create_returns_full_task(client):
    task = create(
        client,
        title="  Get the supplier to confirm the new fabric  ",
        context="Order form rejects size XS\r\nsee https://example.com/order",
        status="waiting",
        waiting_on="Supplier support reply",
        next_check="2026-11-03",
        links=[{"title": "Ticket", "url": "https://example.com/t/1"}, {"url": "http://a.b/c"}],
        ref="TG 5591",
    )
    assert set(task) == TASK_KEYS
    assert (task["agent"], task["title"], task["status"]) == ("allen", "Get the supplier to confirm the new fabric",
                                                              "waiting")
    assert task["context"] == "Order form rejects size XS\nsee https://example.com/order"
    assert (task["waiting_on"], task["next_check"], task["ref"]) == ("Supplier support reply", "2026-11-03", "TG 5591")
    assert task["links"] == [{"title": "Ticket", "url": "https://example.com/t/1"},
                             {"title": "http://a.b/c", "url": "http://a.b/c"}]
    assert task["resolution"] is None and task["finished_at"] is None
    assert task["created_at"].endswith("Z") and task["created_at"] == task["updated_at"]


def test_create_defaults(client):
    task = create(client, title="Minimal")
    assert task["status"] == "active" and task["links"] == []
    assert (task["context"], task["waiting_on"], task["next_check"], task["ref"]) == (None, None, None, None)
    nulls = create(client, title="Nulls", context=None, status=None, links=None, next_check=None)
    assert nulls["status"] == "active" and nulls["links"] == []


def test_task_ids_have_their_own_sequence(client):
    for _ in range(3):
        assert client.post("/api/asks", json={"title": "Ask?"}, headers=auth(ALLEN)).status_code == 201
    assert create(client)["id"] == 1
    assert client.get("/api/asks/1", headers=auth(ALLEN)).json()["title"] == "Ask?"


def test_get_includes_events_and_404(client):
    task = create(client, next_check="2026-11-03")
    data = get(client, task["id"], token=HAMILTON)
    assert data["events"] == [{"id": 1, "task_id": task["id"], "at": task["created_at"], "actor": "allen",
                               "kind": "created", "payload": {
                                   "title": task["title"], "context": None, "status": "active", "waiting_on": None,
                                   "next_check": "2026-11-03", "links": [], "ref": None}}]
    response = client.get("/api/tasks/999", headers=auth(ALLEN))
    assert response.status_code == 404 and response.json() == {"detail": "no task #999"}
    assert finish(client, 999).status_code == 404 and reopen(client, 999).status_code == 404
    assert patch(client, 999, title="x").status_code == 404


@pytest.mark.parametrize("task_id", ["0", "-5", "9223372036854775808", "99999999999999999999"])
def test_out_of_range_ids_are_404(client, task_id):
    for method, path in [("get", ""), ("post", "/done"), ("post", "/drop"), ("post", "/reopen"), ("patch", "")]:
        kwargs = {"headers": auth(ALLEN)}
        if method == "patch":
            kwargs["json"] = {"title": "x"}
        response = getattr(client, method)(f"/api/tasks/{task_id}{path}", **kwargs)
        assert response.status_code == 404, (method, path, response.text)
        assert response.json() == {"detail": f"no task #{int(task_id)}"}


# ---------- permissions ----------

def test_denis_reads_everything_and_writes_nothing(client):
    task = create(client)
    response = client.post("/api/tasks", json={"title": "Mine?"}, headers=auth(DENIS))
    assert response.status_code == 403 and "read" in response.json()["detail"]
    for response in (patch(client, task["id"], token=DENIS, title="x"),
                     finish(client, task["id"], token=DENIS),
                     finish(client, task["id"], "drop", token=DENIS)):
        assert response.status_code == 403
        assert "allen" in response.json()["detail"]
    assert client.get("/api/tasks", headers=auth(DENIS)).json()["tasks"][0]["id"] == task["id"]
    assert finish(client, task["id"]).status_code == 200
    assert reopen(client, task["id"], token=DENIS).status_code == 403
    assert get(client, task["id"])["status"] == "done"


def test_only_the_owner_agent_writes(client):
    task = create(client)
    for response in (patch(client, task["id"], token=HAMILTON, status="waiting"),
                     finish(client, task["id"], token=HAMILTON),
                     finish(client, task["id"], "drop", token=HAMILTON)):
        assert response.status_code == 403 and "allen" in response.json()["detail"]
    finish(client, task["id"])
    assert reopen(client, task["id"], token=HAMILTON).status_code == 403
    # reading is for everyone
    assert get(client, task["id"], token=HAMILTON)["status"] == "done"
    assert len(get(client, task["id"])["events"]) == 2  # created, done: the rejected calls left nothing


def test_owner_comes_from_the_token(client):
    response = client.post("/api/tasks", json={"title": "Whose?", "agent": "hamilton"}, headers=auth(ALLEN))
    assert response.status_code == 422
    assert create(client, token=HAMILTON)["agent"] == "hamilton"


def test_unauthenticated_is_401(client):
    for method, path in [("get", "/api/tasks"), ("post", "/api/tasks"), ("get", "/api/tasks/1"),
                         ("post", "/api/tasks/1/done")]:
        response = getattr(client, method)(path)
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


# ---------- state machine ----------

def test_status_changes_and_finish(client):
    task = create(client, waiting_on="Supplier", next_check="2026-11-03")
    waiting = patch(client, task["id"], status="waiting").json()
    assert (waiting["status"], waiting["waiting_on"]) == ("waiting", "Supplier")
    active = patch(client, task["id"], status="active").json()
    assert (active["status"], active["waiting_on"]) == ("active", "Supplier")  # kept when the status changes
    time.sleep(0.002)
    done = finish(client, task["id"], resolution="  Shipped  ").json()
    assert (done["status"], done["resolution"]) == ("done", "Shipped")
    assert done["finished_at"] == done["updated_at"] and done["finished_at"] > task["created_at"]
    assert done["waiting_on"] == "Supplier" and done["next_check"] == "2026-11-03"

    reopened = reopen(client, task["id"]).json()
    assert (reopened["status"], reopened["resolution"], reopened["finished_at"]) == ("active", None, None)
    assert reopened["waiting_on"] == "Supplier"
    dropped = finish(client, task["id"], "drop").json()
    assert (dropped["status"], dropped["resolution"]) == ("dropped", None) and dropped["finished_at"]
    assert reopen(client, task["id"]).json()["status"] == "active"

    events = get(client, task["id"])["events"]
    assert [e["kind"] for e in events] == ["created", "status", "status", "done", "reopened", "dropped", "reopened"]
    assert events[1]["payload"] == {"changes": {"status": {"from": "active", "to": "waiting"}}}
    assert events[3]["payload"] == {"resolution": "Shipped", "previous_status": "active"}
    assert events[4]["payload"] == {"previous_status": "done", "resolution": "Shipped",
                                    "finished_at": done["finished_at"]}
    assert events[5]["payload"] == {"resolution": None, "previous_status": "active"}


def test_waiting_task_can_be_finished_directly(client):
    task = create(client, status="waiting")
    assert finish(client, task["id"], "drop", resolution="moot").json()["status"] == "dropped"
    task = create(client, status="waiting")
    assert finish(client, task["id"]).json()["status"] == "done"


@pytest.mark.parametrize("action", ["edit", "status", "done", "drop", "reopen"])
def test_invalid_transitions_are_409(client, action):
    task = create(client)
    if action == "reopen":
        response = reopen(client, task["id"])  # still active
    else:
        finish(client, task["id"])
        response = {
            "edit": lambda: patch(client, task["id"], title="x"),
            "status": lambda: patch(client, task["id"], status="active"),
            "done": lambda: finish(client, task["id"]),
            "drop": lambda: finish(client, task["id"], "drop"),
        }[action]()
    assert response.status_code == 409, response.text
    assert isinstance(response.json()["detail"], str)


def test_patch_null_means_unchanged_and_empty_clears(client):
    task = create(client, title="t", context="c", status="waiting", waiting_on="w", next_check="2026-11-03",
                  links=[{"url": "https://x.y"}], ref="TG 1")
    nulls = {"title": "t2", "context": None, "status": None, "waiting_on": None, "next_check": None,
             "links": None, "ref": None}
    edited = patch(client, task["id"], **nulls).json()
    assert edited["title"] == "t2"
    assert {k: edited[k] for k in ("context", "status", "waiting_on", "next_check", "links", "ref")} == {
        "context": "c", "status": "waiting", "waiting_on": "w", "next_check": "2026-11-03", "links": task["links"],
        "ref": "TG 1"}
    cleared = patch(client, task["id"], context="", waiting_on="", next_check="", links=[], ref="").json()
    assert (cleared["context"], cleared["waiting_on"], cleared["next_check"], cleared["links"], cleared["ref"]) == (
        None, None, None, [], None)
    events = get(client, task["id"])["events"]
    assert [e["kind"] for e in events] == ["created", "edited", "edited"]
    assert events[2]["payload"]["changes"]["next_check"] == {"from": "2026-11-03", "to": None}
    # an empty or no-op patch records nothing and keeps updated_at
    before = get(client, task["id"])
    assert patch(client, task["id"]).json()["updated_at"] == before["updated_at"]
    assert patch(client, task["id"], title="t2", status="waiting").json()["updated_at"] == before["updated_at"]
    assert len(get(client, task["id"])["events"]) == 3


def test_patch_with_status_and_fields_is_one_status_event(client):
    task = create(client)
    patch(client, task["id"], status="waiting", waiting_on="supplier", next_check="2026-11-03")
    event = get(client, task["id"])["events"][-1]
    assert event["kind"] == "status"
    assert event["payload"] == {"changes": {"status": {"from": "active", "to": "waiting"},
                                            "waiting_on": {"from": None, "to": "supplier"},
                                            "next_check": {"from": None, "to": "2026-11-03"}}}


# ---------- validation ----------

@pytest.mark.parametrize("body", [
    {},
    {"title": ""},
    {"title": " \t "},
    {"title": "x" * 201},
    {"title": "ok", "context": "x" * 4001},
    {"title": "ok", "status": "done"},
    {"title": "ok", "status": "dropped"},
    {"title": "ok", "status": "paused"},
    {"title": "ok", "waiting_on": "x" * 201},
    {"title": "ok", "next_check": "2026-13-01"},
    {"title": "ok", "next_check": "2026-02-30"},
    {"title": "ok", "next_check": "Nov 3"},
    {"title": "ok", "next_check": "20261103"},
    {"title": "ok", "next_check": "2026-11-03T10:00"},
    {"title": "ok", "next_check": 20261103},
    {"title": "ok", "links": [{"url": "ftp://example.com/x"}]},
    {"title": "ok", "links": [{"url": f"https://e.com/{i}"} for i in range(11)]},
    {"title": "ok", "ref": "x" * 101},
    {"title": "ok", "options": ["a"]},
    {"title": "ok", "priority": "urgent"},
    {"title": "ok", "resolution": "x"},
    {"title": "ok", "finished_at": "2026-11-03T00:00:00.000Z"},
])
def test_create_validation_is_422(client, body):
    response = client.post("/api/tasks", json=body, headers=auth(ALLEN))
    assert response.status_code == 422, body
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_other_validation_errors_are_422(client):
    task = create(client)
    checks = [
        patch(client, task["id"], title=""),
        patch(client, task["id"], status="done"),
        patch(client, task["id"], next_check="tomorrow"),
        patch(client, task["id"], colour="red"),
        finish(client, task["id"], resolution="x" * 1001),
        client.post(f"/api/tasks/{task['id']}/drop", json={"reason": "x"}, headers=auth(ALLEN)),
        client.get("/api/tasks?status=pending", headers=auth(ALLEN)),
        client.get("/api/tasks?status=closed", headers=auth(ALLEN)),
        client.get("/api/tasks?limit=0", headers=auth(ALLEN)),
        client.get("/api/tasks?limit=abc", headers=auth(ALLEN)),
    ]
    for response in checks:
        assert response.status_code == 422, response.text
        assert isinstance(response.json()["detail"], str)
    assert "done or drop" in patch(client, task["id"], status="done").json()["detail"]
    assert get(client, task["id"])["status"] == "active"
    no_body = client.post(f"/api/tasks/{task['id']}/done", headers=auth(ALLEN))
    assert no_body.status_code == 200 and no_body.json()["resolution"] is None


def test_limits_and_control_characters(client):
    task = create(client, title="x" * 200, context="y" * 4000, waiting_on="w" * 200, ref="r" * 100,
                  links=[{"title": "t", "url": f"https://e.com/{i}"} for i in range(10)])
    assert len(task["title"]) == 200 and len(task["waiting_on"]) == 200 and len(task["links"]) == 10
    dirty = create(client, title="t\u001b]0;pwned\u0007", context="a\tb\r\nc\u009b2J", waiting_on="supp\u0008lier",
                   ref="TG\u007f 1", next_check=" 2026-11-03 ")
    assert (dirty["title"], dirty["context"], dirty["waiting_on"], dirty["ref"], dirty["next_check"]) == (
        "t]0;pwned", "a\tb\nc2J", "supplier", "TG 1", "2026-11-03")
    response = client.post("/api/tasks", content=b'{"title": "\\ud800"}',
                           headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 422 and "Unicode" in response.json()["detail"]


def test_body_limit_and_token_before_body(client):
    response = client.post("/api/tasks", content=b"{oops", headers={"Content-Type": "application/json"})
    assert response.status_code == 401 and response.json() == {"detail": "missing bearer token"}
    big = b'{"title": "' + b"x" * MAX_BODY + b'"}'
    response = client.post("/api/tasks", content=big, headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 413
    full = {"title": "я" * 200, "context": "я" * 4000, "waiting_on": "я" * 200, "ref": "я" * 100,
            "next_check": "2026-11-03",
            "links": [{"title": "я" * 200, "url": "https://e.com/" + "a" * 1980} for _ in range(10)]}
    body = json.dumps(full).encode()
    assert len(body) < MAX_BODY
    response = client.post("/api/tasks", content=body, headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 201, response.text


# ---------- list ----------

def test_list_ordering_and_counts(client):
    t_late = create(client, title="check later", next_check="2026-11-10")
    t_none_old = create(client, title="no date, oldest")
    t_soon = create(client, title="check soon", status="waiting", waiting_on="supplier", next_check="2026-11-02")
    t_none_new = create(client, title="no date, newer", token=HAMILTON)
    t_same_day = create(client, title="same day as soon, newer", next_check="2026-11-02", token=HAMILTON)
    t_done_first = create(client, title="done first")
    t_dropped_second = create(client, title="dropped second", token=HAMILTON)
    t_done_third = create(client, title="done third")
    finish(client, t_done_first["id"])
    time.sleep(0.002)
    finish(client, t_dropped_second["id"], "drop", token=HAMILTON)
    time.sleep(0.002)
    finish(client, t_done_third["id"])

    def ids(query="", token=ALLEN):
        data = client.get(f"/api/tasks{query}", headers=auth(token)).json()
        return [t["id"] for t in data["tasks"]], data["counts"]

    open_ids, counts = ids()
    assert open_ids == [t_soon["id"], t_same_day["id"], t_late["id"], t_none_old["id"], t_none_new["id"]]
    assert counts == {"active": 4, "waiting": 1, "done": 2, "dropped": 1}
    finished = [t_done_third["id"], t_dropped_second["id"], t_done_first["id"]]
    assert ids("?status=finished")[0] == finished
    assert ids("?status=all", token=DENIS)[0] == open_ids + finished
    assert ids("?status=active")[0] == [t_same_day["id"], t_late["id"], t_none_old["id"], t_none_new["id"]]
    assert ids("?status=waiting")[0] == [t_soon["id"]]
    assert ids("?status=done")[0] == [t_done_third["id"], t_done_first["id"]]
    assert ids("?status=dropped")[0] == [t_dropped_second["id"]]
    assert ids("?status=all&limit=2")[0] == open_ids[:2]

    hamilton_ids, hamilton_counts = ids("?agent=hamilton&status=all")
    assert hamilton_ids == [t_same_day["id"], t_none_new["id"], t_dropped_second["id"]]
    assert hamilton_counts == {"active": 2, "waiting": 0, "done": 0, "dropped": 1}
    assert ids("?status=finished&limit=1")[1] == counts  # counts ignore status and limit
    assert ids("?agent=nobody")[1] == {"active": 0, "waiting": 0, "done": 0, "dropped": 0}


def test_default_limits(tmp_dir):
    db = Database(tmp_dir / "t.db")
    db.init()
    tasks, allen = TaskService(db), Identity("agent", "allen")
    for i in range(101):
        tasks.done(allen, tasks.create(allen, {"title": f"t{i}"})["id"])
    tasks.create(allen, {"title": "open"})
    assert len(tasks.list("finished")["tasks"]) == 100  # most recent 100 by default
    assert tasks.list("finished")["tasks"][0]["title"] == "t100"
    assert len(tasks.list("done")["tasks"]) == 100
    assert len(tasks.list("all")["tasks"]) == 102  # 200 otherwise
    assert len(tasks.list("finished", limit=101)["tasks"]) == 101


# ---------- replies on tasks (SPEC v5 "1. Replies on tasks") ----------

DENIS_ID, BOT = Identity("denis"), Identity("agent", "allen")


def _service(tmp_dir):
    db = Database(tmp_dir / "r.db")
    db.init()
    return TaskService(db)


def test_reply_then_answer_clears_pending(tmp_dir):
    tasks = _service(tmp_dir)
    task_id = tasks.create(BOT, {"title": "Chase the supplier", "status": "waiting"})["id"]
    replied = tasks.reply(DENIS_ID, task_id, {"text": "Status?", "preset": "status"})
    assert replied["reply_pending"] is True and replied["reply"]["text"] == "Status?"
    assert replied["reply"]["preset"] == "status" and replied["status"] == "waiting" and "events" not in replied
    answered = tasks.answer(BOT, task_id, {"text": "On it"})
    assert answered["reply_pending"] is False and answered["reply"]["text"] == "Status?"
    full = tasks.get(task_id)
    assert [e["kind"] for e in full["events"]][-2:] == ["replied", "answered"]
    assert full["events"][-2]["actor"] == "denis" and full["events"][-1]["payload"] == {"text": "On it"}
    assert (full["reply_pending"], full["reply"]["id"]) == (False, full["events"][-2]["id"])


def test_any_owner_action_clears_pending(tmp_dir):
    tasks = _service(tmp_dir)
    task_id = tasks.create(BOT, {"title": "Chase the supplier"})["id"]
    assert tasks.get(task_id)["reply"] is None and tasks.get(task_id)["reply_pending"] is False
    tasks.reply(DENIS_ID, task_id, {"text": "Nudge them again", "preset": "nudge"})
    assert tasks.update(BOT, task_id, {"next_check": "2026-12-01"})["reply_pending"] is False


def test_list_puts_pending_first_before_limit(tmp_dir):
    tasks = _service(tmp_dir)
    ids = [tasks.create(BOT, {"title": f"t{i}", "next_check": f"2026-11-0{i + 1}"})["id"] for i in range(3)]
    tasks.reply(DENIS_ID, ids[2], {"text": "Is this still needed?", "preset": "still-needed"})
    listed = tasks.list("open", limit=1)["tasks"]
    assert [t["id"] for t in listed] == [ids[2]] and listed[0]["reply_pending"] is True
    everything = tasks.list("all")["tasks"]
    assert [t["id"] for t in everything] == [ids[2], ids[0], ids[1]]
    assert [t["reply_pending"] for t in everything] == [True, False, False]
    tasks.answer(BOT, ids[2], {"text": "Yes, still needed"})
    assert [t["id"] for t in tasks.list("open")["tasks"]] == ids   # back to next_check order


def test_reply_on_finished_task_is_409(tmp_dir):
    from server.rules import Conflict, Forbidden
    tasks = _service(tmp_dir)
    task_id = tasks.create(BOT, {"title": "Chase the supplier"})["id"]
    with pytest.raises(Forbidden):
        tasks.reply(BOT, task_id, {"text": "hi"})
    with pytest.raises(Forbidden):
        tasks.answer(Identity("agent", "hamilton"), task_id, {"text": "hi"})
    tasks.done(BOT, task_id)
    with pytest.raises(Conflict):
        tasks.reply(DENIS_ID, task_id, {"text": "hi"})
    with pytest.raises(Conflict):
        tasks.answer(BOT, task_id, {"text": "hi"})
    assert [e["kind"] for e in tasks.get(task_id)["events"]] == ["created", "done"]


# ---------- schema migration ----------

def test_an_existing_database_gets_the_task_tables(settings, notifier):
    """A pre-v3 database (asks only) gets tasks and task_events on the next start; asks are untouched."""
    with TestClient(create_app(settings, notifier=notifier)) as c:
        ask = c.post("/api/asks", json={"title": "Old ask?"}, headers=auth(ALLEN)).json()
    with sqlite3.connect(settings.db_path) as conn:
        conn.executescript("DROP TABLE task_events; DROP TABLE tasks;")
        before = conn.execute("SELECT * FROM asks").fetchall()
    with TestClient(create_app(settings, notifier=notifier)) as c:
        assert create(c)["id"] == 1
        assert c.get(f"/api/asks/{ask['id']}", headers=auth(DENIS)).json()["title"] == "Old ask?"
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT * FROM asks").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone() == (1,)
