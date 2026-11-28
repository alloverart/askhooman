"""The hourly sweep of the files in answers and in task replies (SPEC 7.6, 8.5): what Cloud Scheduler's POST
/internal/sweep does, one slice of users at a time. For each user whose Drive grant the bridge holds, every `afile-`
file (an ask's answer) and every `tfile-` file (a task's reply) in their appDataFolder is deleted when one of the rules
says so. A task's files follow an ask's rules with the names changed (closed: finished, answered_at: replied_at, an
answered ask: an open task), in the same run:

  closed    the file's ask is closed (at any age)
  finished  the file's task is done or dropped (at any age)
  sent      a secret file whose claim is `done` (the retry of the delete after a send), at any age
  sent      also a secret item with downloaded_at and a live file (the send went out, the delete failed, a re-answer
            kept the item), unless a claim is held and unexpired
  expired   a secret file 24 h after min(uploaded_at, answered_at | replied_at) unless its claim is held and
            unexpired; a normal file of an answered ask or an open task 30 days after answered_at | replied_at; both
            bounded by Drive's createdTime (files.lifetime_end: a clock that is ahead can't extend a life past
            createdTime + ttl + 10 min, one that is behind can't end it before createdTime + ttl)
  orphan    no answer or reply names it: 1 h (secret) / 24 h (normal) after Drive's createdTime (not the time in the
            name; a file with no createdTime is kept)
            (only when its holder is known not to name it: an ask or task file that is not there, an ask whose
            appProperties say `open`, or a task that was read; a holder that is listed but could not be read, and
            any holder while the listing is cut short, keep their files until a run that can read them)

What differs for a task (8.5): an open task may name files, so its marks never say «names nothing» as an open ask's do.
Without a read go only a task finished by its confirmed marks, a done claim, the longest ceiling, an absent task and an
unreadable one (kept); every other file of a task that is open by its marks takes a read of the task file.

One listing serves both kinds (LIST_FILES: one cap, one cut-short flag), split by the full name AND Drive's marks
(kind "file", the holder's id and the item's): a `tfile-` that also carries an ask's mark is nobody's (a task's file
never has ask_id), while an `afile-` is the ask's by those marks whatever else it carries, exactly as before 8.5. A file
is deleted only through its holder's guard, which asks Drive for the file's appProperties first
(HostedAsks._delete_answer_file, HostedTasks._delete_reply_file); then, if an item names it, the record is written
(file_id null, deleted_at, deleted_reason): an ask under the guards of 7.4, a task while the item still names the file
(a reply is never replaced), with a file_deleted event. The sweep also heals what the routes leave: an item
whose Drive file is already gone (for the records of each kind changed in the last HEAL_WINDOW, at most HEAL_ASKS of
each). A claim document belongs to the side its own key names (task_id, else ask_id). A user whose grant is revoked, or
whose Drive fails, is logged by id and skipped; the run goes on. Nothing of a file (name, content, link) is logged or
counted: ids and counts only.

What a run reads of a user: the file listing, and, only if there is a file or a claim of either kind, the ask and task
listing (names and appProperties, one listing for both) and the records the files name (read when the first of their
files comes up, plus the few recently changed ones of a kind that has files or claims, for healing): never every
record of the user. One user's turn is bounded (USER_READS reads of ask and task files together, USER_SECONDS): past
that the user is cut for this run (stats user_cut) and the cursor moves on.

Order and reads, across both kinds together: the claims of holders finished by their confirmed marks first; then the
files in the order of nearest expiry, and what needs no read (a holder finished by its listing marks, a done claim, the
longest ceiling, an ask that is open, a holder that is absent or unreadable) is decided before any read; then the capped
reads go to the files nearest to expiry, so the tail is not starved. A secret file its holder does not name (of an
OPEN ask, an answered ask or an OPEN task: a draft) is not an orphan after 1 h: only an ABSENT holder's is; it lives to
its ceiling. A `closed`, `done` or `dropped` mark in the listing is confirmed by one get_meta of the record's file
before anything is deleted on it; a file that went with its finished task takes no read to be recorded either, since
the bridge's finish stamped every item that had a file (plan_finish).

A ceiling needs no record: a file whose Drive createdTime is older than createdTime + 24 h (secret) or 30 days (normal)
+ 10 min is expired whatever its holder says or fails to say (a record that stays unreadable, a stamp that is not a
time), unless a held, unexpired claim says a send is in flight. It is never earlier than anything the rules above
compute.
"""

import dataclasses
import logging
import re
import time
from collections import Counter
from collections.abc import Callable

from ..rules import ANSWER_FILES, Change
from ..task_rules import FINISHED, OPEN, REPLY_FILES
from ..tokens import Identity
from .files import (
    CLOCK_SLACK,
    FILE_ORPHAN,
    SECRET_ORPHAN,
    SECRET_TTL,
    claim_key,
    created_epoch,
    lifetime_end,
    task_claim_key,
)
from .files_api import _epoch
from .records import DrivePool, HostedAsks, HostedTasks
from .store import Store
from .voice import _iso

log = logging.getLogger("asks.hosted")

NORMAL_TTL = 30 * 86400  # seconds after answered_at (replied_at): a normal file of an answered ask (open task) goes
HEAL_WINDOW = 2 * 86400  # seconds: a record changed within this is looked at for items whose Drive file is gone
HEAL_ASKS = 25  # at most this many such asks, and as many tasks, per user per run
USER_READS = 100  # ask and task files (together) one user's turn may read: past this the user is cut (stats user_cut)
USER_SECONDS = 120.0  # seconds one user's turn may take, the same
LIST_FILES = "(name contains 'afile-' or name contains 'tfile-') and trashed = false"  # both kinds, one listing
FILE_NAME = re.compile(r"afile-([1-9][0-9]{0,18})-([1-9][0-9]{0,18})-([0-9]{1,16})")
TASK_FILE_NAME = re.compile(r"tfile-([1-9][0-9]{0,18})-([1-9][0-9]{0,18})-([0-9]{1,16})")


@dataclasses.dataclass(frozen=True, eq=False)
class Side:
    """One kind of holder of files as the sweep tells them apart (SPEC 8.5: 7.6's rules with the names changed)."""

    kind: str  # the record kind in the ask/task listing, and in the log lines
    field: str  # the record's list of files
    key: str  # the holder's id in a Drive file's appProperties and in a claim document
    refuses: str | None  # a key that makes a file not this side's (a task's never carries ask_id); None: none (an
    #                      ask's file is matched as before 8.5, whatever else its appProperties carry)
    name: re.Pattern  # the full name of the holder's Drive files: <prefix>-<holder id>-<item id>-<unix ms>
    live: tuple  # the statuses whose items go by their own stamps (secret 24 h, normal 30 days)
    finished: tuple  # the statuses whose files go at any age ...
    reason: str  # ... with this reason
    stamp: str  # the item's own stamp beside uploaded_at
    opened: tuple  # the statuses that are open by the listing marks
    open_names_nothing: bool  # True: a holder open by its marks names no file, so its files need no read (an ask)
    finish_stamps_items: bool  # True: the finish itself stamped every item that had a file (a task: only the bridge
    #                            finishes one, plan_finish), so a file that went with it takes no read to be recorded
    claim: Callable[[str, int, str], str]  # the claim document's key
    delete: Callable[..., bool]  # (records, holder id, item id, file id, reason): the guarded Drive delete
    read: Callable[..., dict | None]  # (records, listing meta): the record with its events, through the cache
    mark: Callable[..., bool]  # (pool, user, holder, file id, reason, at): the record written under its guard


def _match(side: Side, meta: dict) -> tuple[int, int] | None:
    """(holder id, item id) when the listed file is one of `side`'s by its full name AND Drive marks (kind "file",
    this holder, this item, and for a task no ask_id); else None."""
    found = side.name.fullmatch(meta.get("name") or "")
    props = meta.get("appProperties") or {}
    if (found is None or not meta.get("id") or props.get("kind") != "file" or props.get(side.key) != found.group(1)
            or props.get("file") != found.group(2) or (side.refuses is not None and side.refuses in props)):
        return None
    return int(found.group(1)), int(found.group(2))


def _ours(files: list[dict]) -> list[tuple[Side, dict, int, int]]:
    """(side, meta, holder id, item id) of the listed files that are an ask's answer files or a task's reply files,
    in the listing's order: any other file is never touched."""
    return [(side, meta, *found) for meta in files for side in SIDES if (found := _match(side, meta)) is not None]


def answer_files(files: list[dict]) -> list[tuple[dict, int, int]]:
    """(meta, ask id, item id) of the listed files that are an ask's answer files by name AND Drive marks (kind
    "file", this ask, this item; nothing else is looked at, as before 8.5): any other file is never touched."""
    return [(meta, ask_id, n) for side, meta, ask_id, n in _ours(files) if side is ASKS]


def reply_files(files: list[dict]) -> list[tuple[dict, int, int]]:
    """(meta, task id, item id) of the listed files that are a task's reply files by name AND Drive marks (kind
    "file", this task, this item, no ask_id): any other file is never touched."""
    return [(meta, task_id, n) for side, meta, task_id, n in _ours(files) if side is TASKS]


PURGE_LIMIT = 200  # answer and reply files together Delete Account deletes from Drive (the rest stay, and are counted)


def purge_answer_files(pool: DrivePool, user: str, limit: int | None = None) -> tuple[int, int, int, bool]:
    """Delete Account (SPEC 7.6, 8.5): every answer file and every reply file in the user's Drive, through the same
    guards as the sweep (Drive's own marks), at most `limit` of both kinds together; the asks and tasks are not
    written (they stay, with the user's other files). Best effort, never raises: (deleted, failed, left) counts
    (`left`: past the cap, not tried; with `cut` True the listing itself was cut short, so there may be more than
    that), ids only in the log. Must run while the bridge still holds the user's Drive grant."""
    limit = PURGE_LIMIT if limit is None else limit
    deleted = failed = left = 0
    cut = False
    try:
        records = pool.records(user)
        listed = records.call(records.drive.list_files, LIST_FILES)
        cut = bool(getattr(listed, "truncated", False))
        files = _ours(listed)
        left = max(len(files) - limit, 0)
        for side, meta, holder_id, n in files[:limit]:
            if side.delete(records, holder_id, n, meta["id"], "deleted account"):
                deleted += 1
            else:
                failed += 1
    except Exception as exc:  # Drive down, no grant: the user can delete the app data in Google's settings
        log.warning("hosted: answer and reply files of user %s not deleted with the account: %s", user,
                    type(exc).__name__)
        failed += 1
    return deleted, failed, left, cut


class Holders:
    """The asks, or the tasks (`side`), of one user as this run knows them: `loaded` (id -> the validated record with
    its events, read on demand), the ids `unreadable` (listed, and read without success: a Drive error, a corrupt,
    oversized or invalid file), and what the ask and task listing could not say (`truncated`, the `skipped` ids that
    were too big to list). Whether a holder's files are orphans is answered by `absent` only: never for a holder that
    exists but can't be read, and for a task only when there is no task file (an open task may name files)."""

    def __init__(self, records, side: Side, listing_state: tuple, check: Callable[[], None]):
        self.records, self.side, self.check = records, side, check
        listing, skipped, self.truncated = listing_state
        self.metas: dict[int, dict] = {item_id: meta for (kind, item_id), meta in listing.items() if kind == side.kind}
        self.skipped = {item_id for kind, item_id in skipped if kind == side.kind}
        self.loaded: dict[int, dict] = {}
        self.unreadable: set[int] = set()
        self.finished_ok: set[int] = set()  # finished by the listing marks AND by the record file's own metadata
        self.refuted: set[int] = set()  # marked finished in the listing, not confirmed: decided by a read

    @staticmethod
    def status_of(meta: dict) -> str | None:
        return (meta.get("appProperties") or {}).get("status")

    def is_open(self, holder_id: int) -> bool:
        meta = self.metas.get(holder_id)
        return meta is not None and self.status_of(meta) in self.side.opened

    def _skips_open(self, holder_id: int) -> bool:
        """An ask open by its marks is never read: it has no answer files. A task open by its marks may name some."""
        return self.side.open_names_nothing and self.is_open(holder_id)

    def absent(self, holder_id: int) -> bool:
        """True when the holder is known not to name anything: no record file in a complete listing, or an ask file
        the appProperties of which say `open` (an open ask has no answer files; an open task may have reply files)."""
        if holder_id in self.loaded or holder_id in self.unreadable:
            return False
        if holder_id in self.metas:
            return self._skips_open(holder_id)
        return not self.truncated and holder_id not in self.skipped

    def load(self, holder_id: int) -> dict | None:
        """The record (any task; an answered or closed ask by its appProperties), read once per run; None, and
        remembered as unreadable, if it can't be. The deadline is asked before each read."""
        if holder_id in self.loaded:
            return self.loaded[holder_id]
        meta = self.metas.get(holder_id)
        if (holder_id in self.unreadable or (meta is None and holder_id not in self.skipped)
                or self._skips_open(holder_id)):
            return None
        if meta is None:  # there, over the size cap: never read
            self.unreadable.add(holder_id)
            return None
        self.check()
        try:
            entry = self.side.read(self.records, meta)
        except Exception as exc:  # Drive down, the file gone since the listing, not JSON, too big while reading
            log.warning("hosted sweep: %s #%s not readable: %s", self.side.kind, holder_id, type(exc).__name__)
            entry = None
        if entry is None or entry["id"] != holder_id:
            self.unreadable.add(holder_id)
            return None
        self.loaded[holder_id] = entry
        return entry

    def needs_read(self, holder_id: int) -> bool:
        """True when deciding this holder's files takes a read of its file that has not been made: it is listed,
        neither finished by its marks nor an ask open by them (a task open by its marks is read), and neither read nor
        known to be unreadable."""
        meta = self.metas.get(holder_id)
        settled = (*(self.side.opened if self.side.open_names_nothing else ()), *self.side.finished)
        return (meta is not None and holder_id not in self.loaded and holder_id not in self.unreadable
                and (self.status_of(meta) not in settled or holder_id in self.refuted))

    def confirm_finished(self, holder_id: int) -> bool:
        """True when the holder is finished (closed; done or dropped) by its listing marks AND by one get_meta of its
        file (the listing is an index that can lag; the file's own metadata is strongly consistent). A confirmation
        that fails or says otherwise is remembered as `refuted`: the files of that holder are decided by a read of
        it, never by the mark alone."""
        if holder_id in self.loaded:
            return self.loaded[holder_id]["status"] in self.side.finished
        if holder_id in self.finished_ok or holder_id in self.refuted:
            return holder_id in self.finished_ok
        meta = self.metas.get(holder_id)
        if meta is None or self.status_of(meta) not in self.side.finished:
            return False
        try:
            fresh = self.records.call(self.records.drive.get_meta, meta["id"])
        except Exception as exc:
            log.warning("hosted sweep: %s #%s not confirmed %s: %s", self.side.kind, holder_id, self.side.reason,
                        type(exc).__name__)
            fresh = None
        confirmed = fresh is not None and self.status_of(fresh) in self.side.finished
        (self.finished_ok if confirmed else self.refuted).add(holder_id)
        return confirmed

    def recent(self, now: float, skip: set[int]) -> list[int]:
        """The ids of the last HEAL_ASKS holders changed within HEAL_WINDOW before `now` that can name files (answered
        or closed asks; any task), newest first (what healing looks at), not those in `skip`."""
        found = []
        for holder_id, meta in self.metas.items():
            if holder_id in skip or self._skips_open(holder_id):
                continue
            try:
                changed = _epoch(meta.get("modifiedTime"))
            except (TypeError, ValueError):
                continue
            if now - changed <= HEAL_WINDOW:
                found.append((changed, holder_id))
        return [holder_id for _, holder_id in sorted(found, reverse=True)[:HEAL_ASKS]]


def _gone_reason(side: Side, holder: dict, claim: dict | None) -> str:
    """Why an item whose Drive file is already gone is recorded as deleted: its holder's finish (closed; finished),
    a done claim (sent), or else its life ran out (expired)."""
    if holder["status"] in side.finished:
        return side.reason
    return "sent" if claim is not None and claim.get("state") == "done" else "expired"


def _mark_deleted(pool: DrivePool, user: str, ask: dict, file_id: str, reason: str, at: str) -> bool:
    """The item that names `file_id` gets file_id null, deleted_at `at` and deleted_reason, only if the ask still has
    this status and answered_at and the item still names this file with no deleted_at (checked on the ask as it is
    when written). A secret file deleted as «sent» gets its downloaded_at too when it has none. True if it wrote."""
    asks = HostedAsks(pool.records(user))
    wrote = []

    def plan(load):
        wrote.clear()  # a write that starts over (the file changed meanwhile) plans again from the file as it is
        current = load()
        if current["status"] != ask["status"] or current["answered_at"] != ask["answered_at"]:
            return current, None
        items = current.get(ANSWER_FILES) or []
        found = next((i for i in items if i["file_id"] == file_id and i["deleted_at"] is None), None)
        if found is None:
            return current, None
        gone = {"file_id": None, "deleted_at": at, "deleted_reason": reason}
        if reason == "sent" and found["downloaded_at"] is None:
            gone["downloaded_at"] = at
        wrote.append(found["id"])
        return current, Change({ANSWER_FILES: [{**i, **gone} if i is found else i for i in items]}, "file_deleted",
                               {"id": found["id"], "reason": reason}, at)

    asks.items.mutate(Identity("agent", ask["agent"]), ask["id"], plan)
    return bool(wrote)


def _mark_task_deleted(pool: DrivePool, user: str, task: dict, file_id: str, reason: str, at: str) -> bool:
    """The item of the task's reply_files that names `file_id` gets file_id null, deleted_at `at` and deleted_reason,
    and the task a `file_deleted` {id, reason} event (actor: the owner agent; not one of OWNER_KINDS, so reply_pending
    stays), only if the item still names this file with no deleted_at, checked on the task as it is when written: a
    reply is never replaced, so the item alone says it (SPEC 8.5). A send recorded here gets no downloaded_at: that
    stamp goes with a file_downloaded event, which only the route writes. True if it wrote."""
    tasks = HostedTasks(pool.records(user))
    wrote = []

    def plan(load):
        wrote.clear()  # a write that starts over (the file changed meanwhile) plans again from the file as it is
        current = load()
        items = current.get(REPLY_FILES) or []
        found = next((i for i in items if i["file_id"] == file_id and i["deleted_at"] is None), None)
        if found is None:
            return current, None
        wrote.append(found["id"])
        gone = {"file_id": None, "deleted_at": at, "deleted_reason": reason}
        return current, Change({REPLY_FILES: [{**i, **gone} if i is found else i for i in items]}, "file_deleted",
                               {"id": found["id"], "reason": reason}, at)

    tasks.items.mutate(Identity("agent", task["agent"]), task["id"], plan)
    return bool(wrote)


# The two sides. The ask's delete, read and write are looked up when called (tests patch them by these names).
ASKS = Side(kind="ask", field=ANSWER_FILES, key="ask_id", refuses=None, name=FILE_NAME, live=("answered",),
            finished=("closed",), reason="closed", stamp="answered_at", opened=("open",), open_names_nothing=True,
            finish_stamps_items=False, claim=claim_key,
            delete=lambda records, *args: HostedAsks(records)._delete_answer_file(*args),
            read=lambda records, meta: HostedAsks(records).items._entry(meta),
            mark=lambda *args: _mark_deleted(*args))
TASKS = Side(kind="task", field=REPLY_FILES, key="task_id", refuses="ask_id", name=TASK_FILE_NAME, live=OPEN,
             finished=FINISHED, reason="finished", stamp="replied_at", opened=OPEN, open_names_nothing=False,
             finish_stamps_items=True, claim=task_claim_key,
             delete=lambda records, *args: HostedTasks(records)._delete_reply_file(*args),
             read=lambda records, meta: HostedTasks(records).items._entry(meta),
             mark=lambda *args: _mark_task_deleted(*args))
SIDES = (ASKS, TASKS)


def _claim_side(doc: dict) -> Side:
    """The side a claim document belongs to, by its own key: a task's names task_id (and no ask_id,
    files.FileClaims); any other is an ask's, as before tasks had files. No ask path reads a task's claim."""
    return TASKS if TASKS.key in doc else ASKS


class Interrupted(Exception):
    """A user's turn stopped in the middle (never a failure of a file: it must pass through every `except Exception`)."""


class OutOfTime(Interrupted):
    """The run's deadline passed between two steps: the user is unfinished and the next run resumes at them."""


class UserCut(Interrupted):
    """One user took more than their share of the run (USER_READS reads or `user_over()`): what is left of them
    waits for a later run, and the cursor moves on, so one slow or huge user can't hold the others back."""


def sweep_user(store: Store, pool: DrivePool, user: str, now: float, stats: Counter,
               out_of_time: Callable[[], bool] = lambda: False,
               user_over: Callable[[], bool] = lambda: False) -> None:
    """One user's files of both kinds. Raises what the Drive listing raises (a revoked grant, Drive down); a failure
    of one file is logged and counted, and the next file goes on. Before each step after the first (a record read, a
    file, a healed item) `out_of_time()` raises OutOfTime when true (what is left stays for the next run, which
    resumes at this user: every run does at least one step), and `user_over()` or USER_READS reads raise UserCut."""
    done = reads = 0

    def check(read: bool = False) -> None:
        nonlocal done, reads
        if done and out_of_time():
            raise OutOfTime()
        if done and (user_over() or (read and reads >= USER_READS)):
            raise UserCut()
        done += 1
        reads += read
    records = pool.records(user)
    files = records.call(records.drive.list_files, LIST_FILES)
    files_cut = bool(getattr(files, "truncated", False))  # some files may be unseen: no claim is judged dead on their absence
    mine = _ours(files)
    claims: dict[Side, list[tuple[str, dict]]] = {side: [] for side in SIDES}
    for key, doc in store.find("fileclaims", "user", user):
        claims[_claim_side(doc)].append((key, doc))
    active = [side for side in SIDES if claims[side] or any(entry[0] is side for entry in mine)]
    if not active:
        return  # nothing of the user's answers or replies to look at: the ask and task listing is not even read
    listing = records.listing_state()  # one listing for both kinds
    holders = {side: Holders(records, side, listing, lambda: check(True)) for side in SIDES}
    at = _iso(int(now))
    listed = {meta.get("id") for meta in files}

    def claim_of(side: Side, holder_id: int, file_id: str) -> dict | None:
        return store.get("fileclaims", side.claim(user, holder_id, file_id))

    def held(side: Side, holder_id: int, file_id: str) -> bool:
        claim = claim_of(side, holder_id, file_id)
        return claim is not None and claim.get("state") == "held" and claim.get("until", 0) > now

    def item_of(side: Side, holder: dict | None, meta: dict, n: int) -> dict | None:
        return next((i for i in (holder or {}).get(side.field, []) if i["file_id"] == meta["id"] and i["id"] == n),
                    None)

    def marked_secret(meta: dict) -> bool:
        return (meta.get("appProperties") or {}).get("secret") == "1"

    def rules(side: Side, meta: dict, holder: dict | None, item: dict | None, holder_id: int) -> str:
        """The reason from the holder's own rules ("" = keep). Raises ValueError (or another error of reading a
        stamp) for a stamp that is not a time."""
        if holder is not None and holder["status"] in side.finished:
            return side.reason
        created = created_epoch(meta)
        if holder is not None and item is not None and holder["status"] in side.live:
            if item["secret"]:
                claim = claim_of(side, holder_id, meta["id"])
                if claim is not None and claim.get("state") == "done":
                    return "sent"
                if claim is not None and claim.get("state") == "held" and claim.get("until", 0) > now:
                    return ""
                if item["downloaded_at"] is not None:  # sent, and the delete failed (a re-answer kept the item)
                    return "sent"
                since = min(_epoch(item["uploaded_at"]), _epoch(item[side.stamp]))
                return "expired" if now >= lifetime_end(since, created, SECRET_TTL) else ""
            return "expired" if now >= lifetime_end(_epoch(item[side.stamp]), created, NORMAL_TTL) else ""
        if holder is None and not holders[side].absent(holder_id):
            return ""  # the holder is there but unreadable, or the listing is cut: its files wait (for the ceiling)
        if created is None:  # Drive's word, not the name's: a client can write any time into a name
            return ""
        secret = marked_secret(meta)
        if secret and (holder is not None or holders[side].is_open(holder_id)):
            # a draft: the app uploads on attach, before Send, so an open ask or task, or an answered ask that does
            # not name the file (a re-answer being written), has nothing for it yet: it lives to its 24 h ceiling,
            # not 1 h. The hour is for a holder that is absent.
            return ""
        return "orphan" if now - created >= (SECRET_ORPHAN if secret else FILE_ORPHAN) else ""

    def ceiling(side: Side, meta: dict, holder_id: int, item: dict | None, ttl: float | None = None) -> str:
        """The backstop that needs no record: a file older than createdTime + its ttl + CLOCK_SLACK is expired. That
        is never earlier than anything `rules` computes (files.lifetime_end is bounded by exactly this), so it only
        catches what they can't decide: a record that stays unreadable, a stamp that is not a time. The ttl is the
        item's secret flag when an item names the file, else Drive's own mark (`ttl`: given, the caller knows better:
        before a record is read it is the longest, NORMAL_TTL). A held, unexpired claim is a send in flight: waits."""
        created = created_epoch(meta)
        if created is None:
            return ""
        if ttl is None:
            secret = item["secret"] if item is not None else marked_secret(meta)
            ttl = SECRET_TTL if secret else NORMAL_TTL
        if now < created + ttl + CLOCK_SLACK or held(side, holder_id, meta["id"]):
            return ""
        return "expired"

    def decide(side: Side, meta: dict, holder_id: int, n: int, may_read: bool) -> str | None:
        """The reason for a file ("" = keep), or None when the answer needs a read of its record and `may_read` is
        False. Without a read: a holder finished by its confirmed marks, a done claim (the file was sent), a file past
        the longest ceiling, an ask open by its marks, a holder that is absent or unreadable."""
        found = holders[side]
        holder = found.loaded.get(holder_id)
        if holder is None and found.confirm_finished(holder_id):  # by its marks, confirmed by the file's own metadata
            return side.reason
        if holder is None:
            claim = claim_of(side, holder_id, meta["id"])
            if claim is not None and claim.get("state") == "done":
                return "sent"  # sent: nothing can read it, whatever the record says (read or not)
            if found.needs_read(holder_id):
                if not may_read:
                    return ceiling(side, meta, holder_id, None, NORMAL_TTL) or None
                holder = found.load(holder_id)
        item = item_of(side, holder, meta, n)
        try:
            reason = rules(side, meta, holder, item, holder_id)
        except (TypeError, ValueError, ArithmeticError, OSError):  # a stamp of the item is not a time
            log.warning("hosted sweep: stamps of item %s of %s #%s (user %s) are not readable", n, side.kind,
                        holder_id, user)
            reason = ""
        return reason or ceiling(side, meta, holder_id, item)

    gone_ids: set[str] = set()  # Drive files this run saw gone: deleted, or not there when healing
    unrecorded: dict[tuple[Side, int], list[tuple[str, str]]] = {}  # (side, id) -> (file id, reason) gone unread

    def act(side: Side, meta: dict, holder_id: int, n: int, reason: str) -> None:
        """Delete the file for `reason` (through its holder's guard) and, when the record names it, record it."""
        if not side.delete(records, holder_id, n, meta["id"], reason):
            stats["failed"] += 1
            return
        gone_ids.add(meta["id"])
        stats[f"deleted_{reason}"] += 1
        if reason == "orphan":
            return  # no answer or reply names it
        holder = holders[side].loaded.get(holder_id)
        if holder is not None:
            if item_of(side, holder, meta, n) is not None and side.mark(pool, user, holder, meta["id"], reason, at):
                stats["recorded"] += 1
        elif reason != side.reason or not side.finish_stamps_items:
            unrecorded.setdefault((side, holder_id), []).append((meta["id"], reason))  # decided unread: noted below

    def expected_end(entry) -> float:
        created = created_epoch(entry[1])
        return float("inf") if created is None else created + (SECRET_TTL if marked_secret(entry[1]) else NORMAL_TTL)

    def sweep_one(side: Side, meta: dict, holder_id: int, n: int, may_read: bool) -> bool:
        """Decide and act on one file; False when it waits for a read (`may_read` False). A failure is counted."""
        try:
            reason = decide(side, meta, holder_id, n, may_read)
            if reason:
                act(side, meta, holder_id, n, reason)
            return reason is not None
        except Interrupted:
            raise
        except Exception as exc:
            stats["failed"] += 1
            log.warning("hosted sweep: file %s of %s #%s (user %s) not swept: %s", meta.get("id"), side.kind,
                        holder_id, user, type(exc).__name__)
            return True

    # Claims of holders finished by their (confirmed) marks need no read and nothing can read their files: first, so
    # a user who is cut for reading still gets this.
    dropped: set[str] = set()
    for side in SIDES:
        for key, doc in claims[side]:
            check()
            try:
                if holders[side].confirm_finished(doc.get(side.key)):
                    store.delete("fileclaims", key)
                    dropped.add(key)
                    stats["claims_deleted"] += 1
            except Interrupted:
                raise
            except Exception as exc:
                stats["failed"] += 1
                log.warning("hosted sweep: claim of user %s not cleaned: %s", user, type(exc).__name__)

    # In the order of nearest expiry, both kinds together, so the reads (which are capped, for both together) go to
    # the files that are due first. Everything that needs no read is decided before any read: a tail behind the cap is
    # still looked at.
    waiting = []
    for entry in sorted(mine, key=expected_end):
        check()
        if not sweep_one(*entry, may_read=False):
            waiting.append(entry)
    for entry in waiting:
        check()
        sweep_one(*entry, may_read=True)

    # The files that went without a read of their record: the record is written now, if it can be read (what the cap
    # leaves is only a note in the record: the file is gone).
    for (side, holder_id), gone in unrecorded.items():
        holder = holders[side].load(holder_id)
        if holder is None:
            continue
        for file_id, reason in gone:
            if any(i["file_id"] == file_id and i["deleted_at"] is None for i in holder.get(side.field, [])) \
                    and side.mark(pool, user, holder, file_id, reason, at):
                stats["recorded"] += 1

    # What the routes can leave: an item whose Drive file is already gone but whose record still names it. Only for a
    # kind the user has files or claims of (a user with none of a kind is not looked at for it).
    for side in active:
        found = holders[side]
        for holder_id in found.recent(now, set(found.loaded) | found.unreadable):  # the named ones, and recent ones
            found.load(holder_id)
        for holder in list(found.loaded.values()):
            for item in holder.get(side.field, []):
                if item["file_id"] is None or item["deleted_at"] is not None or item["file_id"] in listed:
                    continue
                check()
                try:
                    if records.call(records.drive.get_meta, item["file_id"]) is not None:
                        continue  # there, only not named like ours: left alone
                    gone_ids.add(item["file_id"])
                    reason = _gone_reason(side, holder, claim_of(side, holder["id"], item["file_id"]))
                    if side.mark(pool, user, holder, item["file_id"], reason, at):
                        stats["healed"] += 1
                except Interrupted:
                    raise
                except Exception as exc:
                    stats["failed"] += 1
                    log.warning("hosted sweep: item %s of %s #%s (user %s) not healed: %s", item["id"], side.kind,
                                holder["id"], user, type(exc).__name__)

    # Claims of holders found finished by a read are dead weight too (the confirmed marks went first, above). So are
    # done claims past their expires_at (90 days) unless an item of their holder still names a live file under them.
    for side in SIDES:
        found = holders[side]
        live = {side.claim(user, holder["id"], item["file_id"])
                for holder in found.loaded.values() for item in holder.get(side.field, [])
                if item["file_id"] is not None and item["file_id"] not in gone_ids}
        try:
            for key, doc in claims[side]:
                if key in dropped:
                    continue
                holder_id = doc.get(side.key)
                holder = found.loaded.get(holder_id)
                expires = doc.get("expires_at")
                lapsed = (doc.get("state") == "done" and isinstance(expires, (int, float)) and expires <= now
                          and not files_cut and key not in live and holder_id not in found.unreadable)
                if (holder is not None and holder["status"] in side.finished) or lapsed:
                    store.delete("fileclaims", key)
                    stats["claims_deleted"] += 1
        except Interrupted:
            raise
        except Exception as exc:
            stats["failed"] += 1
            log.warning("hosted sweep: claims of user %s not cleaned: %s", user, type(exc).__name__)


def sweep(store: Store, pool: DrivePool, now: float, cursor: str | None, limit: int, users: dict | None = None,
          deadline: float | None = None, clock: Callable[[], float] = time.monotonic,
          user_seconds: float = USER_SECONDS) -> tuple[str | None, dict]:
    """At most `limit` users after `cursor` (their ids in order; None = from the start): (the cursor to resume
    from, or None when the last user was reached; counts). `users`: the users collection as the caller read it once
    (default: read here). A user whose Drive can't be reached is skipped, the run goes on; the cursor moves past
    every user it finished. With a `deadline` (on `clock`) the run stops between users, and between the files of
    one, once it passes (each call finishes at least one file): the cursor then names the last finished user, so
    the next call resumes at the unfinished one. One user's turn is also bounded by `user_seconds` and USER_READS: a
    user who hits either is cut (stats user_cut), counted as done for this run, and the cursor moves past them."""
    users = store.all("users") if users is None else users
    todo = [sub for sub in sorted(users) if cursor is None or sub > cursor]
    batch = todo[:limit]
    stats: Counter = Counter()

    def out_of_time() -> bool:
        return deadline is not None and clock() >= deadline

    last = cursor  # the last user finished
    for index, sub in enumerate(batch):
        if index and out_of_time():
            return last, dict(stats)
        if not users[sub].get("google_refresh"):
            stats["unlinked"] += 1  # no grant: nothing the bridge can reach
            last = sub
            continue
        began = clock()
        try:
            sweep_user(store, pool, sub, now, stats, out_of_time, lambda: clock() - began >= user_seconds)
            stats["users"] += 1
        except OutOfTime:
            return last, dict(stats)
        except UserCut:
            stats["users"] += 1
            stats["user_cut"] += 1
            log.warning("hosted sweep: user %s cut for this run (too much to read in one turn)", sub)
        except Exception as exc:  # a revoked grant, Drive down, a store error: this user only
            stats["users_failed"] += 1
            log.warning("hosted sweep: user %s skipped: %s", sub, type(exc).__name__)
        last = sub
    return (last if len(todo) > limit else None), dict(stats)
