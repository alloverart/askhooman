"""Replies on tasks and task questions (SPEC.md v5 "1. Replies on tasks", "6. Task questions"): the pure rules
every store applies."""

import pytest

from server.rules import Conflict, Forbidden, Invalid
from server.task_rules import (
    PRESETS,
    QUESTION_MAX,
    REPLY_MAX,
    UNASK_NOTE_MAX,
    plan_task_answer,
    plan_task_ask,
    plan_task_reply,
    plan_task_unask,
    question_state,
    reply_state,
    task_order,
    task_state,
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


# ---------- task questions (SPEC v5 "6. Task questions") ----------

def _event(event_id, kind, **payload):
    return {"id": event_id, "actor": "denis" if kind == "replied" else "bot", "kind": kind, "at": f"t{event_id}",
            "payload": payload}


def test_ask_by_the_owner_on_an_open_task():
    task, change = plan_task_ask(AGENT, {"question": " Which fabric?\r\n", "options": [" Cotton ", "Linen"]},
                                 lambda: OPEN_TASK, NOW)
    assert task is OPEN_TASK
    assert (change.kind, change.updates, change.at) == ("asked", {"updated_at": NOW}, NOW)
    assert change.payload == {"text": "Which fabric?", "options": ["Cotton", "Linen"]}
    _, plain = plan_task_ask(AGENT, {"question": "Is the sample with you?"}, lambda: {**OPEN_TASK, "status": "waiting"},
                             NOW)
    assert plain.payload == {"text": "Is the sample with you?", "options": []}


def test_ask_rules():
    assert (QUESTION_MAX, UNASK_NOTE_MAX) == (500, 1000)
    with pytest.raises(Forbidden):
        plan_task_ask(DENIS, {"question": "x"}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Forbidden):
        plan_task_ask(OTHER_AGENT, {"question": "x"}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Conflict):
        plan_task_ask(AGENT, {"question": "x"}, lambda: {**OPEN_TASK, "status": "done"}, NOW)
    for bad in ({"question": "  "}, {}, {"question": "x" * (QUESTION_MAX + 1)}, {"question": "x", "text": "y"},
                {"question": "x", "options": ["a"] * 7}, {"question": "x", "options": ["o" * 81]},
                {"question": "x", "options": "yes"}):
        with pytest.raises(Invalid):
            plan_task_ask(AGENT, bad, lambda: OPEN_TASK, NOW)


def test_unask_takes_back_an_open_question_only():
    asked = _event(2, "asked", text="Which fabric?", options=[])
    with_question = {**OPEN_TASK, "events": [_event(1, "created"), asked]}
    task, change = plan_task_unask(AGENT, {"note": " Answered in chat: cotton "}, lambda: with_question, NOW)
    assert task is with_question
    assert (change.kind, change.updates, change.payload) == ("unasked", {"updated_at": NOW},
                                                             {"note": "Answered in chat: cotton"})
    _, bare = plan_task_unask(AGENT, {}, lambda: with_question, NOW)
    assert bare.payload == {"note": None}
    with pytest.raises(Conflict, match="no open question"):
        plan_task_unask(AGENT, {}, lambda: {**OPEN_TASK, "events": [_event(1, "created")]}, NOW)
    with pytest.raises(Conflict, match="no open question"):  # already answered
        plan_task_unask(AGENT, {}, lambda: {**with_question, "events": [asked, _event(3, "replied", text="Cotton")]},
                        NOW)
    with pytest.raises(Conflict, match="no open question"):  # no history at hand counts as no question
        plan_task_unask(AGENT, {}, lambda: OPEN_TASK, NOW)
    with pytest.raises(Forbidden):
        plan_task_unask(DENIS, {}, lambda: with_question, NOW)
    with pytest.raises(Forbidden):
        plan_task_unask(OTHER_AGENT, {}, lambda: with_question, NOW)
    with pytest.raises(Invalid):
        plan_task_unask(AGENT, {"note": "x" * (UNASK_NOTE_MAX + 1)}, lambda: with_question, NOW)
    with pytest.raises(Invalid):
        plan_task_unask(AGENT, {"text": "x"}, lambda: with_question, NOW)


def test_question_state():
    created, asked = _event(1, "created"), _event(2, "asked", text="Which fabric?", options=["Cotton", "Linen"])
    question = {"id": 2, "at": "t2", "text": "Which fabric?", "options": ["Cotton", "Linen"]}
    assert question_state([created]) == {"question": None, "question_open": False}
    assert question_state([]) == {"question": None, "question_open": False}
    assert question_state([created, asked]) == {"question": question, "question_open": True}
    # the owner working on the task leaves the question open
    for kind in ("edited", "status", "answered"):
        assert question_state([asked, _event(3, kind)])["question_open"] is True, kind
    # the user's reply, taking it back and finishing the task close it; a reopened task has no question again
    for kind in ("replied", "unasked", "done", "dropped"):
        assert question_state([asked, _event(3, kind)]) == {"question": question, "question_open": False}, kind
    assert question_state([asked, _event(3, "done"), _event(4, "reopened")])["question_open"] is False
    # a new question replaces the old one, also after an answer
    again = _event(4, "asked", text="And the colour?", options=[])
    state = question_state([asked, _event(3, "replied", text="Cotton"), again])
    assert state == {"question": {"id": 4, "at": "t4", "text": "And the colour?", "options": []},
                     "question_open": True}
    assert question_state([asked, again])["question"]["id"] == 4
    # junk in a file never breaks a listing
    assert question_state([asked, "junk", {"kind": "replied"}, {"id": True, "kind": "replied"}])["question_open"] is True
    broken = question_state([{"id": 2, "kind": "asked", "payload": "x"}])
    assert broken == {"question": {"id": 2, "at": None, "text": None, "options": []}, "question_open": True}
    assert question_state([{"id": 2, "kind": "asked", "payload": {"text": "Q", "options": ["a", 5, None]}}])[
        "question"]["options"] == ["a"]


def test_a_question_is_the_owners_action_on_a_reply():
    replied = _event(2, "replied", text="Status?", preset="status")
    for kind in ("asked", "unasked"):
        assert reply_state([replied, _event(3, kind)])["reply_pending"] is False, kind
    state = task_state([replied, _event(3, "asked", text="Which fabric?", options=[])])
    assert (state["reply_pending"], state["question_open"], state["reply"]["text"]) == (False, True, "Status?")
    assert set(task_state([])) == {"reply", "reply_pending", "question", "question_open"}
