"""Task rules shared by every store (SPEC.md "Tasks … (v3)"): validation, permissions and the state machine.

Pure functions like rules.py: TaskService (SQLite, tasks.py) and the hosted bridge (Drive files) both apply them.
Statuses: active <-> waiting (update), active|waiting -> done|dropped, done|dropped -> active (reopen).
Only the owner agent writes its tasks; the human's token reads everything and writes nothing (403).
"""

import re
from datetime import date

from .rules import (
    CONTEXT_MAX,
    REF_MAX,
    RESOLUTION_MAX,
    TITLE_MAX,
    Change,
    Conflict,
    Forbidden,
    Invalid,
    Loader,
    _Desc,
    _links,
    _text,
)
from .tokens import Identity

WAITING_ON_MAX = 200
OPEN = ("active", "waiting")
FINISHED = ("done", "dropped")
EDITABLE = ("title", "context", "status", "waiting_on", "next_check", "links", "ref")
STATUS_FILTERS = {
    "open": OPEN,
    "active": ("active",),
    "waiting": ("waiting",),
    "done": ("done",),
    "dropped": ("dropped",),
    "finished": FINISHED,
    "all": OPEN + FINISHED,
}
LIMIT_DEFAULT = 200
LIMIT_FINISHED = 100  # default when only finished tasks are listed
# Replies on tasks (SPEC v5 "1. Replies on tasks"): the user replies on an open task, the owner answers.
REPLY_MAX = 2000
PRESETS = {  # ids and texts are part of the contract: agents match them, the apps show them as buttons
    "status": "What's the status?",
    "still-needed": "Is this still needed?",
    "done": "This is done, please close it",
    "not-needed": "Not needed, please drop it",
    "nudge": "Nudge them again",
}
# The owner's own events: any of them after the user's reply means the agent has acted on it.
OWNER_KINDS = ("answered", "edited", "status", "done", "dropped", "reopened")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


# ---------- validation ----------

def _status(value) -> str:
    if value is None:
        return "active"
    if value not in OPEN:
        raise Invalid("status must be 'active' or 'waiting' (finish a task with done or drop)")
    return value


def _date(value) -> str | None:
    text = _text(value, "next_check", 100)
    if text is None:
        return None
    try:
        if not DATE.match(text):
            raise ValueError
        date.fromisoformat(text)
    except ValueError:
        raise Invalid(f"next_check must be a date YYYY-MM-DD, got {text!r}") from None
    return text


def clean_task_fields(data: dict, partial: bool = False) -> dict:
    """Validate and normalise editable fields. With partial=True only the keys present are checked."""
    unknown = set(data) - set(EDITABLE)
    if unknown:
        raise Invalid(f"unknown fields: {', '.join(sorted(unknown))}")
    checks = {
        "title": lambda v: _text(v, "title", TITLE_MAX, required=True),
        "context": lambda v: _text(v, "context", CONTEXT_MAX),
        "status": _status,
        "waiting_on": lambda v: _text(v, "waiting_on", WAITING_ON_MAX),
        "next_check": _date,
        "links": _links,
        "ref": lambda v: _text(v, "ref", REF_MAX),
    }
    return {
        field: check(data.get(field))
        for field, check in checks.items()
        if not partial or field in data
    }


def default_limit(status: str, limit: int | None) -> int | None:
    if limit is None and status in STATUS_FILTERS:
        return LIMIT_FINISHED if set(STATUS_FILTERS[status]) <= set(FINISHED) else LIMIT_DEFAULT
    return limit


# ---------- transitions ----------

def plan_task_create(who: Identity, data: dict) -> dict:
    """The cleaned fields of a new task (also the payload of its `created` event)."""
    if who.is_denis:
        raise Forbidden("tasks are the agents' own work: the human's token can read them but not create them")
    return clean_task_fields(data)


def new_task_record(who: Identity, task_id: int, fields: dict, now: str) -> dict:
    """A fresh task as REST returns it."""
    return {
        "id": task_id, "agent": who.agent, "title": fields["title"], "context": fields["context"],
        "status": fields["status"], "waiting_on": fields["waiting_on"], "next_check": fields["next_check"],
        "links": fields["links"], "ref": fields["ref"], "resolution": None, "created_at": now, "updated_at": now,
        "finished_at": None,
    }


def plan_task_update(who: Identity, data: dict, load: Loader, now: str) -> tuple[dict, Change | None]:
    """Edit fields of an open task; active <-> waiting goes through here too. One event per call:
    kind "status" if the status changed, else "edited"; payload {"changes": {field: {from, to}}}."""
    fields = clean_task_fields(data, partial=True)
    task = load()
    check_owner(who, task, "edit")
    if task["status"] not in OPEN:
        raise Conflict(f"task #{task['id']} is {task['status']}; reopen it before editing")
    changes = {f: {"from": task[f], "to": v} for f, v in fields.items() if task[f] != v}
    if not changes:
        return task, None
    updates = {f: v["to"] for f, v in changes.items()}
    updates["updated_at"] = now
    kind = "status" if "status" in changes else "edited"
    return task, Change(updates, kind, {"changes": changes}, now)


def plan_finish(who: Identity, status: str, resolution, load: Loader, now: str) -> tuple[dict, Change]:
    """done or dropped, with an optional resolution."""
    resolution = _text(resolution, "resolution", RESOLUTION_MAX)
    verb = "finish" if status == "done" else "drop"
    task = load()
    check_owner(who, task, verb)
    if task["status"] not in OPEN:
        raise Conflict(f"task #{task['id']} is already {task['status']}")
    updates = {"status": status, "resolution": resolution, "finished_at": now, "updated_at": now}
    return task, Change(updates, status, {"resolution": resolution, "previous_status": task["status"]}, now)


def plan_task_reopen(who: Identity, load: Loader, now: str) -> tuple[dict, Change]:
    """done|dropped -> active; clears resolution and finished_at (both stay in the event)."""
    task = load()
    check_owner(who, task, "reopen")
    if task["status"] not in FINISHED:
        raise Conflict(f"task #{task['id']} is {task['status']}, not finished; nothing to reopen")
    updates = {"status": "active", "resolution": None, "finished_at": None, "updated_at": now}
    payload = {"previous_status": task["status"], "resolution": task["resolution"], "finished_at": task["finished_at"]}
    return task, Change(updates, "reopened", payload, now)


def plan_task_reply(who: Identity, data: dict, load: Loader, now: str) -> tuple[dict, Change]:
    """The user replies on an open task: free text, or a preset (its text, a note on the next line). A reply
    never changes the status; only the user's token replies."""
    text, preset = _reply_fields(data, ("text", "preset"))
    if preset is not None and preset not in PRESETS:
        raise Invalid(f"unknown preset {preset!r}; one of: {', '.join(PRESETS)}")
    task = load()
    if not who.is_denis:
        raise Forbidden("only the user replies on tasks; the owner answers a reply with task_answer")
    _check_open(task, "replies go to open tasks")
    return task, Change({"updated_at": now}, "replied", {"text": text, "preset": preset}, now)


def plan_task_answer(who: Identity, data: dict, load: Loader, now: str) -> tuple[dict, Change]:
    """The owner answers the user's reply on an open task."""
    text, _ = _reply_fields(data, ("text",))
    task = load()
    check_owner(who, task, "answer")
    _check_open(task, "answers go to open tasks")
    return task, Change({"updated_at": now}, "answered", {"text": text}, now)


def _reply_fields(data: dict, allowed: tuple[str, ...]) -> tuple[str, str | None]:
    unknown = set(data) - set(allowed)
    if unknown:
        raise Invalid(f"unknown fields: {', '.join(sorted(unknown))}")
    text = _text(data.get("text"), "text", REPLY_MAX, required=True)
    preset = data.get("preset")
    if preset is not None and not isinstance(preset, str):
        raise Invalid("preset must be a string or null")
    return text, preset


def _check_open(task: dict, why: str) -> None:
    if task["status"] not in OPEN:
        raise Conflict(f"task #{task['id']} is {task['status']}; {why}")


def reply_state(events: list) -> dict:
    """`reply` = the user's latest reply {id, at, text, preset} or None; `reply_pending` while it is newer than
    every event of the owner. Derived from the events, never stored. Non-dict events and events without an int
    id don't count."""
    last_reply, last_owner = None, 0
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("id"), int) or isinstance(event.get("id"), bool):
            continue
        if event.get("kind") == "replied":
            if last_reply is None or event["id"] > last_reply["id"]:
                last_reply = event
        elif event.get("kind") in OWNER_KINDS:
            last_owner = max(last_owner, event["id"])
    if last_reply is None:
        return {"reply": None, "reply_pending": False}
    payload = last_reply.get("payload") if isinstance(last_reply.get("payload"), dict) else {}
    reply = {"id": last_reply["id"], "at": last_reply.get("at"), "text": payload.get("text"),
             "preset": payload.get("preset")}
    return {"reply": reply, "reply_pending": last_reply["id"] > last_owner}


def check_owner(who: Identity, task: dict, action: str) -> None:
    if who.is_denis:
        raise Forbidden(f"the human's token can read tasks but not {action} them; "
                        f"only the agent who owns task #{task['id']} ({task['agent']}) can")
    if task["agent"] != who.agent:
        raise Forbidden(f"only the agent who owns task #{task['id']} ({task['agent']}) can {action} it")


# ---------- listing (stores without SQL; must match tasks.TaskService.list) ----------

def task_order(task: dict) -> tuple:
    """open ones with a pending reply first, then by next_check (none last), then the longest-running;
    finished ones latest first."""
    if task["status"] in OPEN:
        next_check = task.get("next_check")
        return (0, not task.get("reply_pending"), next_check is None, next_check or "",
                task.get("created_at") or "", task["id"])
    return (1, False, False, _Desc(task.get("finished_at") or ""), _Desc(""), _Desc(""), _Desc(task["id"]))
