"""Replies on tasks (SPEC.md v5 "1. Replies on tasks"): the pure rules every store applies."""

import pytest

from server.rules import Conflict, Forbidden, Invalid
from server.task_rules import (
    PRESETS,
    REPLY_MAX,
    plan_task_answer,
    plan_task_reply,
    reply_state,
    task_order,
)
from server.tokens import Identity

DENIS = Identity("denis")
AGENT = Identity("agent", "bot")
OTHER_AGENT = Identity("agent", "other")
NOW = "2026-11-03T10:00:00.000Z"
OPEN_TASK = {
    "id": 5, "agent": "bot", "title": "Chase the supplier", "context": None, "status": "active",
    "waiting_on": None, "next_check": None, "links": [], "ref": None, "resolution": None,
    "created_at": "2026-11-01T00:00:00.000Z", "updated_at": "2026-11-01T00:00:00.000Z", "finished_at": None,
}


def test_reply_by_the_user_on_an_open_task():
    task, change = plan_task_reply(DENIS, {"text": " What's the status?\r\nany news ", "preset": "status"},
                                   lambda: OPEN_TASK, NOW)
    assert task is OPEN_TASK
    assert change.kind == "replied" and change.updates == {"updated_at": NOW} and change.at == NOW
    assert change.payload == {"text": "What's the status?\nany news", "preset": "status"}


def test_free_text_reply_has_no_preset_and_waiting_tasks_take_replies():
    _, change = plan_task_reply(DENIS, {"text": "Ping me tomorrow"}, lambda: {**OPEN_TASK, "status": "waiting"}, NOW)
    assert change.payload == {"text": "Ping me tomorrow", "preset": None}


def test_reply_rules():
    with pytest.raises(Forbidden):
        plan_task_reply(AGENT, {"text": "hi"}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Conflict):
        plan_task_reply(DENIS, {"text": "hi"}, lambda: {**OPEN_TASK, "status": "done"}, NOW)
    with pytest.raises(Invalid):
        plan_task_reply(DENIS, {"text": "hi", "preset": "later"}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Invalid):
        plan_task_reply(DENIS, {"text": "x" * (REPLY_MAX + 1)}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Invalid):
        plan_task_reply(DENIS, {"text": "   "}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Invalid):
        plan_task_reply(DENIS, {"text": "hi", "status": "done"}, lambda: OPEN_TASK, NOW)
    assert REPLY_MAX == 2000
    assert list(PRESETS) == ["status", "still-needed", "done", "not-needed", "nudge"]
    assert PRESETS == {
        "status": "What's the status?", "still-needed": "Is this still needed?",
        "done": "This is done, please close it", "not-needed": "Not needed, please drop it",
        "nudge": "Nudge them again",
    }


def test_answer_only_by_the_owner_on_an_open_task():
    _, change = plan_task_answer(AGENT, {"text": "Asked them again today"}, lambda: OPEN_TASK, NOW)
    assert (change.kind, change.payload, change.updates) == ("answered", {"text": "Asked them again today"},
                                                             {"updated_at": NOW})
    with pytest.raises(Forbidden):
        plan_task_answer(OTHER_AGENT, {"text": "x"}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Forbidden):
        plan_task_answer(DENIS, {"text": "x"}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Conflict):
        plan_task_answer(AGENT, {"text": "x"}, lambda: {**OPEN_TASK, "status": "dropped"}, NOW)
    with pytest.raises(Invalid):
        plan_task_answer(AGENT, {"text": ""}, lambda: OPEN_TASK, NOW)


def test_reply_state():
    created = {"id": 1, "actor": "bot", "kind": "created", "at": "a", "payload": {}}
    replied = {"id": 2, "actor": "denis", "kind": "replied", "at": "b", "payload": {"text": "Status?", "preset": "status"}}
    edited = {"id": 3, "actor": "bot", "kind": "edited", "at": "c", "payload": {}}
    assert reply_state([created]) == {"reply": None, "reply_pending": False}
    assert reply_state([created, replied]) == {
        "reply": {"id": 2, "at": "b", "text": "Status?", "preset": "status"}, "reply_pending": True}
    assert reply_state([created, replied, edited])["reply_pending"] is False
    assert reply_state([created, replied, {"id": 9, "kind": "weird"}])["reply_pending"] is True  # unknown kinds
    assert reply_state([created, replied, "junk", {"kind": "answered"}])["reply_pending"] is True  # no int id
    assert reply_state([])["reply"] is None
    answered = {"id": 4, "actor": "bot", "kind": "answered", "at": "d", "payload": {"text": "On it"}}
    again = {"id": 5, "actor": "denis", "kind": "replied", "at": "e", "payload": {"text": "Thanks", "preset": None}}
    state = reply_state([created, replied, answered, again])
    assert state == {"reply": {"id": 5, "at": "e", "text": "Thanks", "preset": None}, "reply_pending": True}
    for kind in ("answered", "edited", "status", "done", "dropped", "reopened"):
        assert reply_state([replied, {"id": 7, "kind": kind}])["reply_pending"] is False, kind


def test_pending_reply_sorts_first_among_open_tasks():
    a = {**OPEN_TASK, "id": 1, "next_check": "2026-01-01", "reply_pending": False}
    b = {**OPEN_TASK, "id": 2, "next_check": None, "reply_pending": True}
    done = {**OPEN_TASK, "id": 3, "status": "done", "finished_at": "2026-11-02", "reply_pending": True}
    assert [t["id"] for t in sorted([done, a, b], key=task_order)] == [2, 1, 3]
    # a task without the derived field sorts as before
    plain = {k: v for k, v in a.items() if k != "reply_pending"}
    assert sorted([plain, b], key=task_order)[0]["id"] == 2
