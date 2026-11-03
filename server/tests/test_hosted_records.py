"""Hosted bridge: asks and tasks in each user's Drive, through the same rules as the single-tenant bridge."""

import json
import random

from server.db import Database, ask_from_row, task_from_row, to_json
from server.drive import DriveClient
from server.mirror import ASK, app_properties, build_doc
from server.rules import ask_order
from server.task_rules import task_order

from .fake_drive import app_action, app_task_reply
from .hosted_support import (
    DENIS_SUB,
    OLGA_EMAIL,
    OLGA_SUB,
    connect,
    mcp,
    register,
    tool_error,
    tool_result,
)


def agent_token(hosted, google, name="Allen", sub=DENIS_SUB, email="owner@example.com") -> str:
    client = register(hosted)
    return connect(hosted, google, client["client_id"], agent=name, sub=sub, email=email)["access_token"]


def test_two_users_are_isolated(hosted, google):
    denis = agent_token(hosted, google, "Allen")
    olga = agent_token(hosted, google, "Allen", sub=OLGA_SUB, email=OLGA_EMAIL)  # same agent name, other user
    a = tool_result(mcp(hosted, denis, "ask_create", {"title": "Denis's question?"}))
    b = tool_result(mcp(hosted, olga, "ask_create", {"title": "Olga's question?"}))
    assert (a["id"], b["id"]) == (1, 1)  # ids are per user
    assert google.drive(DENIS_SUB).doc(1)["title"] == "Denis's question?"
    assert google.drive(OLGA_SUB).doc(1)["title"] == "Olga's question?"
    everyone = tool_result(mcp(hosted, denis, "asks_list", {"status": "all"}))
    assert [x["title"] for x in everyone["asks"]] == ["Denis's question?"]
    tool_result(mcp(hosted, olga, "ask_close", {"id": 1}))
    assert google.drive(DENIS_SUB).doc(1)["status"] == "open"


def test_state_machine_on_drive_files_with_the_app_as_second_writer(hosted, google):
    token = agent_token(hosted, google)
    drive = google.drive(DENIS_SUB)
    ask = tool_result(mcp(hosted, token, "ask_create", {"title": "Ship it?", "context": "v2"}))
    # the app (Denis) answers by writing the file directly, adding a field the bridge doesn't know
    app_action(drive, ask["id"], "answered", answer="Yes", extra={"app_note": "keep me"})
    mine = tool_result(mcp(hosted, token, "asks_mine"))
    assert mine["asks"][0]["answer"] == "Yes" and mine["counts"]["answered"] == 1
    assert "conflict" in tool_error(mcp(hosted, token, "ask_update", {"id": 1, "title": "Edited?"}))
    closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "Shipped"}))
    assert (closed["status"], closed["closed_by"], closed["resolution"]) == ("closed", "allen", "Shipped")
    doc = drive.doc(1)
    assert doc["app_note"] == "keep me" and doc["writer"] == "bridge" and doc["rev"] == 3
    assert [(e["id"], e["kind"], e["actor"]) for e in doc["events"]] == [
        (1, "created", "allen"), (2, "answered", "denis"), (3, "closed", "allen")]
    assert drive.file("ask-1.json")["appProperties"] == {"ask_id": "1", "status": "closed", "agent": "allen",
                                                          "rev": "3"}
    history = tool_result(mcp(hosted, token, "ask_get", {"id": 1}))
    assert [e["kind"] for e in history["events"]] == ["created", "answered", "closed"]
    auth = {"Authorization": f"Bearer {token}"}
    reopened = hosted.post("/api/asks/1/reopen", headers=auth)  # the owner may reopen (REST)
    assert reopened.status_code == 200 and (reopened.json()["status"], reopened.json()["closed_by"]) == ("open", None)
    assert hosted.post("/api/asks/1/reopen", headers=auth).status_code == 409
    # validation errors come before any Drive access and match the single-tenant bridge
    assert "title" in tool_error(mcp(hosted, token, "ask_create", {"title": "x" * 201}))
    assert "not found" in tool_error(mcp(hosted, token, "ask_get", {"id": 99}))
    assert "not found" in tool_error(mcp(hosted, token, "ask_get", {"id": 10**20}))


def test_other_agent_cannot_touch_and_tasks_follow_the_rules(hosted, google):
    allen = agent_token(hosted, google, "Allen")
    feynman = agent_token(hosted, google, "Feynman")
    ask = tool_result(mcp(hosted, allen, "ask_create", {"title": "Mine?"}))
    assert "forbidden" in tool_error(mcp(hosted, feynman, "ask_close", {"id": ask["id"]}))
    task = tool_result(mcp(hosted, allen, "task_create", {"title": "Get the supplier to confirm the fabric", "status": "waiting",
                                                           "waiting_on": "supplier", "next_check": "2026-11-03"}))
    assert (task["id"], task["status"]) == (1, "waiting")
    assert "forbidden" in tool_error(mcp(hosted, feynman, "task_done", {"id": 1}))
    moved = tool_result(mcp(hosted, allen, "task_update", {"id": 1, "status": "active"}))
    assert moved["status"] == "active" and moved["waiting_on"] == "supplier"
    done = tool_result(mcp(hosted, allen, "task_done", {"id": 1, "resolution": "Accepted"}))
    assert done["status"] == "done" and done["finished_at"]
    assert "conflict" in tool_error(mcp(hosted, allen, "task_drop", {"id": 1}))
    reopened = tool_result(mcp(hosted, allen, "task_reopen", {"id": 1}))
    assert (reopened["status"], reopened["resolution"]) == ("active", None)
    doc = google.drive(DENIS_SUB).task_doc(1)
    assert doc["kind"] == "task" and [e["kind"] for e in doc["events"]] == ["created", "status", "done", "reopened"]
    assert all(e["task_id"] == 1 for e in doc["events"])
    tools = {t["name"] for t in hosted.post("/mcp", headers={
        "Authorization": f"Bearer {allen}", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()["result"]["tools"]}
    assert "ask_answer" not in tools and {"asks_mine", "tasks_mine", "ask_create", "task_create"} <= tools


def test_rest_for_agents(hosted, google):
    token = agent_token(hosted, google)
    auth = {"Authorization": f"Bearer {token}"}
    created = hosted.post("/api/asks", json={"title": "Via REST?"}, headers=auth)
    assert created.status_code == 201 and created.json()["agent"] == "allen"
    assert hosted.post("/api/asks", json={"title": "x", "agent": "other"}, headers=auth).status_code == 422
    answer = hosted.post(f"/api/asks/{created.json()['id']}/answer", json={"answer": "x"}, headers=auth)
    assert answer.status_code == 403
    listed = hosted.get("/api/asks", headers=auth).json()
    assert [a["title"] for a in listed["asks"]] == ["Via REST?"]
    assert hosted.get("/api/tasks?status=bogus", headers=auth).status_code == 422


def test_new_ids_start_after_the_highest_in_drive_and_skip_collisions(hosted, google):
    drive = google.drive(DENIS_SUB)
    old = {"id": 7, "agent": "allen", "title": "From a single-tenant bridge", "context": None, "options": [],
           "links": [], "priority": "normal", "ref": None, "status": "open", "answer": None, "answered_at": None,
           "resolution": None, "closed_by": None, "closed_at": None, "created_at": "2026-10-31T10:00:00.000Z",
           "updated_at": "2026-10-31T10:00:00.000Z", "events": []}
    drive.add("ask-7.json", build_doc(old, 4), app_properties(old, 4, ASK))
    token = agent_token(hosted, google)
    assert tool_result(mcp(hosted, token, "ask_create", {"title": "Next?"}))["id"] == 8
    # another writer creates ask-9 behind our back: the counter moves past it instead of overwriting
    drive.add("ask-9.json", build_doc({**old, "id": 9}, 1), app_properties({**old, "id": 9}, 1, ASK))
    assert tool_result(mcp(hosted, token, "ask_create", {"title": "After?"}))["id"] == 10
    assert drive.doc(9)["title"] == "From a single-tenant bridge"
    assert tool_result(mcp(hosted, token, "task_create", {"title": "First task"}))["id"] == 1
    listed = tool_result(mcp(hosted, token, "asks_list", {"status": "all"}))
    assert [a["id"] for a in listed["asks"]] == [7, 9, 8, 10]  # open: oldest first (7 and 9 are from 10:00)


def test_concurrent_app_write_is_not_lost(hosted, google, monkeypatch):
    token = agent_token(hosted, google)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Race?"}))
    drive = google.drive(DENIS_SUB)
    original = DriveClient.get_meta
    raced = []

    def get_meta(self, file_id):
        if not raced:  # the app answers right between the bridge's read and its write
            raced.append(True)
            app_action(drive, 1, "answered", answer="From the app")
        return original(self, file_id)

    monkeypatch.setattr(DriveClient, "get_meta", get_meta)
    closed = tool_result(mcp(hosted, token, "ask_close", {"id": 1, "resolution": "done"}))
    assert closed["status"] == "closed"
    doc = drive.doc(1)
    assert [e["kind"] for e in doc["events"]] == ["created", "answered", "closed"]  # the answer survived
    assert doc["answer"] == "From the app" and doc["events"][-1]["payload"]["previous_status"] == "answered"


def test_drive_not_linked_or_revoked_is_a_clear_error(hosted, google):
    token = agent_token(hosted, google)
    for refresh in list(google.refresh_tokens):
        google.revoked_refresh.add(refresh)
    hosted.app.state.pool._clients.clear()  # forget the cached Google access token
    text = tool_error(mcp(hosted, token, "asks_mine"))
    assert "unavailable: AskHooman can't reach" in text and "/oauth/link" in text
    assert not hosted.store.get("users", DENIS_SUB)["google_refresh"]  # dead link forgotten -> app can relink
    rest = hosted.get("/api/asks", headers={"Authorization": f"Bearer {token}"})
    assert rest.status_code == 503 and "/oauth/link" in rest.json()["detail"]


def test_python_order_matches_sqlite_order(tmp_dir):
    """Stores without SQL sort with rules.ask_order / task_order: same order as the SQLite queries."""
    db = Database(tmp_dir / "order.db")
    db.init()
    rnd = random.Random(7)
    stamps = [f"2026-10-{d:02d}T{h:02d}:00:00.000Z" for d in (1, 2, 3) for h in (1, 2)]
    with db.transaction(write=True) as conn:
        for _ in range(60):
            status = rnd.choice(["open", "answered", "closed"])
            conn.execute(
                """INSERT INTO asks (agent, title, priority, status, answered_at, closed_at, created_at, updated_at)
                   VALUES ('a', 't', ?, ?, ?, ?, ?, ?)""",
                (rnd.choice(["normal", "urgent"]), status, rnd.choice(stamps) if status == "answered" else None,
                 rnd.choice(stamps) if status == "closed" else None, rnd.choice(stamps), "x"))
            tstatus = rnd.choice(["active", "waiting", "done", "dropped"])
            conn.execute(
                """INSERT INTO tasks (agent, title, status, next_check, links, created_at, updated_at, finished_at)
                   VALUES ('a', 't', ?, ?, ?, ?, 'x', ?)""",
                (tstatus, rnd.choice([None, "2026-11-01", "2026-11-02"]), to_json([]), rnd.choice(stamps),
                 rnd.choice(stamps) if tstatus in ("done", "dropped") else None))
    from server.core import AskService
    from server.tasks import TaskService
    sql_asks = AskService(db).list("all", None, 1000)["asks"]
    sql_tasks = TaskService(db).list("all", None, 1000)["tasks"]
    with db.transaction() as conn:
        asks = [ask_from_row(r) for r in conn.execute("SELECT * FROM asks")]
        tasks = [task_from_row(r) for r in conn.execute("SELECT * FROM tasks")]
    rnd.shuffle(asks)
    rnd.shuffle(tasks)
    assert [a["id"] for a in sorted(asks, key=ask_order)] == [a["id"] for a in sql_asks]
    assert [t["id"] for t in sorted(tasks, key=task_order)] == [t["id"] for t in sql_tasks]
    assert json.dumps(sql_asks)  # plain JSON


def test_task_replies_from_the_app_and_task_answer(hosted, google):
    """SPEC v5: the app writes the user's `replied` event into task-<id>.json; the hosted bridge derives
    reply / reply_pending from the file (never stored), lists pending first, and the owner answers."""
    allen = agent_token(hosted, google, "Allen")
    feynman = agent_token(hosted, google, "Feynman")
    drive = google.drive(DENIS_SUB)
    first = tool_result(mcp(hosted, allen, "task_create", {"title": "Chase the supplier", "next_check": "2026-11-04"}))
    second = tool_result(mcp(hosted, allen, "task_create", {"title": "Renew the domain", "next_check": "2026-12-01"}))
    assert first["reply"] is None and first["reply_pending"] is False
    app_task_reply(drive, second["id"], "What's the status?\nany news", "status")
    got = tool_result(mcp(hosted, allen, "task_get", {"id": second["id"]}))
    assert got["reply_pending"] is True and got["reply"]["text"] == "What's the status?\nany news"
    assert got["reply"]["preset"] == "status" and got["events"][-1]["kind"] == "replied"
    mine = tool_result(mcp(hosted, allen, "tasks_mine"))["tasks"]
    assert [t["id"] for t in mine] == [second["id"], first["id"]] and mine[0]["reply_pending"] is True
    assert "forbidden" in tool_error(mcp(hosted, feynman, "task_answer", {"id": second["id"], "text": "not mine"}))
    answered = tool_result(mcp(hosted, allen, "task_answer", {"id": second["id"], "text": "Asked them again"}))
    assert answered["reply_pending"] is False and answered["reply"]["text"].startswith("What's the status?")
    doc = drive.task_doc(second["id"])
    assert "reply" not in doc and "reply_pending" not in doc
    assert [(e["kind"], e["actor"]) for e in doc["events"]] == [
        ("created", "allen"), ("replied", "denis"), ("answered", "allen")]
    assert [t["id"] for t in tool_result(mcp(hosted, allen, "tasks_mine"))["tasks"]] == [first["id"], second["id"]]
    # an update that changes nothing still returns the derived fields, as the single-tenant bridge does
    app_task_reply(drive, first["id"], "Nudge them again", "nudge")
    same = tool_result(mcp(hosted, allen, "task_update", {"id": first["id"], "title": first["title"]}))
    assert same["reply_pending"] is True and same["reply"]["preset"] == "nudge"
    # an agent can't reply (REST); finished tasks take no answer
    auth = {"Authorization": f"Bearer {allen}"}
    assert hosted.post(f"/api/tasks/{first['id']}/reply", json={"text": "hi"}, headers=auth).status_code == 403
    tool_result(mcp(hosted, allen, "task_done", {"id": first["id"]}))
    assert "conflict" in tool_error(mcp(hosted, allen, "task_answer", {"id": first["id"], "text": "late"}))
