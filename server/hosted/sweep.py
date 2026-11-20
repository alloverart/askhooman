"""The hourly sweep of answer files (SPEC 7.6): what Cloud Scheduler's POST /internal/sweep does, one slice of users at
a time. For each user whose Drive grant the bridge holds, every `afile-` file in their appDataFolder is deleted when
one of the rules says so:

  closed    the file's ask is closed (at any age)
  sent      a secret file whose claim is `done` (the retry of the delete after a send), at any age
  sent      also a secret item with downloaded_at and a live file (the send went out, the delete failed, a re-answer
            kept the item), unless a claim is held and unexpired
  expired   a secret file 24 h after min(uploaded_at, answered_at) unless its claim is held and unexpired; a normal
            file of an answered ask 30 days after answered_at; both bounded by Drive's createdTime (files.lifetime_end:
            a clock that is ahead can't extend a life past createdTime + ttl + 10 min, one that is behind can't end
            it before createdTime + ttl)
  orphan    no answer names it: 1 h (secret) / 24 h (normal) after Drive's createdTime (not the time in the name; a
            file with no createdTime is kept)
            (only when the ask is known not to name it: an ask file that is not there, or whose appProperties say
            `open`; an ask that is listed but could not be read, and any ask while the listing is cut short, keep
            their files until a run that can read them)

A file is deleted only through HostedAsks._delete_answer_file, which asks Drive for the file's appProperties first
(kind "file", this ask, this item); then, if an item names it, the ask is written (file_id null, deleted_at,
deleted_reason) under the guards of 7.4. The sweep also heals what the answer route leaves: an item whose Drive file
is already gone (for the asks changed in the last HEAL_WINDOW, at most HEAL_ASKS of them). A user whose grant is
revoked, or whose Drive fails, is logged by id and skipped; the run goes on.
Nothing of a file (name, content, link) is logged or counted: ids and counts only.

What a run reads of a user: the `afile-` listing, and, only if there is an afile file or a claim, the ask listing
(names and appProperties) and the ask files that the afile files name (read when the first of their files comes up,
plus the few recently changed ones for healing): never every ask of the user. One user's turn is bounded (USER_READS
ask reads, USER_SECONDS): past that the user is cut for this run (stats user_cut) and the cursor moves on.

Order and reads: the files go in the order of nearest expiry, and what needs no ask read (an ask closed by its listing
marks, a done claim, the longest ceiling, an open, absent or unreadable ask) is decided before any read; then the capped
reads go to the files nearest to expiry, so the tail is not starved. A secret file of an OPEN ask, or of an answered
ask that does not name it (a draft), is not an orphan after 1 h: only an ABSENT ask's is; it lives to its ceiling. A
`closed` mark in the listing is confirmed by one get_meta of the ask file before anything is deleted on it.

A ceiling needs no ask: a file whose Drive createdTime is older than createdTime + 24 h (secret) or 30 days (normal) +
10 min is expired whatever its ask says or fails to say (an ask that stays unreadable, a stamp that is not a time),
unless a held, unexpired claim says a send is in flight. It is never earlier than anything the rules above compute.
"""

import logging
import re
import time
from collections import Counter
from collections.abc import Callable

from ..rules import ANSWER_FILES, Change
from ..tokens import Identity
from .files import CLOCK_SLACK, FILE_ORPHAN, SECRET_ORPHAN, SECRET_TTL, claim_key, created_epoch, lifetime_end
from .files_api import _epoch
from .records import DrivePool, HostedAsks
from .store import Store
from .voice import _iso

log = logging.getLogger("asks.hosted")

NORMAL_TTL = 30 * 86400  # seconds after answered_at: a normal file of an answered ask is deleted
HEAL_WINDOW = 2 * 86400  # seconds: an ask changed within this is looked at for items whose Drive file is gone
HEAL_ASKS = 25  # at most this many such asks per user per run
USER_READS = 100  # ask files one user's turn may read: past this the user is cut for this run (stats user_cut)
USER_SECONDS = 120.0  # seconds one user's turn may take, the same
LIST_FILES = "name contains 'afile-' and trashed = false"
FILE_NAME = re.compile(r"afile-([1-9][0-9]{0,18})-([1-9][0-9]{0,18})-([0-9]{1,16})")


def answer_files(files: list[dict]) -> list[tuple[dict, int, int]]:
    """(meta, ask id, item id) of the listed files that are an ask's answer files by name AND Drive marks (kind
    "file", this ask, this item): any other file is never touched."""
    mine = []
    for meta in files:
        found = FILE_NAME.fullmatch(meta.get("name") or "")
        props = meta.get("appProperties") or {}
        if (found is None or not meta.get("id") or props.get("kind") != "file"
                or props.get("ask_id") != found.group(1) or props.get("file") != found.group(2)):
            continue
        mine.append((meta, int(found.group(1)), int(found.group(2))))
    return mine


PURGE_LIMIT = 200  # answer files Delete Account deletes from the user's Drive (the rest stay, and are counted)


def purge_answer_files(pool: DrivePool, user: str, limit: int | None = None) -> tuple[int, int, int, bool]:
    """Delete Account (SPEC 7.6): every answer file in the user's Drive, through the same guard as the sweep
    (HostedAsks._delete_answer_file: Drive's own marks), at most `limit`; the asks are not written (they stay, with
    the user's other files). Best effort, never raises: (deleted, failed, left) counts (`left`: past the cap, not
    tried; with `cut` True the listing itself was cut short, so there may be more than that), ids only in the
    log. Must run while the bridge still holds the user's Drive grant."""
    limit = PURGE_LIMIT if limit is None else limit
    deleted = failed = left = 0
    cut = False
    try:
        records = pool.records(user)
        listed = records.call(records.drive.list_files, LIST_FILES)
        cut = bool(getattr(listed, "truncated", False))
        files = answer_files(listed)
        left = max(len(files) - limit, 0)
        for meta, ask_id, n in files[:limit]:
            if HostedAsks(records)._delete_answer_file(ask_id, n, meta["id"], "deleted account"):
                deleted += 1
            else:
                failed += 1
    except Exception as exc:  # Drive down, no grant: the user can delete the app data in Google's settings
        log.warning("hosted: answer files of user %s not deleted with the account: %s", user, type(exc).__name__)
        failed += 1
    return deleted, failed, left, cut


class Asks:
    """The asks of one user as this run knows them: `loaded` (id -> the validated ask with its events, read on
    demand), the ids `unreadable` (listed, not open, and read without success: a Drive error, a corrupt, oversized or
    invalid file), and what the ask listing could not say (`truncated`, the `skipped` ids that were too big to list).
    Whether an ask's files are orphans is answered by `absent` only: never for an ask that exists but can't be read."""

    def __init__(self, records, check: Callable[[], None]):
        self.records, self.check = records, check
        self.metas: dict[int, dict] = {}
        listing, skipped, self.truncated = records.listing_state()
        for (kind, ask_id), meta in listing.items():
            if kind == "ask":
                self.metas[ask_id] = meta
        self.skipped = {ask_id for kind, ask_id in skipped if kind == "ask"}
        self.loaded: dict[int, dict] = {}
        self.unreadable: set[int] = set()
        self.closed_ok: set[int] = set()  # closed by the listing marks AND by the ask file's own metadata
        self.refuted: set[int] = set()  # marked closed in the listing, not confirmed: decided by a read

    @staticmethod
    def status_of(meta: dict) -> str | None:
        return (meta.get("appProperties") or {}).get("status")

    def is_open(self, ask_id: int) -> bool:
        meta = self.metas.get(ask_id)
        return meta is not None and self.status_of(meta) == "open"

    def absent(self, ask_id: int) -> bool:
        """True when the ask is known not to name anything: no ask file in a complete listing, or an ask file the
        appProperties of which say `open` (an open ask has no answer files)."""
        if ask_id in self.loaded or ask_id in self.unreadable:
            return False
        if ask_id in self.metas:
            return self.status_of(self.metas[ask_id]) == "open"
        return not self.truncated and ask_id not in self.skipped

    def load(self, ask_id: int) -> dict | None:
        """The ask (answered or closed by its appProperties), read once per run; None, and remembered as unreadable,
        if it can't be. The deadline is asked before each read."""
        if ask_id in self.loaded:
            return self.loaded[ask_id]
        meta = self.metas.get(ask_id)
        if ask_id in self.unreadable or (meta is None and ask_id not in self.skipped) or self.is_open(ask_id):
            return None
        if meta is None:  # there, over the size cap: never read
            self.unreadable.add(ask_id)
            return None
        self.check()
        try:
            entry = HostedAsks(self.records).items._entry(meta)
        except Exception as exc:  # Drive down, the file gone since the listing, not JSON, too big while reading
            log.warning("hosted sweep: ask #%s not readable: %s", ask_id, type(exc).__name__)
            entry = None
        if entry is None or entry["id"] != ask_id:
            self.unreadable.add(ask_id)
            return None
        self.loaded[ask_id] = entry
        return entry

    def needs_read(self, ask_id: int) -> bool:
        """True when deciding this ask's files takes an ask file read that has not been made: the ask is listed,
        neither open nor closed by its marks, and neither read nor known to be unreadable."""
        meta = self.metas.get(ask_id)
        return (meta is not None and ask_id not in self.loaded and ask_id not in self.unreadable
                and (self.status_of(meta) not in ("open", "closed") or ask_id in self.refuted))

    def confirm_closed(self, ask_id: int) -> bool:
        """True when the ask is closed by its listing marks AND by one get_meta of its file (the listing is an index
        that can lag; the file's own metadata is strongly consistent). A confirmation that fails or says otherwise is
        remembered as `refuted`: the files of that ask are decided by a read of the ask, never by the mark alone."""
        if ask_id in self.loaded:
            return self.loaded[ask_id]["status"] == "closed"
        if ask_id in self.closed_ok or ask_id in self.refuted:
            return ask_id in self.closed_ok
        meta = self.metas.get(ask_id)
        if meta is None or self.status_of(meta) != "closed":
            return False
        try:
            fresh = self.records.call(self.records.drive.get_meta, meta["id"])
        except Exception as exc:
            log.warning("hosted sweep: ask #%s not confirmed closed: %s", ask_id, type(exc).__name__)
            fresh = None
        confirmed = fresh is not None and self.status_of(fresh) == "closed"
        (self.closed_ok if confirmed else self.refuted).add(ask_id)
        return confirmed

    def recent(self, now: float, skip: set[int]) -> list[int]:
        """The ids of the last HEAL_ASKS answered or closed asks changed within HEAL_WINDOW before `now`, newest
        first (what healing looks at), not those in `skip`."""
        found = []
        for ask_id, meta in self.metas.items():
            if ask_id in skip or self.status_of(meta) == "open":
                continue
            try:
                changed = _epoch(meta.get("modifiedTime"))
            except (TypeError, ValueError):
                continue
            if now - changed <= HEAL_WINDOW:
                found.append((changed, ask_id))
        return [ask_id for _, ask_id in sorted(found, reverse=True)[:HEAL_ASKS]]


def _gone_reason(ask: dict, claim: dict | None) -> str:
    if ask["status"] == "closed":
        return "closed"
    return "sent" if claim is not None and claim.get("state") == "done" else "expired"


def _mark_deleted(pool: DrivePool, user: str, ask: dict, file_id: str, reason: str, at: str) -> bool:
    """The item that names `file_id` gets file_id null, deleted_at `at` and deleted_reason, only if the ask still has
    this status and answered_at and the item still names this file with no deleted_at (checked on the ask as it is
    when written). A secret file deleted as «sent» gets its downloaded_at too when it has none. True if it wrote."""
    asks = HostedAsks(pool.records(user))
    wrote = []

    def plan(load):
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


class Interrupted(Exception):
    """A user's turn stopped in the middle (never a failure of a file: it must pass through every `except Exception`)."""


class OutOfTime(Interrupted):
    """The run's deadline passed between two steps: the user is unfinished and the next run resumes at them."""


class UserCut(Interrupted):
    """One user took more than their share of the run (USER_READS ask reads or `user_over()`): what is left of them
    waits for a later run, and the cursor moves on, so one slow or huge user can't hold the others back."""


def sweep_user(store: Store, pool: DrivePool, user: str, now: float, stats: Counter,
               out_of_time: Callable[[], bool] = lambda: False,
               user_over: Callable[[], bool] = lambda: False) -> None:
    """One user's files. Raises what the Drive listing raises (a revoked grant, Drive down); a failure of one file
    is logged and counted, and the next file goes on. Before each step after the first (an ask file read, a file, a
    healed item) `out_of_time()` raises OutOfTime when true (what is left stays for the next run, which resumes at
    this user: every run does at least one step), and `user_over()` or USER_READS ask reads raise UserCut."""
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
    mine = answer_files(files)
    claims = store.find("fileclaims", "user", user)
    if not mine and not claims:
        return  # nothing of the user's answers to look at: the ask listing is not even read
    asks = Asks(records, lambda: check(True))
    at = _iso(int(now))
    listed = {meta.get("id") for meta in files}

    def claim_of(ask_id: int, file_id: str) -> dict | None:
        return store.get("fileclaims", claim_key(user, ask_id, file_id))

    def held(ask_id: int, file_id: str) -> bool:
        claim = claim_of(ask_id, file_id)
        return claim is not None and claim.get("state") == "held" and claim.get("until", 0) > now

    def item_of(ask: dict | None, meta: dict, n: int) -> dict | None:
        return next((i for i in (ask or {}).get(ANSWER_FILES, []) if i["file_id"] == meta["id"] and i["id"] == n), None)

    def marked_secret(meta: dict) -> bool:
        return (meta.get("appProperties") or {}).get("secret") == "1"

    def rules(meta: dict, ask: dict | None, item: dict | None, ask_id: int) -> str:
        """The reason from the answer's own rules ("" = keep). Raises ValueError (or another error of reading a stamp)
        for a stamp that is not a time."""
        if ask is not None and ask["status"] == "closed":
            return "closed"
        created = created_epoch(meta)
        if ask is not None and item is not None and ask["status"] == "answered":
            if item["secret"]:
                claim = claim_of(ask_id, meta["id"])
                if claim is not None and claim.get("state") == "done":
                    return "sent"
                if claim is not None and claim.get("state") == "held" and claim.get("until", 0) > now:
                    return ""
                if item["downloaded_at"] is not None:  # sent, and the delete failed: a re-answer kept the item
                    return "sent"
                since = min(_epoch(item["uploaded_at"]), _epoch(item["answered_at"]))
                return "expired" if now >= lifetime_end(since, created, SECRET_TTL) else ""
            return "expired" if now >= lifetime_end(_epoch(item["answered_at"]), created, NORMAL_TTL) else ""
        if ask is None and not asks.absent(ask_id):
            return ""  # the ask is there but unreadable, or the listing is cut: its files wait (for the ceiling)
        if created is None:  # Drive's word, not the name's: a client can write any time into a name
            return ""
        secret = marked_secret(meta)
        if secret and (ask is not None or asks.is_open(ask_id)):
            # a draft: the app uploads on attach, before Send, so an open ask, or an answered one that does not name
            # the file (a re-answer being written), has nothing for it yet: it lives to its 24 h ceiling, not 1 h.
            # The hour is for an ask that is absent.
            return ""
        return "orphan" if now - created >= (SECRET_ORPHAN if secret else FILE_ORPHAN) else ""

    def ceiling(meta: dict, ask_id: int, item: dict | None, ttl: float | None = None) -> str:
        """The backstop that needs no ask: a file older than createdTime + its ttl + CLOCK_SLACK is expired. That is
        never earlier than anything `rules` computes (files.lifetime_end is bounded by exactly this), so it only
        catches what they can't decide: an ask that stays unreadable, a stamp that is not a time. The ttl is the
        item's secret flag when an item names the file, else Drive's own mark (`ttl`: given, the caller knows better:
        before an ask is read it is the longest, NORMAL_TTL). A held, unexpired claim is a send in flight: waits."""
        created = created_epoch(meta)
        if created is None:
            return ""
        if ttl is None:
            secret = item["secret"] if item is not None else marked_secret(meta)
            ttl = SECRET_TTL if secret else NORMAL_TTL
        if now < created + ttl + CLOCK_SLACK or held(ask_id, meta["id"]):
            return ""
        return "expired"

    def decide(meta: dict, ask_id: int, n: int, may_read: bool) -> tuple[str, dict | None, dict | None] | None:
        """(reason, ask, item) for a file ("" = keep), or None when the answer needs an ask file read and `may_read`
        is False. Without a read: an ask closed by its listing marks, a done claim (the file was sent), a file past
        the longest ceiling, an ask that is open, absent or unreadable."""
        ask = asks.loaded.get(ask_id)
        if ask is None and asks.confirm_closed(ask_id):  # closed by its marks, confirmed by the file's own metadata
            return "closed", None, None
        if ask is None:
            claim = claim_of(ask_id, meta["id"])
            if claim is not None and claim.get("state") == "done":
                return "sent", None, None  # sent: nothing can read it, whatever the ask says (read or not)
            if asks.needs_read(ask_id):
                if not may_read:
                    return (ceiling(meta, ask_id, None, NORMAL_TTL), None, None) if ceiling(
                        meta, ask_id, None, NORMAL_TTL) else None
                ask = asks.load(ask_id)
        item = item_of(ask, meta, n)
        try:
            reason = rules(meta, ask, item, ask_id)
        except (TypeError, ValueError, ArithmeticError, OSError):  # a stamp of the item is not a time
            log.warning("hosted sweep: stamps of item %s of ask #%s (user %s) are not readable", n, ask_id, user)
            reason = ""
        return reason or ceiling(meta, ask_id, item), ask, item

    gone_ids: set[str] = set()  # Drive files this run saw gone: deleted, or not there when healing
    unrecorded: dict[int, list[tuple[str, str]]] = {}  # ask id -> (file id, reason) deleted without reading the ask

    def act(meta: dict, ask_id: int, n: int, reason: str) -> None:
        """Delete the file for `reason` (through the guard) and, when its ask is read and names it, record it."""
        if not HostedAsks(records)._delete_answer_file(ask_id, n, meta["id"], reason):
            stats["failed"] += 1
            return
        gone_ids.add(meta["id"])
        stats[f"deleted_{reason}"] += 1
        if reason == "orphan":
            return  # no answer names it
        ask = asks.loaded.get(ask_id)
        if ask is None:
            unrecorded.setdefault(ask_id, []).append((meta["id"], reason))  # decided without a read: noted below
        elif item_of(ask, meta, n) is not None and _mark_deleted(pool, user, ask, meta["id"], reason, at):
            stats["recorded"] += 1

    def expected_end(entry) -> float:
        created = created_epoch(entry[0])
        return float("inf") if created is None else created + (SECRET_TTL if marked_secret(entry[0]) else NORMAL_TTL)

    # Claims of asks closed by their (confirmed) marks need no read and nothing can read their files: first, so a user
    # who is cut for reading still gets this.
    dropped: set[str] = set()
    for key, doc in claims:
        check()
        try:
            if asks.confirm_closed(doc.get("ask_id")):
                store.delete("fileclaims", key)
                dropped.add(key)
                stats["claims_deleted"] += 1
        except Interrupted:
            raise
        except Exception as exc:
            stats["failed"] += 1
            log.warning("hosted sweep: claim of user %s not cleaned: %s", user, type(exc).__name__)

    # In the order of nearest expiry, so the reads (which are capped) go to the files that are due first. Everything
    # that needs no read is decided before any read: a tail behind the cap is still looked at.
    waiting = []
    for meta, ask_id, n in sorted(mine, key=expected_end):
        check()
        try:
            found = decide(meta, ask_id, n, may_read=False)
            if found is None:
                waiting.append((meta, ask_id, n))
            elif found[0]:
                act(meta, ask_id, n, found[0])
        except Interrupted:
            raise
        except Exception as exc:
            stats["failed"] += 1
            log.warning("hosted sweep: file %s of ask #%s (user %s) not swept: %s", meta.get("id"), ask_id, user,
                        type(exc).__name__)
    for meta, ask_id, n in waiting:
        check()
        try:
            found = decide(meta, ask_id, n, may_read=True)
            if found is not None and found[0]:
                act(meta, ask_id, n, found[0])
        except Interrupted:
            raise
        except Exception as exc:
            stats["failed"] += 1
            log.warning("hosted sweep: file %s of ask #%s (user %s) not swept: %s", meta.get("id"), ask_id, user,
                        type(exc).__name__)

    # The files that went without a read of their ask: the ask is written now, if it can be read (what the cap leaves
    # is only a note in the ask: the file is gone).
    for ask_id, gone in unrecorded.items():
        ask = asks.load(ask_id)
        if ask is None:
            continue
        for file_id, reason in gone:
            if any(i["file_id"] == file_id and i["deleted_at"] is None for i in ask.get(ANSWER_FILES, [])) \
                    and _mark_deleted(pool, user, ask, file_id, reason, at):
                stats["recorded"] += 1

    # What the answer route can leave: an item whose Drive file is already gone but whose ask still names it.
    for ask_id in asks.recent(now, set(asks.loaded) | asks.unreadable):  # the asks the files named, and the recent ones
        asks.load(ask_id)
    for ask in list(asks.loaded.values()):
        for item in ask.get(ANSWER_FILES, []):
            if item["file_id"] is None or item["deleted_at"] is not None or item["file_id"] in listed:
                continue
            check()
            try:
                if records.call(records.drive.get_meta, item["file_id"]) is not None:
                    continue  # there, only not named like ours: left alone
                gone_ids.add(item["file_id"])
                if _mark_deleted(pool, user, ask, item["file_id"],
                                 _gone_reason(ask, claim_of(ask["id"], item["file_id"])), at):
                    stats["healed"] += 1
            except Interrupted:
                raise
            except Exception as exc:
                stats["failed"] += 1
                log.warning("hosted sweep: item %s of ask #%s (user %s) not healed: %s", item["id"], ask["id"], user,
                            type(exc).__name__)

    # Claims of asks found closed by a read are dead weight too (the confirmed marks went first, above). So are done
    # claims past their expires_at (90 days) unless the item they belong to still names a live file.
    live = {claim_key(user, ask["id"], item["file_id"])
            for ask in asks.loaded.values() for item in ask.get(ANSWER_FILES, [])
            if item["file_id"] is not None and item["file_id"] not in gone_ids}
    try:
        for key, doc in claims:
            if key in dropped:
                continue
            ask_id = doc.get("ask_id")
            ask = asks.loaded.get(ask_id)
            expires = doc.get("expires_at")
            lapsed = (doc.get("state") == "done" and isinstance(expires, (int, float)) and expires <= now
                      and not files_cut and key not in live and ask_id not in asks.unreadable)
            if (ask is not None and ask["status"] == "closed") or lapsed:
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
