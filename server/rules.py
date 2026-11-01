"""Ask rules shared by every store: validation, permissions and the state machine as pure functions.

SPEC.md "Data model" and "State machine". Nothing here touches storage: AskService (SQLite, core.py) and the
hosted bridge (the user's Drive files, hosted/records.py) both apply these functions, so the two can't drift.

A transition takes the caller, the raw input and `load`, a callable that returns the current ask (raising
NotFound). It checks things in a fixed order (input first, then existence, then permission, then state), so
both stores report the same error for the same request, and returns a Change (fields to set + the event to
append), or None when nothing changes.
"""

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from .tokens import Identity

TITLE_MAX = 200
CONTEXT_MAX = 4000
OPTIONS_MAX = 6
OPTION_MAX = 80
LINKS_MAX = 10
LINK_TITLE_MAX = 200
URL_MAX = 2000
REF_MAX = 100
ANSWER_MAX = 4000
RESOLUTION_MAX = 1000
LIMIT_MAX = 10000
ID_MAX = 2**63 - 1  # SQLite INTEGER range; larger ids can't exist (and would overflow in sqlite3)

# C0/C1 control characters except \t and \n. Agent text is printed in terminals (CLI), where
# escape sequences would be executed, so they are dropped on the way in.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

PRIORITIES = ("normal", "urgent")
EDITABLE = ("title", "context", "options", "links", "priority", "ref")
STATUS_FILTERS = {
    "active": ("open", "answered"),
    "open": ("open",),
    "answered": ("answered",),
    "closed": ("closed",),
    "all": ("open", "answered", "closed"),
}
# Written into ask files that the app writes too (SPEC "Drive storage"), so the text is part of the file format.
DENIS_DISMISS_RESOLUTION = "Not relevant (Denis)"
CLEARED_BY_REOPEN = ("answer", "answered_at", "resolution", "closed_by", "closed_at")


class AskError(Exception):
    status_code = 400
    label = "error"  # prefix for MCP tool errors


class NotFound(AskError):
    status_code = 404
    label = "not found"


class Forbidden(AskError):
    status_code = 403
    label = "forbidden"


class Conflict(AskError):
    status_code = 409
    label = "conflict"


class Invalid(AskError):
    status_code = 422
    label = "invalid"


class Unavailable(AskError):
    """The store can't be reached right now (hosted mode: the user's Google Drive)."""
    status_code = 503
    label = "unavailable"


@dataclass(frozen=True)
class Change:
    """What a transition does: set `updates` on the record (updated_at included) and append one event."""
    updates: dict
    kind: str
    payload: dict
    at: str


Loader = Callable[[], dict]


# ---------- validation ----------

def _text(value, field: str, max_len: int, required: bool = False) -> str | None:
    """Strip; empty means absent (None). Line breaks and tabs inside are kept, other control
    characters are dropped."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Invalid(f"{field} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # e.g. a lone surrogate from JSON "\ud800"
        raise Invalid(f"{field} contains characters that are not valid Unicode") from None
    value = CONTROL_CHARS.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not value:
        if required:
            raise Invalid(f"{field} is required and must not be empty")
        return None
    if len(value) > max_len:
        raise Invalid(f"{field} is too long: {len(value)} chars, max {max_len}")
    return value


def _options(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise Invalid("options must be a list of strings")
    if len(value) > OPTIONS_MAX:
        raise Invalid(f"options: at most {OPTIONS_MAX} items, got {len(value)}")
    return [_text(item, f"options[{i}]", OPTION_MAX, required=True) for i, item in enumerate(value)]


def _links(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise Invalid("links must be a list of {title, url} objects")
    if len(value) > LINKS_MAX:
        raise Invalid(f"links: at most {LINKS_MAX} items, got {len(value)}")
    links = []
    for i, item in enumerate(value):
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            raise Invalid(f"links[{i}] must be an object with title and url")
        url = _text(item.get("url"), f"links[{i}].url", URL_MAX, required=True)
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise Invalid(f"links[{i}].url must be an http(s) URL")
        title = _text(item.get("title"), f"links[{i}].title", LINK_TITLE_MAX) or url
        links.append({"title": title, "url": url})
    return links


def _priority(value) -> str:
    if value is None:
        return "normal"
    if value not in PRIORITIES:
        raise Invalid("priority must be 'normal' or 'urgent'")
    return value


def clean_fields(data: dict, partial: bool = False) -> dict:
    """Validate and normalise editable fields. With partial=True only the keys present are checked."""
    unknown = set(data) - set(EDITABLE)
    if unknown:
        raise Invalid(f"unknown fields: {', '.join(sorted(unknown))}")
    checks = {
        "title": lambda v: _text(v, "title", TITLE_MAX, required=True),
        "context": lambda v: _text(v, "context", CONTEXT_MAX),
        "options": _options,
        "links": _links,
        "priority": _priority,
        "ref": lambda v: _text(v, "ref", REF_MAX),
    }
    return {
        field: check(data.get(field))
        for field, check in checks.items()
        if not partial or field in data
    }


def valid_id(item_id) -> bool:
    return not isinstance(item_id, bool) and isinstance(item_id, int) and 1 <= item_id <= ID_MAX


def check_list_args(filters: dict, status: str, agent, limit) -> tuple[tuple[str, ...], str | None]:
    """The statuses a `status` filter means and the cleaned agent filter; 422 for a bad status/limit."""
    if status not in filters:
        raise Invalid(f"status must be one of: {', '.join(filters)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIMIT_MAX:
        raise Invalid(f"limit must be an integer from 1 to {LIMIT_MAX}")
    return filters[status], _text(agent, "agent", 100)


# ---------- transitions ----------

def plan_create(who: Identity, data: dict) -> dict:
    """The cleaned fields of a new ask (also the payload of its `created` event)."""
    if who.is_denis:
        raise Forbidden("only agents create asks; the human answers them")
    return clean_fields(data)


def new_record(who: Identity, ask_id: int, fields: dict, now: str) -> dict:
    """A fresh open ask as REST returns it."""
    return {
        "id": ask_id, "agent": who.agent, "title": fields["title"], "context": fields["context"],
        "options": fields["options"], "links": fields["links"], "priority": fields["priority"], "ref": fields["ref"],
        "status": "open", "answer": None, "answered_at": None, "resolution": None, "closed_by": None,
        "closed_at": None, "created_at": now, "updated_at": now,
    }


def plan_update(who: Identity, data: dict, load: Loader, now: str) -> tuple[dict, Change | None]:
    fields = clean_fields(data, partial=True)
    ask = load()
    if who.is_denis or ask["agent"] != who.agent:
        raise Forbidden(f"only the agent who created ask #{ask['id']} ({ask['agent']}) can edit it")
    if ask["status"] != "open":
        raise Conflict(f"ask #{ask['id']} is {ask['status']}; only open asks can be edited")
    changes = {f: {"from": ask[f], "to": v} for f, v in fields.items() if ask[f] != v}
    if not changes:
        return ask, None
    updates = {f: v["to"] for f, v in changes.items()}
    updates["updated_at"] = now
    return ask, Change(updates, "edited", {"changes": changes}, now)


def plan_answer(who: Identity, answer, load: Loader, now: str) -> tuple[dict, Change | None]:
    if not who.is_denis:
        raise Forbidden("only the human can answer asks")
    answer = _text(answer, "answer", ANSWER_MAX, required=True)
    ask = load()
    if ask["status"] not in ("open", "answered"):
        raise Conflict(f"ask #{ask['id']} is closed; reopen it before answering")
    if ask["status"] == "answered" and ask["answer"] == answer:
        return ask, None  # same answer again: nothing changes, no event, no second ping
    updates = {"status": "answered", "answer": answer, "answered_at": now, "updated_at": now}
    return ask, Change(updates, "answered", {"answer": answer, "previous_answer": ask["answer"]}, now)


def plan_close(who: Identity, resolution, load: Loader, now: str) -> tuple[dict, Change]:
    resolution = _text(resolution, "resolution", RESOLUTION_MAX)
    ask = load()
    check_owner_or_denis(who, ask, "close")
    if ask["status"] not in ("open", "answered"):
        raise Conflict(f"ask #{ask['id']} is already closed")
    if who.is_denis and ask["status"] == "open" and not resolution:
        resolution = DENIS_DISMISS_RESOLUTION
    updates = {"status": "closed", "resolution": resolution, "closed_by": who.actor, "closed_at": now,
               "updated_at": now}
    return ask, Change(updates, "closed", {"resolution": resolution, "previous_status": ask["status"]}, now)


def plan_reopen(who: Identity, load: Loader, now: str) -> tuple[dict, Change]:
    ask = load()
    check_owner_or_denis(who, ask, "reopen")
    if ask["status"] not in ("closed", "answered"):
        raise Conflict(f"ask #{ask['id']} is already open")
    cleared = {f: ask[f] for f in CLEARED_BY_REOPEN}
    updates = {"status": "open", **{f: None for f in CLEARED_BY_REOPEN}, "updated_at": now}
    return ask, Change(updates, "reopened", {"previous_status": ask["status"], **cleared}, now)


def check_owner_or_denis(who: Identity, ask: dict, action: str) -> None:
    if not who.is_denis and ask["agent"] != who.agent:
        raise Forbidden(f"only the human or the agent who created ask #{ask['id']} ({ask['agent']}) can {action} it")


# ---------- listing (stores without SQL; must match core.AskService.list) ----------

class _Desc:
    """Sort key wrapper that reverses the order of its value."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return other.value < self.value

    def __eq__(self, other):
        return self.value == other.value


def ask_order(ask: dict) -> tuple:
    """open first (urgent, then oldest), then answered (latest answer first), then closed (latest close first)."""
    rank = {"open": 0, "answered": 1}.get(ask["status"], 2)
    if rank == 0:
        return (0, 0 if ask.get("priority") == "urgent" else 1, ask.get("created_at") or "", ask["id"])
    stamp = ask.get("answered_at") if rank == 1 else ask.get("closed_at")
    return (rank, 0, _Desc(stamp or ""), _Desc(ask["id"]))


def count_by_status(items, statuses: tuple[str, ...]) -> dict:
    counts = dict.fromkeys(statuses, 0)
    for item in items:
        if item.get("status") in counts:
            counts[item["status"]] += 1
    return counts
