"""Asks and tasks of one hosted user, stored in their Drive appDataFolder (SPEC v4 "Data of record").

HostedAsks / HostedTasks have the same methods as AskService / TaskService, so the REST routes and MCP tools
use them unchanged. Every write is a read-modify-write of the file through the shared rules (rules.py,
task_rules.py): download the file fresh, plan the transition against it, apply the field updates, append the
event (id = max + 1, as the app does), rev + 1, writer "bridge", and upload content + appProperties. Fields the
bridge doesn't know stay in the file. Drive has no conditional writes, so just before uploading the bridge
checks that the file's modifiedTime is still the one it read and starts over if not (the app may have written
it); writes from one instance to one file are also serialised.

New ids come from the user's counter in the metadata store (transactional), initialised from the highest id
in Drive; a name that turns out to exist already (another bridge on the same account) moves the counter past it.

Users can put anything into their own appDataFolder, so reads are bounded: listings stop at MAX_FILES files,
files over MAX_FILE_BYTES (by their `size`, and while reading) are skipped as unreadable, downloads are never
decompressed, a list downloads only the files its `limit` can need (chosen from appProperties first), and the
per-instance cache holds validated records only, within a byte budget.
"""

import copy
import json
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..db import utc_now
from ..drive import FILE_FIELDS, DriveBadFile, DriveClient, DriveError, DriveNotFound, parse_name
from ..mirror import ASK, TASK, Kind, app_properties, build_doc
from ..rules import (
    STATUS_FILTERS,
    Change,
    Conflict,
    NotFound,
    Unavailable,
    _Desc,
    ask_order,
    check_list_args,
    count_by_status,
    new_record,
    plan_answer,
    plan_close,
    plan_create,
    plan_reopen,
    plan_update,
    valid_id,
)
from ..task_rules import (
    FINISHED,
    OPEN,
    default_limit,
    new_task_record,
    plan_finish,
    plan_task_create,
    plan_task_reopen,
    plan_task_update,
    task_order,
)
from ..task_rules import STATUS_FILTERS as TASK_FILTERS
from ..tokens import Identity

log = logging.getLogger("asks.hosted.records")

WRITE_ATTEMPTS = 3
ALLOCATE_ATTEMPTS = 5
MAX_FILE_BYTES = 256 * 1024  # a real ask/task file is a few KB (the largest valid ask with history ~200 KB)
MAX_FILES = 5000  # ask + task files read from one user's folder
CACHE_BYTES = 32 * 1024 * 1024  # validated records kept per instance, keyed by (kind, file id, modifiedTime)
LIST_SLACK = 50  # files downloaded beyond `limit` when a list has to choose by appProperties
DOWNLOAD_THREADS = 8
LOCK_STRIPES = 64
ASK_STATUSES = ("open", "answered", "closed")
TASK_STATUSES = OPEN + FINISHED


class _Changed(Exception):
    """The file changed between our read and our write: start over."""


def _rev(meta: dict) -> int:
    try:
        return int((meta.get("appProperties") or {}).get("rev") or 0)
    except (TypeError, ValueError):
        return 0


def _pick(metas: list[dict]) -> dict | None:
    """Among files with one name, the one with the highest rev (as the mirror picks)."""
    return max(metas, key=lambda m: (_rev(m), m.get("modifiedTime") or "")) if metas else None


class DrivePool:
    """Drive clients of hosted users (one per user, sharing one HTTP connection pool), a cache of downloaded
    files and the download threads. One per process."""

    def __init__(self, accounts, client_id: str, client_secret: str, http: httpx.Client, link_url: str,
                 size: int = 256):
        self.accounts = accounts
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http
        self.link_url = link_url
        self.size = size
        self._clients: OrderedDict[str, DriveClient] = OrderedDict()
        self._cache: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.Lock()
        self._stripes = [threading.Lock() for _ in range(LOCK_STRIPES)]
        self.executor = ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS, thread_name_prefix="drive-dl")

    def client(self, sub: str) -> DriveClient:
        with self._lock:
            client = self._clients.get(sub)
            if client is not None:
                self._clients.move_to_end(sub)
                return client
            client = DriveClient(None, None, http=self.http, credentials=lambda: self._credentials(sub),
                                 relink_hint=f"allow Drive access again at {self.link_url}",
                                 file_fields=f"{FILE_FIELDS},size", max_file_bytes=MAX_FILE_BYTES,
                                 max_files=MAX_FILES)
            self._clients[sub] = client
            while len(self._clients) > self.size:
                self._clients.popitem(last=False)
            return client

    def _credentials(self, sub: str) -> tuple[str, str, str]:
        refresh = self.accounts.google_refresh(sub)
        if not refresh:
            raise DriveError("this account has not allowed AskHooman to use its Google Drive yet", reason="not_linked")
        return self.client_id, self.client_secret, refresh

    def forget(self, sub: str) -> None:
        """Drop the user's Drive client and the Google access token it holds (their account was deleted)."""
        with self._lock:
            self._clients.pop(sub, None)

    def records(self, sub: str) -> "UserRecords":
        return UserRecords(self, sub)

    # cache of validated records (REST object + events), LRU within a byte budget

    def cached(self, key: tuple) -> dict | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
        return copy.deepcopy(hit[0]) if hit is not None else None  # callers may modify what they get

    def remember(self, key: tuple, entry: dict) -> None:
        if not key[-1]:  # no modifiedTime: can't tell when it goes stale
            return
        size = len(json.dumps(entry, ensure_ascii=False))
        if size > MAX_FILE_BYTES:
            return
        entry = copy.deepcopy(entry)
        with self._lock:
            old = self._cache.pop(key, None)
            if old is not None:
                self._cache_bytes -= old[1]
            self._cache[key] = (entry, size)
            self._cache_bytes += size
            while self._cache_bytes > CACHE_BYTES and self._cache:
                _, (_, dropped) = self._cache.popitem(last=False)
                self._cache_bytes -= dropped

    def stripe(self, key: tuple) -> threading.Lock:
        return self._stripes[hash(key) % LOCK_STRIPES]

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


class UserRecords:
    """Drive access for one user, with Drive failures turned into readable 503 errors."""

    def __init__(self, pool: DrivePool, sub: str):
        self.pool = pool
        self.sub = sub
        self.accounts = pool.accounts
        self.drive = pool.client(sub)

    def call(self, fn, *args):
        try:
            return fn(*args)
        except DriveNotFound:
            raise
        except DriveBadFile as exc:
            raise Unavailable(f"a file in Google Drive is not readable: {exc}") from None
        except DriveError as exc:
            if exc.reason in ("not_linked", "invalid_grant"):
                if exc.reason == "invalid_grant":
                    self.accounts.unlink_google(self.sub)
                raise Unavailable("AskHooman can't reach this account's Google Drive: its owner must allow access "
                                  f"at {self.pool.link_url} (or connect an agent again)") from None
            log.warning("Drive call failed: %s", exc)
            raise Unavailable("Google Drive is not available right now, try again") from None

    def listing(self) -> dict[tuple[str, int], dict]:
        """(kind, id) -> the file's metadata, for every ask and task file (one files.list, at most MAX_FILES;
        files over MAX_FILE_BYTES are left out as unreadable)."""
        found: dict[tuple[str, int], list[dict]] = {}
        for meta in self.call(self.drive.list_files):
            parsed = parse_name(meta.get("name"))
            if parsed is None or not meta.get("id"):
                continue
            try:
                too_big = int(meta.get("size") or 0) > MAX_FILE_BYTES
            except (TypeError, ValueError):
                too_big = False
            if too_big:
                log.warning("skipped Drive file %s: %s bytes is more than %d", meta.get("name"), meta.get("size"),
                            MAX_FILE_BYTES)
                continue
            found.setdefault(parsed, []).append(meta)
        if self.drive.truncated:
            log.warning("a user's appDataFolder has more than %d ask/task files: only the first ones are read",
                        MAX_FILES)
        return {key: _pick(metas) for key, metas in found.items()}

    def find(self, kind: Kind, item_id: int) -> dict | None:
        return _pick(self.call(self.drive.find, kind.file(item_id)))

    def download(self, meta: dict) -> dict:
        """The file's whole content, fresh (writes keep the fields the bridge doesn't know)."""
        return self.call(self.drive.download, meta["id"])

    def allocate(self, kind: Kind) -> int:
        """The next free id of this kind for this user."""
        if not self.accounts.has_counter(self.sub, kind.name):
            self._raise_counters()
        for _ in range(ALLOCATE_ATTEMPTS):
            item_id = self.accounts.next_id(self.sub, kind.name)
            if self.find(kind, item_id) is None:
                return item_id
            log.warning("%s already exists in a user's Drive: moving the counter past it", kind.file(item_id))
            self._raise_counters()
        raise Unavailable(f"could not find a free {kind.name} id, try again")

    def _raise_counters(self) -> None:
        listing = self.listing()
        for kind in (ASK, TASK):
            top = max((i for name, i in listing if name == kind.name), default=0)
            self.accounts.raise_counter(self.sub, kind.name, top)


class DriveCollection:
    """The asks (or tasks) of one user: reads and writes of their files."""

    def __init__(self, records: UserRecords, kind: Kind, filters: dict, statuses: tuple, order, list_fields,
                 groups: dict):
        self.records = records
        self.kind = kind
        self.filters = filters
        self.statuses = statuses
        self.order = order
        self.list_fields = list_fields
        self.groups = groups  # status -> rank of its block in list order (0 = sorted by age, others latest first)

    # helpers

    def _record(self, doc: dict, item_id: int | None = None) -> dict | None:
        """The REST object of a file's content, or None if it isn't one of ours."""
        if not isinstance(doc, dict) or doc.get("status") not in self.statuses:
            return None
        if not isinstance(doc.get("id"), int) or (item_id is not None and doc["id"] != item_id):
            return None
        record = {field: doc.get(field) for field in self.kind.fields}
        for field in self.list_fields:
            if not isinstance(record[field], list):
                record[field] = []
        return record

    def _missing(self, item_id) -> NotFound:
        return NotFound(f"no {self.kind.name} #{item_id}")

    def _load(self, item_id) -> tuple[dict, dict]:
        """(file metadata, whole content) of an item, downloaded fresh: for writes."""
        if not valid_id(item_id):
            raise self._missing(item_id)
        meta = self.records.find(self.kind, item_id)
        if meta is None:
            raise self._missing(item_id)
        try:
            doc = self.records.download(meta)
        except DriveNotFound:
            raise self._missing(item_id) from None
        if self._record(doc, item_id) is None:
            raise Unavailable(f"{self.kind.file(item_id)} in Google Drive is not a valid {self.kind.name}")
        return meta, doc

    def _entry_of(self, doc: dict, item_id: int | None = None) -> dict | None:
        record = self._record(doc, item_id)
        if record is None:
            return None
        events = doc.get("events")
        record["events"] = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
        return record

    def _entry(self, meta: dict) -> dict | None:
        """The validated record + events of a file, from the cache or downloaded (None: not ours / unreadable)."""
        key = (self.kind.name, meta.get("id"), meta.get("modifiedTime"))
        entry = self.records.pool.cached(key)
        if entry is not None:
            return entry
        entry = self._entry_of(self.records.download(meta))
        if entry is not None:
            self.records.pool.remember(key, entry)
        return entry

    def _entries(self, metas: list[dict]) -> list[dict]:
        def one(meta):
            try:
                return self._entry(meta)
            except (Unavailable, DriveNotFound) as exc:
                log.warning("skipped unreadable Drive file %s: %s", meta.get("name"), exc)
                return None
        return [e for e in self.records.pool.executor.map(one, metas) if e is not None]

    def _hint(self, meta: dict) -> tuple:
        """List position guessed from appProperties alone: status block, then id (oldest first) in the first block
        and last change (latest first) in the others."""
        rank = self.groups.get((meta.get("appProperties") or {}).get("status"), len(self.statuses))
        item_id = (parse_name(meta.get("name")) or ("", 0))[1]
        return (rank, item_id, _Desc("")) if rank == 0 else (rank, 0, _Desc(meta.get("modifiedTime") or ""))

    # reads

    def get(self, item_id: int, with_events: bool = True) -> dict:
        if not valid_id(item_id):
            raise self._missing(item_id)
        meta = self.records.find(self.kind, item_id)
        if meta is None:
            raise self._missing(item_id)
        try:
            entry = self._entry(meta)
        except DriveNotFound:
            raise self._missing(item_id) from None
        if entry is None or entry["id"] != item_id:
            raise Unavailable(f"{self.kind.file(item_id)} in Google Drive is not a valid {self.kind.name}")
        if not with_events:
            entry.pop("events")
        return entry

    def list(self, status: str, agent: str | None, limit: int) -> dict:
        statuses, agent = check_list_args(self.filters, status, agent, limit)
        metas = [meta for (name, _), meta in self.records.listing().items() if name == self.kind.name]
        props = [meta.get("appProperties") or {} for meta in metas]
        counted = [p for p in props if not agent or p.get("agent") == agent]
        counts = count_by_status(counted, self.statuses)
        wanted = [m for m, p in zip(metas, props, strict=True)
                  if p.get("status") in statuses and (not agent or p.get("agent") == agent)]
        if len(wanted) > limit + LIST_SLACK:  # download only what the limit can need
            wanted = sorted(wanted, key=self._hint)[:limit + LIST_SLACK]
        items = []
        for entry in self._entries(wanted):
            entry.pop("events")
            if entry["status"] in statuses and (not agent or entry["agent"] == agent):
                items.append(entry)
        items.sort(key=self.order)
        return {f"{self.kind.name}s": items[:limit], "counts": counts}

    # writes

    def create(self, who: Identity, fields: dict, make_record) -> dict:
        item_id = self.records.allocate(self.kind)
        now = utc_now()
        record = make_record(who, item_id, fields, now)
        event = {"id": 1, self.kind.key: item_id, "at": now, "actor": who.actor, "kind": "created", "payload": fields}
        doc = build_doc({**record, "events": [event]}, 1, None, self.kind)
        self.records.call(self.records.drive.create, self.kind.file(item_id), doc, app_properties(record, 1, self.kind))
        log.info("hosted: created %s for agent %s", self.kind.file(item_id), who.agent)
        return record

    def mutate(self, who: Identity, item_id, plan) -> dict:
        """Read-modify-write of one file. plan(load) -> (current record, Change | None), from the shared rules;
        `load` downloads the file only when the rules get to it (input errors come first, as with SQLite)."""
        with self.records.pool.stripe((self.records.sub, self.kind.name, item_id)):
            for _ in range(WRITE_ATTEMPTS):
                loaded: dict = {}

                def load(loaded=loaded):
                    loaded["meta"], loaded["doc"] = self._load(item_id)
                    return self._record(loaded["doc"], item_id)

                current, change = plan(load)
                if change is None:
                    return current
                try:
                    return self._write(who, item_id, loaded["meta"], loaded["doc"], change)
                except _Changed:
                    continue
        raise Conflict(f"{self.kind.name} #{item_id} is being changed by someone else right now; try again")

    def _write(self, who: Identity, item_id: int, meta: dict, doc: dict, change: Change) -> dict:
        doc.update(change.updates)
        events = doc.get("events") if isinstance(doc.get("events"), list) else []
        ids = [e["id"] for e in events if isinstance(e, dict) and isinstance(e.get("id"), int)]
        events.append({"id": max(ids, default=0) + 1, self.kind.key: item_id, "at": change.at, "actor": who.actor,
                       "kind": change.kind, "payload": change.payload})
        try:
            rev = max(int(doc.get("rev") or 0), _rev(meta)) + 1
        except (TypeError, ValueError):
            rev = _rev(meta) + 1
        doc.update(events=events, rev=rev, schema=1, writer="bridge")
        doc.update(self.kind.marker)
        record = self._record(doc, item_id)
        current = self.records.call(self.records.drive.get_meta, meta["id"])
        if current is None:
            raise self._missing(item_id)
        if current.get("modifiedTime") != meta.get("modifiedTime"):
            raise _Changed()
        try:
            written = self.records.call(self.records.drive.update, meta["id"], doc,
                                        app_properties(record, rev, self.kind))
        except DriveNotFound:
            raise self._missing(item_id) from None
        entry = self._entry_of(doc, item_id)
        if entry is not None and written.get("modifiedTime"):
            self.records.pool.remember((self.kind.name, meta["id"], written["modifiedTime"]), entry)
        return record


class HostedAsks:
    """AskService's interface over one user's Drive."""

    def __init__(self, records: UserRecords):
        self.items = DriveCollection(records, ASK, STATUS_FILTERS, ASK_STATUSES, ask_order, ("options", "links"),
                                     {"open": 0, "answered": 1, "closed": 2})

    def get(self, ask_id: int, with_events: bool = True) -> dict:
        return self.items.get(ask_id, with_events)

    def list(self, status: str = "active", agent: str | None = None, limit: int = 200) -> dict:
        return self.items.list(status, agent, limit)

    def create(self, who: Identity, data: dict) -> dict:
        return self.items.create(who, plan_create(who, data), new_record)

    def update(self, who: Identity, ask_id: int, data: dict) -> dict:
        return self.items.mutate(who, ask_id, lambda load: plan_update(who, data, load, utc_now()))

    def answer(self, who: Identity, ask_id: int, answer) -> dict:
        return self.items.mutate(who, ask_id, lambda load: plan_answer(who, answer, load, utc_now()))

    def close(self, who: Identity, ask_id: int, resolution=None) -> dict:
        return self.items.mutate(who, ask_id, lambda load: plan_close(who, resolution, load, utc_now()))

    def reopen(self, who: Identity, ask_id: int) -> dict:
        return self.items.mutate(who, ask_id, lambda load: plan_reopen(who, load, utc_now()))


class HostedTasks:
    """TaskService's interface over one user's Drive."""

    def __init__(self, records: UserRecords):
        self.items = DriveCollection(records, TASK, TASK_FILTERS, TASK_STATUSES, task_order, ("links",),
                                     {"active": 0, "waiting": 0, "done": 1, "dropped": 1})

    def get(self, task_id: int, with_events: bool = True) -> dict:
        return self.items.get(task_id, with_events)

    def list(self, status: str = "open", agent: str | None = None, limit: int | None = None) -> dict:
        return self.items.list(status, agent, default_limit(status, limit))

    def create(self, who: Identity, data: dict) -> dict:
        return self.items.create(who, plan_task_create(who, data), new_task_record)

    def update(self, who: Identity, task_id: int, data: dict) -> dict:
        return self.items.mutate(who, task_id, lambda load: plan_task_update(who, data, load, utc_now()))

    def done(self, who: Identity, task_id: int, resolution=None) -> dict:
        return self.items.mutate(who, task_id, lambda load: plan_finish(who, "done", resolution, load, utc_now()))

    def drop(self, who: Identity, task_id: int, resolution=None) -> dict:
        return self.items.mutate(who, task_id, lambda load: plan_finish(who, "dropped", resolution, load, utc_now()))

    def reopen(self, who: Identity, task_id: int) -> dict:
        return self.items.mutate(who, task_id, lambda load: plan_task_reopen(who, load, utc_now()))

