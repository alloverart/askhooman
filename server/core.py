"""Asks domain on top of SQLite: the rules from rules.py (validation, permissions, state machine) applied in
SQLite transactions.

Statuses: open -> answered -> closed (see SPEC.md, "State machine").
Every change is recorded in ask_events, so cleared answers/resolutions stay in history.
After every committed change the optional mirror (Drive, SPEC.md "Drive storage") is told to push.
"""

import logging

from .db import Database, ask_from_row, event_from_row, to_json, utc_now
from .rules import (  # noqa: F401  (re-exported: older imports use server.core for these names)
    ANSWER_MAX,
    CONTEXT_MAX,
    CONTROL_CHARS,
    DENIS_DISMISS_RESOLUTION,
    EDITABLE,
    ID_MAX,
    LIMIT_MAX,
    LINK_TITLE_MAX,
    LINKS_MAX,
    OPTION_MAX,
    OPTIONS_MAX,
    PRIORITIES,
    REF_MAX,
    RESOLUTION_MAX,
    STATUS_FILTERS,
    TITLE_MAX,
    URL_MAX,
    AskError,
    Change,
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    _links,
    _options,
    _priority,
    _text,
    check_list_args,
    clean_fields,
    plan_answer,
    plan_close,
    plan_create,
    plan_reopen,
    plan_update,
    valid_id,
)
from .tokens import Identity

log = logging.getLogger("asks")


class AskService:
    def __init__(self, db: Database, notifier=None, mirror=None):
        self.db = db
        self.notifier = notifier
        self.mirror = mirror  # has ask_changed(ask_id); must return at once (the push runs in the background)

    # reads

    def get(self, ask_id: int, with_events: bool = True) -> dict:
        with self.db.transaction() as conn:
            ask = self._load(conn, ask_id)
            if with_events:
                rows = conn.execute("SELECT * FROM ask_events WHERE ask_id = ? ORDER BY id", (ask_id,))
                ask["events"] = [event_from_row(r) for r in rows]
        return ask

    def list(self, status: str = "active", agent: str | None = None, limit: int = 200) -> dict:
        statuses, agent = check_list_args(STATUS_FILTERS, status, agent, limit)

        where = f"status IN ({', '.join('?' * len(statuses))})"
        params: list = list(statuses)
        if agent:
            where += " AND agent = ?"
            params.append(agent)
        # open: urgent first, then oldest; answered: latest answer first; closed: latest close first
        # (rules.ask_order is the same order for stores without SQL)
        query = f"""
            SELECT * FROM asks WHERE {where}
            ORDER BY
                CASE status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END,
                CASE WHEN status = 'open' AND priority = 'urgent' THEN 0 ELSE 1 END,
                CASE WHEN status = 'open' THEN created_at END ASC,
                CASE WHEN status = 'open' THEN id END ASC,
                CASE status WHEN 'answered' THEN answered_at WHEN 'closed' THEN closed_at END DESC,
                id DESC
            LIMIT ?
        """
        with self.db.transaction() as conn:
            asks = [ask_from_row(r) for r in conn.execute(query, (*params, limit))]
            if agent:
                rows = conn.execute("SELECT status, COUNT(*) FROM asks WHERE agent = ? GROUP BY status", (agent,))
            else:
                rows = conn.execute("SELECT status, COUNT(*) FROM asks GROUP BY status")
            counts = {"open": 0, "answered": 0, "closed": 0}
            counts.update({s: n for s, n in rows if s in counts})
        return {"asks": asks, "counts": counts}

    # writes

    def create(self, who: Identity, data: dict) -> dict:
        data = dict(data)
        attachments = data.pop("attachments", None)
        if attachments is not None and attachments != []:  # SPEC v5 step 4: hosted-only; absent, null or [] is fine
            raise Invalid("attachments need the hosted AskHooman")
        fields = plan_create(who, data)
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO asks (agent, title, context, options, links, priority, ref, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (who.agent, fields["title"], fields["context"], to_json(fields["options"]),
                 to_json(fields["links"]), fields["priority"], fields["ref"], now, now),
            )
            ask_id = cur.lastrowid
            self._event(conn, ask_id, now, who, "created", fields)
        self._changed(ask_id)
        return self.get(ask_id, with_events=False)

    def update(self, who: Identity, ask_id: int, data: dict) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            _, change = plan_update(who, data, lambda: self._load(conn, ask_id), now)
            if change:
                self._apply(conn, ask_id, who, change)
        if change:
            self._changed(ask_id)
        return self.get(ask_id, with_events=False)

    def answer(self, who: Identity, ask_id: int, answer) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            ask, change = plan_answer(who, answer, lambda: self._load(conn, ask_id), now)
            if change is None:
                return ask  # same answer again: nothing changes, no event, no second ping
            self._apply(conn, ask_id, who, change)
        self._changed(ask_id)
        ask = self.get(ask_id, with_events=False)
        if self.notifier:
            self.notifier.ask_answered(ask)
        return ask

    def close(self, who: Identity, ask_id: int, resolution=None) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            _, change = plan_close(who, resolution, lambda: self._load(conn, ask_id), now)
            self._apply(conn, ask_id, who, change)
        self._changed(ask_id)
        ask = self.get(ask_id, with_events=False)
        if who.is_denis and self.notifier:
            self.notifier.ask_closed_by_denis(ask)
        return ask

    def reopen(self, who: Identity, ask_id: int) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            before, change = plan_reopen(who, lambda: self._load(conn, ask_id), now)
            self._apply(conn, ask_id, who, change)
        self._changed(ask_id)
        ask = self.get(ask_id, with_events=False)
        # The agent was told about the human's answer/close; tell it that it no longer stands
        # (also makes the web app's Undo honest).
        if who.is_denis and self.notifier:
            self.notifier.ask_reopened_by_denis(ask, before["status"])
        return ask

    # helpers

    def _apply(self, conn, ask_id: int, who: Identity, change: Change) -> None:
        """Write a transition's field updates and its event."""
        assignments = ", ".join(f"{f} = ?" for f in change.updates)
        values = [to_json(v) if f in ("options", "links") else v for f, v in change.updates.items()]
        conn.execute(f"UPDATE asks SET {assignments} WHERE id = ?", (*values, ask_id))
        self._event(conn, ask_id, change.at, who, change.kind, change.payload)

    def _changed(self, ask_id: int) -> None:
        """Tell the mirror (if any) that an ask changed. Never fails the write that already committed."""
        if self.mirror is None:
            return
        try:
            self.mirror.ask_changed(ask_id)
        except Exception:
            log.exception("mirror notification for ask #%s failed", ask_id)

    @staticmethod
    def _load(conn, ask_id: int) -> dict:
        if not valid_id(ask_id):
            raise NotFound(f"no ask #{ask_id}")
        row = conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        if row is None:
            raise NotFound(f"no ask #{ask_id}")
        return ask_from_row(row)

    @staticmethod
    def _event(conn, ask_id: int, at: str, who: Identity, kind: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO ask_events (ask_id, at, actor, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (ask_id, at, who.actor, kind, to_json(payload)),
        )
