"""Mirror of every ask and task into the Google Drive appDataFolder, and replay of the human's actions from the app.

SPEC.md, "Drive storage (v2)" and "Tasks … (v3)" → "Drive". One background thread does all Drive work,
one step at a time:
- a full sync every `poll` seconds: list the folder, download the files that changed since the bridge
  last saw them, replay the human's new answer/close/reopen events through AskService as the human (so events,
  validation and Telegram pings are exactly as with REST), then push every ask and task Drive doesn't
  have yet;
- between full syncs AskService/TaskService wake it after each write, and it pushes what changed.
Asks (ask-<id>.json) and tasks (task-<id>.json) go through the same steps; a `Kind` says what differs
(tables, file name, fixed fields). Ask files and the user's task replies are replayed: in task files the app
writes only `replied` events (SPEC v5), replayed through TaskService.reply; any other change to a task file
is logged and overwritten with the bridge's state.
What Drive holds is tracked in SQLite (drive_files for asks, drive_task_files for tasks: file id, rev,
last event pushed), so a push that failed (Drive down, process restarted) is simply done by a later
cycle, and the first start with an empty table pushes everything that exists (migration). Drive problems
are logged and retried; they never reach an API caller. Nothing in Drive is ever deleted.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass

from .core import AskError, AskService, NotFound
from .db import ASK_FIELDS, TASK_FIELDS
from .drive import DriveBadFile, DriveClient, DriveError, file_name, parse_name
from .tokens import Identity

log = logging.getLogger("asks.drive")

# v2 tables (drive_files, drive_replayed) are unchanged; v3 adds drive_task_files next to them, so a
# v2 database needs no migration of its rows.
SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_files (       -- asks
    ask_id        INTEGER PRIMARY KEY,
    file_id       TEXT NOT NULL,
    rev           INTEGER NOT NULL,             -- highest rev the bridge wrote to or read from the file
    pushed_event  INTEGER NOT NULL,             -- last ask_events.id the file holds; -1 = push needed
    modified_time TEXT,                         -- the file's modifiedTime after that write/read
    extra         TEXT NOT NULL DEFAULT '{}'    -- top-level fields the bridge doesn't know, kept on writes
);
CREATE TABLE IF NOT EXISTS drive_replayed (    -- the human's actions from the app already replayed (or skipped)
    ask_id INTEGER NOT NULL,
    kind   TEXT NOT NULL,
    at     TEXT NOT NULL,
    PRIMARY KEY (ask_id, kind, at)
);
CREATE TABLE IF NOT EXISTS drive_task_replayed (  -- v5: the user's task replies from the app already replayed
    task_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (task_id, kind, at)
);
CREATE TABLE IF NOT EXISTS drive_task_files (  -- tasks: same columns as drive_files
    task_id       INTEGER PRIMARY KEY,
    file_id       TEXT NOT NULL,
    rev           INTEGER NOT NULL,
    pushed_event  INTEGER NOT NULL,             -- last task_events.id the file holds; -1 = push needed
    modified_time TEXT,
    extra         TEXT NOT NULL DEFAULT '{}'
);
"""

DENIS = Identity("denis")
REPLAYED_KINDS = ("answered", "closed", "reopened")
REPLAYED_TASK_KINDS = ("replied",)
FILE_SCHEMA = 1
BACKOFF_MAX = 300.0  # seconds between retries while Drive keeps failing
LOG_EVERY = 600.0  # repeat an unchanged warning this often


@dataclass(frozen=True)
class Kind:
    """One kind of mirrored record. `key` is the id column of both the events table and the mapping table."""
    name: str  # file prefix (ask-<id>.json) and appProperties id key (ask_id)
    table: str
    events: str
    key: str
    mapping: str
    fields: tuple[str, ...]
    marker: tuple[tuple[str, object], ...] = ()  # fixed top-level fields of the file, e.g. ("kind", "task")
    replay: bool = False  # the human's app actions in the file are replayed (asks; tasks: replies, v5)

    def file(self, item_id: int) -> str:
        return file_name(item_id, self.name)

    @property
    def known(self) -> frozenset:
        return frozenset((*self.fields, *(k for k, _ in self.marker), "events", "rev", "schema", "writer"))


ASK = Kind("ask", "asks", "ask_events", "ask_id", "drive_files", ASK_FIELDS, replay=True)
TASK = Kind("task", "tasks", "task_events", "task_id", "drive_task_files", TASK_FIELDS, marker=(("kind", "task"),),
            replay=True)
KNOWN_KEYS = ASK.known


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rev(meta: dict) -> int:
    return _int((meta.get("appProperties") or {}).get("rev"))


def _last_event(item: dict) -> int:
    return max((event["id"] for event in item["events"]), default=0)


def _state(doc: dict, kind: Kind = ASK) -> dict:
    """The part of a file that must equal SQLite: the record's fields and events (and the fixed fields)."""
    return {**{field: doc.get(field) for field in (*kind.fields, *(k for k, _ in kind.marker))},
            "events": doc.get("events")}


def _beyond_replies(remote: dict, local: dict) -> bool:
    """A task file differs from SQLite in more than the user's replies (SPEC v5): compare both without `replied`
    events and updated_at, which a reply changes on both sides."""
    def strip(doc: dict) -> dict:
        state = _state({**doc, **dict(TASK.marker)}, TASK)
        events = state["events"] if isinstance(state["events"], list) else []
        state["events"] = [e for e in events if not (isinstance(e, dict) and e.get("kind") == "replied")]
        state.pop("updated_at", None)
        return state
    return strip(remote) != strip(local)


def build_doc(item: dict, rev: int, extra: dict | None = None, kind: Kind = ASK) -> dict:
    """File content: the REST object + events + rev/schema(/kind)/writer, then fields the bridge doesn't know."""
    doc = {field: item[field] for field in kind.fields}
    doc.update(events=item["events"], rev=rev, schema=FILE_SCHEMA)
    doc.update(kind.marker)
    doc["writer"] = "bridge"
    for key, value in (extra or {}).items():
        doc.setdefault(key, value)
    return doc


def app_properties(item: dict, rev: int, kind: Kind = ASK) -> dict:
    return {f"{kind.name}_id": str(item["id"]), "status": item["status"], "agent": item["agent"], "rev": str(rev)}


class DriveMirror:
    def __init__(self, service: AskService, drive: DriveClient, poll: float = 15.0, tasks=None):
        """tasks: the TaskService, whose tasks are mirrored too (None: asks only)."""
        self.service = service
        self.db = service.db
        self.drive = drive
        self.poll = poll
        self.kinds = (ASK, TASK) if tasks is not None else (ASK,)
        self._services = {ASK.name: service, TASK.name: tasks}
        self._lock = threading.Lock()  # one sync step at a time (the worker or a direct call)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = False
        self._warned: dict[str, tuple[str, float]] = {}

    # ---------- lifecycle ----------

    def init(self) -> None:
        """Create the mirror's tables (only when the mirror is on: with it off the schema stays as without it)."""
        self.db.ensure_schema(SCHEMA)
        self._ready = True

    def start(self) -> None:
        self.init()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="asks-drive", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker. A Drive call in flight is not interrupted; after `timeout` we stop waiting."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def ask_changed(self, ask_id: int) -> None:
        """Called by AskService after every committed write: push soon (the worker finds what changed)."""
        self._wake.set()

    def task_changed(self, task_id: int) -> None:
        """Called by TaskService after every committed write, like ask_changed."""
        self._wake.set()

    # ---------- worker ----------

    def _run(self) -> None:
        next_full = retry_at = 0.0
        failures = 0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= retry_at:
                full = now >= next_full
                try:
                    if full:
                        self.sync_once()
                    else:
                        self.push_pending()
                except Exception as exc:
                    failures += 1
                    delay = min(self.poll * 2 ** min(failures - 1, 10), BACKOFF_MAX)
                    retry_at = now + delay
                    self._warn("sync", str(exc), f"Drive sync failed ({failures} in a row), next try in "
                               f"{delay:.0f} s: {exc}", exc_info=not isinstance(exc, DriveError))
                else:
                    if failures:
                        log.info("Drive sync works again after %d failed attempts", failures)
                        self._warned.pop("sync", None)
                    failures, retry_at = 0, 0.0
                    if full:
                        next_full = now + self.poll
            until = retry_at if retry_at > now else next_full
            self._wake.wait(max(until - time.monotonic(), 0.0))
            self._wake.clear()

    def _warn(self, key: str, detail: str, message: str, exc_info: bool = False) -> None:
        """Log a warning, but repeat the same problem (same key and detail) only every LOG_EVERY seconds."""
        now = time.monotonic()
        previous = self._warned.get(key)
        if previous and previous[0] == detail and now - previous[1] < LOG_EVERY:
            return
        self._warned[key] = (detail, now)
        log.warning(message, exc_info=exc_info)

    # ---------- sync steps ----------

    def sync_once(self) -> None:
        """Full sync: pull every file that changed since the bridge last saw it, then push what Drive lacks.
        Raises DriveError if Drive is unreachable (the worker retries later)."""
        with self._lock:
            if not self._ready:
                self.init()
            listed: dict[tuple[str, int], list[dict]] = {}
            for meta in self.drive.list_files():  # ask and task files in one listing
                parsed = parse_name(meta.get("name"))
                if parsed is not None and meta.get("id"):  # anything else in the folder is not ours: ignored
                    listed.setdefault(parsed, []).append(meta)
            for kind in self.kinds:
                rows = self._rows(kind)
                for item_id in sorted({i for name, i in listed if name == kind.name} | set(rows)):
                    row = rows.get(item_id)
                    meta = self._pick(kind, item_id, listed.get((kind.name, item_id), []), row)
                    self._guard(kind, item_id, self._check_file, kind, item_id, meta, row)
            self._push_dirty()

    def push_pending(self) -> None:
        """Push every ask and task whose latest change Drive doesn't have yet (oldest change first)."""
        with self._lock:
            if not self._ready:
                self.init()
            self._push_dirty()

    def _check_file(self, kind: Kind, item_id: int, meta: dict | None, row: dict | None) -> None:
        if row is not None and (meta is None or meta["id"] != row["file_id"]):
            # Our file is missing from the listing: deleted, or the listing lags. Ask Drive directly.
            meta = self.drive.get_meta(row["file_id"])
            if meta is None:
                log.warning("Drive file for %s #%s is gone: a new one will be written", kind.name, item_id)
                self._forget(kind, item_id)
                return
        if meta is not None and (row is None or self._changed(row, meta)):
            self._pull(kind, item_id, meta, row)

    def _push_dirty(self) -> None:
        for kind in self.kinds:
            for item_id in self._dirty(kind):
                self._guard(kind, item_id, self._push, kind, item_id)

    def _guard(self, kind: Kind, item_id: int, step, *args) -> None:
        """Run one record's step. Drive being down aborts the whole sync (raised); a problem with this one
        record is logged and retried next cycle without holding up the others."""
        try:
            step(*args)
        except DriveError as exc:
            if exc.transient:
                raise
            self._warn(f"{kind.name} {item_id}", str(exc),
                       f"Drive sync of {kind.name} #{item_id} failed, will retry: {exc}")
        except Exception as exc:
            self._warn(f"{kind.name} {item_id}", repr(exc), f"Drive sync of {kind.name} #{item_id} failed, will retry",
                       exc_info=True)

    def _pull(self, kind: Kind, item_id: int, meta: dict, row: dict | None) -> None:
        """Read a file that changed since the bridge last saw it (or one it adopts), replay the human's new
        actions from it (asks) and remember what was seen. If the file still differs from SQLite afterwards,
        the record stays marked for a push, so the file converges to the bridge's state."""
        name = meta.get("name") or kind.file(item_id)
        service = self._services[kind.name]
        try:
            local = service.get(item_id)
        except NotFound:
            self._warn(f"orphan {meta['id']}", "", f"Drive file {name} has no {kind.name} #{item_id} in SQLite: ignored")
            return
        try:
            remote = self.drive.download(meta["id"])
        except DriveBadFile as exc:
            log.warning("Drive file %s is unreadable (%s): it will be rewritten from SQLite", name, exc)
            remote = {}
        if not remote:
            pass
        elif row is None and (remote.get("agent"), remote.get("created_at")) != (local["agent"], local["created_at"]):
            # Not the record we have under this id (e.g. the database was restored from an old copy).
            log.warning("Drive file %s holds another %s (agent %s, created %s): it will be rewritten from SQLite",
                        name, kind.name, remote.get("agent"), remote.get("created_at"))
        elif kind.replay and self._replay(kind, item_id, remote.get("events")):
            local = service.get(item_id)
        rev = max(_rev(meta), _int(remote.get("rev")), row["rev"] if row else 0)
        in_sync = remote.get("writer") == "bridge" and _state(remote, kind) == _state({**local, **dict(kind.marker)}, kind)
        if remote and row is not None and kind is TASK and not in_sync and _beyond_replies(remote, local):
            # In task files the app writes only replies (replayed above); anything else someone changed: nothing to
            # apply, the bridge's state wins.
            log.warning("Drive file %s was changed outside the bridge (rev %s, writer %s): overwriting it",
                        name, rev, remote.get("writer"))
        extra = {key: value for key, value in remote.items() if key not in kind.known}
        self._save(kind, item_id, meta["id"], rev, _last_event(local) if in_sync else -1, meta.get("modifiedTime"),
                   extra)

    def _replay(self, item_kind: Kind, item_id: int, events) -> int:
        """Apply the human's actions from the file that the bridge doesn't have yet, in file order, as the human
        through AskService (asks: answer/close/reopen) or TaskService (tasks: replies, SPEC v5). An action is new
        if no SQLite event and no earlier replay has its kind + at. Invalid ones (e.g. the agent closed the ask
        or finished the task meanwhile) are logged and skipped. Returns how many were applied."""
        if not isinstance(events, list):
            return 0
        is_task = item_kind is TASK
        replayed_table, kinds = ("drive_task_replayed", REPLAYED_TASK_KINDS) if is_task else ("drive_replayed",
                                                                                             REPLAYED_KINDS)
        with self.db.transaction() as conn:
            seen = {(r["kind"], r["at"]) for r in conn.execute(
                f"SELECT kind, at FROM {item_kind.events} WHERE {item_kind.key} = ? "
                f"UNION SELECT kind, at FROM {replayed_table} WHERE {item_kind.key} = ?",
                (item_id, item_id))}
        applied = 0
        for event in events:
            if not isinstance(event, dict) or event.get("actor") != "denis" or event.get("kind") not in kinds:
                continue
            kind, at = event["kind"], str(event.get("at"))
            if (kind, at) in seen:
                continue
            seen.add((kind, at))
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            try:
                if is_task:
                    self._services[TASK.name].reply(DENIS, item_id, {"text": payload.get("text"),
                                                                     "preset": payload.get("preset")})
                elif kind == "answered":
                    self.service.answer(DENIS, item_id, payload.get("answer"))
                elif kind == "closed":
                    self.service.close(DENIS, item_id, payload.get("resolution"))
                else:
                    self.service.reopen(DENIS, item_id)
            except AskError as exc:
                log.warning("Drive: skipped the human's %s of %s #%s from the app (at %s): %s",
                            kind, item_kind.name, item_id, at, exc)
            else:
                log.info("Drive: replayed the human's %s of %s #%s from the app (at %s)",
                         kind, item_kind.name, item_id, at)
                applied += 1
            with self.db.transaction(write=True) as conn:
                conn.execute(f"INSERT OR IGNORE INTO {replayed_table} ({item_kind.key}, kind, at) VALUES (?, ?, ?)",
                             (item_id, kind, at))
        return applied

    def _push(self, kind: Kind, item_id: int) -> None:
        """Write the record from SQLite to its file, creating the file the first time. Checks the file's rev
        first: if someone wrote it since the bridge last saw it, that is pulled (replayed) before the push."""
        row = self._row(kind, item_id)
        if row is not None:
            meta = self.drive.get_meta(row["file_id"])
            if meta is None:
                log.warning("Drive file for %s #%s is gone: writing a new one", kind.name, item_id)
                self._forget(kind, item_id)
                row = None
            elif self._changed(row, meta):
                self._pull(kind, item_id, meta, row)
                row = self._row(kind, item_id)
        if row is None:
            # Adopt a file that exists without a mapping (lost table, or our create whose reply never came).
            meta = self._pick(kind, item_id, self.drive.find(kind.file(item_id)), None)
            if meta is not None:
                self._pull(kind, item_id, meta, None)
                row = self._row(kind, item_id)
        item = self._services[kind.name].get(item_id)
        last = _last_event(item)
        if row is not None and row["pushed_event"] >= last:
            return  # the file already holds this state
        rev = (row["rev"] if row else 0) + 1
        extra = json.loads(row["extra"]) if row else {}
        doc = build_doc(item, rev, extra, kind)
        properties = app_properties(item, rev, kind)
        if row is None:
            meta = self.drive.create(kind.file(item_id), doc, properties)
            file_id = meta["id"]
            log.info("Drive: created %s (rev %d)", kind.file(item_id), rev)
        else:
            meta = self.drive.update(row["file_id"], doc, properties)
            file_id = row["file_id"]
        self._save(kind, item_id, file_id, rev, last, meta.get("modifiedTime"), extra)

    # ---------- helpers ----------

    @staticmethod
    def _changed(row: dict, meta: dict) -> bool:
        """Someone wrote the file since the bridge last saw it: a higher rev, or (two writers that both
        wrote rev N+1) a different modifiedTime."""
        modified = meta.get("modifiedTime")
        return _rev(meta) > row["rev"] or (modified is not None and modified != row["modified_time"])

    def _pick(self, kind: Kind, item_id: int, metas: list[dict], row: dict | None) -> dict | None:
        """The file for this record among same-named ones: the mapped one, else the highest rev."""
        if not metas:
            return None
        if row is not None:
            for meta in metas:
                if meta["id"] == row["file_id"]:
                    return meta
        if len(metas) > 1:
            self._warn(f"dup {kind.name} {item_id}", str(len(metas)),
                       f"Drive has {len(metas)} files named {kind.file(item_id)}: "
                       "using the one with the highest rev, the others are ignored")
        return max(metas, key=lambda meta: (_rev(meta), meta.get("modifiedTime") or ""))

    def _dirty(self, kind: Kind) -> list[int]:
        """Records whose latest event the Drive file doesn't hold (or that have no file), oldest change first."""
        with self.db.transaction() as conn:
            rows = conn.execute(f"""
                SELECT id FROM (
                    SELECT a.id, d.{kind.key} AS mapped, d.pushed_event AS pushed,
                           (SELECT COALESCE(MAX(e.id), 0) FROM {kind.events} e WHERE e.{kind.key} = a.id) AS last
                    FROM {kind.table} a LEFT JOIN {kind.mapping} d ON d.{kind.key} = a.id
                )
                WHERE mapped IS NULL OR pushed < last
                ORDER BY last, id
            """).fetchall()
        return [row["id"] for row in rows]

    def _rows(self, kind: Kind) -> dict[int, dict]:
        with self.db.transaction() as conn:
            return {row[kind.key]: dict(row) for row in conn.execute(f"SELECT * FROM {kind.mapping}")}

    def _row(self, kind: Kind, item_id: int) -> dict | None:
        with self.db.transaction() as conn:
            row = conn.execute(f"SELECT * FROM {kind.mapping} WHERE {kind.key} = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def _save(self, kind: Kind, item_id: int, file_id: str, rev: int, pushed_event: int, modified_time: str | None,
              extra: dict) -> None:
        with self.db.transaction(write=True) as conn:
            conn.execute(
                f"""INSERT INTO {kind.mapping} ({kind.key}, file_id, rev, pushed_event, modified_time, extra)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT ({kind.key}) DO UPDATE SET file_id = excluded.file_id, rev = excluded.rev,
                        pushed_event = excluded.pushed_event, modified_time = excluded.modified_time,
                        extra = excluded.extra""",
                (item_id, file_id, rev, pushed_event, modified_time, json.dumps(extra, ensure_ascii=False)),
            )

    def _forget(self, kind: Kind, item_id: int) -> None:
        with self.db.transaction(write=True) as conn:
            conn.execute(f"DELETE FROM {kind.mapping} WHERE {kind.key} = ?", (item_id,))
