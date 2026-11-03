"""Tasks domain on top of SQLite (SPEC.md "Tasks and iPhone tab bar (v3)"): the rules from task_rules.py applied
in SQLite transactions.

Statuses: active <-> waiting (update), active|waiting -> done|dropped, done|dropped -> active (reopen).
Only the owner agent writes its tasks; the human's token reads everything and writes nothing (403).
No Telegram notifications. Same errors and event history as asks (core.py);
after every committed change the optional Drive mirror is told to push task-<id>.json.
"""

import logging

from .db import Database, event_from_row, task_from_row, to_json, utc_now
from .rules import Change, NotFound, check_list_args, valid_id
from .task_rules import (  # noqa: F401  (re-exported: older imports use server.tasks for these names)
    DATE,
    EDITABLE,
    FINISHED,
    LIMIT_DEFAULT,
    OWNER_KINDS,
    LIMIT_FINISHED,
    OPEN,
    STATUS_FILTERS,
    WAITING_ON_MAX,
    _date,
    _status,
    clean_task_fields,
    default_limit,
    plan_finish,
    plan_task_answer,
    plan_task_create,
    plan_task_reopen,
    plan_task_reply,
    plan_task_update,
    reply_state,
)
from .tokens import Identity

log = logging.getLogger("asks")


class TaskService:
    def __init__(self, db: Database, mirror=None):
        self.db = db
        self.mirror = mirror  # has task_changed(task_id); must return at once (the push runs in the background)

    # reads

    def get(self, task_id: int, with_events: bool = True) -> dict:
        with self.db.transaction() as conn:
            task = self._load(conn, task_id)
            self._with_reply(conn, [task])
            if with_events:
                rows = conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))
                task["events"] = [event_from_row(r, "task_id") for r in rows]
        return task

    def list(self, status: str = "open", agent: str | None = None, limit: int | None = None) -> dict:
        limit = default_limit(status, limit)
        statuses, agent = check_list_args(STATUS_FILTERS, status, agent, limit)

        where = f"status IN ({', '.join('?' * len(statuses))})"
        params: list = list(statuses)
        if agent:
            where += " AND agent = ?"
            params.append(agent)
        # open: a pending reply first, then next check (no date last), then the longest-running; finished:
        # latest first (task_rules.task_order is the same order for stores without SQL). The pending sort
        # happens before LIMIT, so a reply on a task beyond the limit still comes back first.
        owner_kinds = ", ".join(f"'{k}'" for k in OWNER_KINDS)
        query = f"""
            WITH r AS (
                SELECT task_id,
                       MAX(CASE WHEN kind = 'replied' THEN id END) AS last_reply,
                       MAX(CASE WHEN kind IN ({owner_kinds}) THEN id END) AS last_owner
                FROM task_events WHERE kind IN ('replied', {owner_kinds}) GROUP BY task_id
            )
            SELECT tasks.* FROM tasks LEFT JOIN r ON r.task_id = tasks.id WHERE {where}
            ORDER BY
                CASE WHEN status IN ('active', 'waiting') THEN 0 ELSE 1 END,
                CASE WHEN status IN ('active', 'waiting')
                          AND r.last_reply > COALESCE(r.last_owner, 0) THEN 0 ELSE 1 END,
                CASE WHEN status IN ('active', 'waiting') THEN next_check IS NULL END,
                CASE WHEN status IN ('active', 'waiting') THEN next_check END ASC,
                CASE WHEN status IN ('active', 'waiting') THEN created_at END ASC,
                CASE WHEN status IN ('active', 'waiting') THEN id END ASC,
                CASE WHEN status IN ('done', 'dropped') THEN finished_at END DESC,
                id DESC
            LIMIT ?
        """
        with self.db.transaction() as conn:
            tasks = [task_from_row(r) for r in conn.execute(query, (*params, limit))]
            self._with_reply(conn, tasks)
            if agent:
                rows = conn.execute("SELECT status, COUNT(*) FROM tasks WHERE agent = ? GROUP BY status", (agent,))
            else:
                rows = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
            counts = {"active": 0, "waiting": 0, "done": 0, "dropped": 0}
            counts.update({s: n for s, n in rows if s in counts})
        return {"tasks": tasks, "counts": counts}

    # writes (owner agent only)

    def create(self, who: Identity, data: dict) -> dict:
        fields = plan_task_create(who, data)
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO tasks (agent, title, context, status, waiting_on, next_check, links, ref,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (who.agent, fields["title"], fields["context"], fields["status"], fields["waiting_on"],
                 fields["next_check"], to_json(fields["links"]), fields["ref"], now, now),
            )
            task_id = cur.lastrowid
            self._event(conn, task_id, now, who, "created", fields)
        self._changed(task_id)
        return self.get(task_id, with_events=False)

    def update(self, who: Identity, task_id: int, data: dict) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            _, change = plan_task_update(who, data, lambda: self._load(conn, task_id), now)
            if change:
                self._apply(conn, task_id, who, change)
        if change:
            self._changed(task_id)
        return self.get(task_id, with_events=False)

    def reply(self, who: Identity, task_id: int, data: dict) -> dict:
        """The user replies on an open task (SPEC v5): an event only, the status stays."""
        return self._event_only(plan_task_reply, who, task_id, data)

    def answer(self, who: Identity, task_id: int, data: dict) -> dict:
        """The owner answers the user's reply."""
        return self._event_only(plan_task_answer, who, task_id, data)

    def done(self, who: Identity, task_id: int, resolution=None) -> dict:
        return self._finish(who, task_id, "done", resolution)

    def drop(self, who: Identity, task_id: int, resolution=None) -> dict:
        return self._finish(who, task_id, "dropped", resolution)

    def reopen(self, who: Identity, task_id: int) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            _, change = plan_task_reopen(who, lambda: self._load(conn, task_id), now)
            self._apply(conn, task_id, who, change)
        self._changed(task_id)
        return self.get(task_id, with_events=False)

    # helpers

    def _finish(self, who: Identity, task_id: int, status: str, resolution) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            _, change = plan_finish(who, status, resolution, lambda: self._load(conn, task_id), now)
            self._apply(conn, task_id, who, change)
        self._changed(task_id)
        return self.get(task_id, with_events=False)

    def _event_only(self, plan, who: Identity, task_id: int, data: dict) -> dict:
        now = utc_now()
        with self.db.transaction(write=True) as conn:
            _, change = plan(who, data, lambda: self._load(conn, task_id), now)
            self._apply(conn, task_id, who, change)
        self._changed(task_id)
        return self.get(task_id, with_events=False)

    @staticmethod
    def _with_reply(conn, tasks: "list[dict]") -> None:  # quoted: `list` is the method above, in this class body
        """Set the derived `reply` / `reply_pending` (task_rules.reply_state) on each task, in place."""
        if not tasks:
            return
        kinds = ("replied", *OWNER_KINDS)
        ids = [t["id"] for t in tasks]
        rows = conn.execute(
            f"SELECT * FROM task_events WHERE task_id IN ({', '.join('?' * len(ids))}) "
            f"AND kind IN ({', '.join('?' * len(kinds))}) ORDER BY id",
            (*ids, *kinds),
        )
        events: dict[int, list] = {}
        for row in rows:
            event = event_from_row(row, "task_id")
            events.setdefault(event["task_id"], []).append(event)
        for task in tasks:
            task.update(reply_state(events.get(task["id"], [])))

    def _apply(self, conn, task_id: int, who: Identity, change: Change) -> None:
        """Write a transition's field updates and its event."""
        assignments = ", ".join(f"{f} = ?" for f in change.updates)
        values = [to_json(v) if f == "links" else v for f, v in change.updates.items()]
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values, task_id))
        self._event(conn, task_id, change.at, who, change.kind, change.payload)

    def _changed(self, task_id: int) -> None:
        """Tell the mirror (if any) that a task changed. Never fails the write that already committed."""
        if self.mirror is None:
            return
        try:
            self.mirror.task_changed(task_id)
        except Exception:
            log.exception("mirror notification for task #%s failed", task_id)

    @staticmethod
    def _load(conn, task_id: int) -> dict:
        if not valid_id(task_id):
            raise NotFound(f"no task #{task_id}")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"no task #{task_id}")
        return task_from_row(row)

    @staticmethod
    def _event(conn, task_id: int, at: str, who: Identity, kind: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO task_events (task_id, at, actor, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (task_id, at, who.actor, kind, to_json(payload)),
        )
