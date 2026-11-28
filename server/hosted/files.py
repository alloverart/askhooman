"""Files in answers on hosted asks (SPEC section 7.2 "Data model") and in replies on hosted tasks (8.1 "Data model"):
what the ask's `answer_files` and the task's `reply_files` may be.

The user's apps upload each file to the user's Drive appDataFolder (afile-<ask id>-<n>-<unix ms>, appProperties ask_id
and kind "file") and write `answer_files` with the answer, in the same write. No MCP tool or REST body sets it. The
list is the user's, so the bridge never trusts it: validate_files checks a list the bridge is about to write (a field
path in the error), clean_files reads one back (a bad item is dropped, never served) and ties it to the answer it
belongs to, as clean_voice does for a recording. Names are cleaned on input and again on every read (clean_name), so a
hand-edited ask file can't carry more than the apps could. The ask's status decides the rest: on an `open` ask the list
reads as [], on a `closed` one it is shown as stored (the route gives no download then).

    id             unique within the ask's answers, 1 ...
    name           the device's name, cleaned (clean_name), 1-200 chars; never a path
    mime           type/subtype, lower-cased, else application/octet-stream
    bytes          1 ... MAX_FILE_BYTES; a secret file 1 ... MAX_SECRET_BYTES
    file_id        the Drive file id; null once deleted, exactly when deleted_at is set
    secret         boolean
    uploaded_at, answered_at (the ask's), downloaded_at, blocked_at, deleted_at: ISO-8601 stamps, the last three or null
    deleted_reason null, or sent, closed, reopened, expired, replaced

A reply's file is the same item with reply_id and replied_at (the id and `at` of the `replied` event that carried it)
in place of answered_at, and deleted_reason null, sent, finished or expired (REPLY_ITEM_KEYS). The apps upload it as
tfile-<task id>-<n>-<unix ms> (appProperties task_id and kind "file") and add it to `reply_files` in the write of their
reply; clean_reply_files reads the list back against the task file's own events. A reply is a new message, not a
changed answer: the files of several replies coexist (at most MAX_FILES a reply, MAX_TASK_FILES a task), none goes
stale, and the task's status never empties the list.

Nothing here touches Drive or logs anything of a file.
"""

import hashlib
import json
import re
import secrets
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote

from ..rules import Invalid
from ..task_rules import REPLY_FILES
from .crypto import CryptoError
from .store import DELETE, UNCHANGED, Store
from .voice import FILE_ID, LINK_TTL, MEDIA_PATH, STAMP_MAX, TOKEN_MAX, _iso

MAX_FILES = 6
MAX_FILE_BYTES = 104_857_600  # 100 MiB
MAX_SECRET_BYTES = 10_485_760  # 10 MiB
PART_BYTES = 32_505_856  # 31 MiB, the most body one file response carries: Cloud Run refuses a response of 32 MiB
# or more (spike 1, 20.11.2026), so a larger normal file is fetched in Range parts (files_api, cli/asks.py)
SECRET_TTL = 24 * 3600  # seconds: a secret file not sent by then is deleted (deleted_reason "expired")
SECRET_ORPHAN = 3600  # seconds: a secret file no list mentions, deleted by the sweep after this
FILE_ORPHAN = 24 * 3600  # seconds: the same for a normal file
REQUEST_TIMEOUT = 900  # seconds a download may take
CLOCK_SLACK = 600  # seconds a client's clock may differ from Drive's before Drive's createdTime decides (SPEC 7.6)

MAX_TASK_FILES = 30  # the most items a task's reply_files holds (SPEC 8.1): files add up over a task's life

ITEM_KEYS = ("id", "name", "mime", "bytes", "file_id", "secret", "uploaded_at", "answered_at", "downloaded_at",
             "blocked_at", "deleted_at", "deleted_reason")
DELETED_REASONS = ("sent", "closed", "reopened", "expired", "replaced")
# A reply's item (SPEC 8.1): an answer's, with reply_id and replied_at in place of answered_at, and its own reasons
REPLY_ITEM_KEYS = ("id", "name", "mime", "bytes", "file_id", "secret", "uploaded_at", "reply_id", "replied_at",
                   "downloaded_at", "blocked_at", "deleted_at", "deleted_reason")
REPLY_DELETED_REASONS = ("sent", "finished", "expired")
NAME_MAX = 200
OCTET_STREAM = "application/octet-stream"
MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")
EXTENSION = re.compile(r"[A-Za-z0-9]{1,10}")
UNSAFE = set('/\\<>:"|?*')  # path separators and the Windows-reserved characters
DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
FIELD = "answer_files"
LINK_CONTEXT = "file"  # the AEAD context of a file link's token: a voice token (context "media") never opens as one
CLAIMS = {"user": str, "agent": str, "ask_id": int, "answered_at": str, "file_id": str, "n": int, "exp": int}


# ---------- names ----------

def _extension(name: str) -> str:
    """The last extension with its dot, "" for none (a leading dot is not an extension)."""
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def clean_name(raw: str) -> str:
    """A file name as the bridge shows it (SPEC 7.2): NFC; control, format (bidi too), private-use, surrogate and line
    or paragraph separator characters, `/`, `\\`, `<>:"|?*` replaced by `_`; leading dots and spaces (and a leading
    path) and trailing dots and spaces stripped; a Windows device name (CON, NUL, COM1 ... with or without an
    extension, any case) gets a leading `_`; at most NAME_MAX chars, cut before the extension; empty -> `file`."""
    name = unicodedata.normalize("NFC", raw)
    name = name.lstrip(" ./\\")  # a leading "../" is dropped, not turned into "..__"
    name = "".join("_" if ch in UNSAFE or unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") else ch
                   for ch in name)
    name = name.lstrip(" .").rstrip(" .")
    if name.split(".", 1)[0].rstrip(" ").upper() in DEVICES:
        name = "_" + name
    if len(name) > NAME_MAX:
        ext = _extension(name)
        name = name[:NAME_MAX] if len(ext) >= NAME_MAX else name[:NAME_MAX - len(ext)] + ext
        stem = name[:len(name) - len(_extension(name))].rstrip(" .")
        name = (stem + _extension(name)) if stem else name
    return name or "file"


def download_name(ask_id: int, n: int, name: str) -> tuple[str, str]:
    """(the ASCII `filename`, the RFC 5987 `filename*` value) of a download's Content-Disposition. The first is
    ask-<id>-file-<n>[.<ext>], <ext> the cleaned name's last extension when it is 1-10 ASCII letters or digits
    (lower-cased); the second is UTF-8''<percent-encoded cleaned name>. The caller quotes the first."""
    name = clean_name(name)
    ext = _extension(name)[1:]
    ascii_name = f"ask-{ask_id}-file-{n}" + (f".{ext.lower()}" if EXTENSION.fullmatch(ext) else "")
    return ascii_name, "UTF-8''" + quote(name, safe="")


# ---------- the list ----------

def _count(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _stamp(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= STAMP_MAX


def _checked(value, keys: tuple, reasons: tuple, path: str, own: Callable[[dict], None]) -> dict:
    """The checks an answer's item and a reply's item share (SPEC 7.2, 8.1): every key of `keys` there; id, name,
    secret, bytes, mime, file_id with deleted_at, deleted_reason (one of `reasons`), uploaded_at, downloaded_at,
    blocked_at. `own(value)` checks the kind's own keys after uploaded_at (raising Invalid). Returns the item in `keys`
    order with its name cleaned and its mime normalised; Invalid, with the field's path, for anything else."""
    if not isinstance(value, dict):
        raise Invalid(f"{path} must be an object")
    for key in keys:
        if key not in value:
            raise Invalid(f"{path}.{key} is missing")
    if not _count(value["id"], 1, 2**53):
        raise Invalid(f"{path}.id must be a positive integer")
    if not isinstance(value["name"], str):
        raise Invalid(f"{path}.name must be a string")
    try:
        value["name"].encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid(f"{path}.name contains characters that are not valid Unicode") from None
    secret = value["secret"]
    if not isinstance(secret, bool):
        raise Invalid(f"{path}.secret must be true or false")
    cap = MAX_SECRET_BYTES if secret else MAX_FILE_BYTES
    if not _count(value["bytes"], 1, cap):
        raise Invalid(f"{path}.bytes must be 1 ... {cap}")
    mime = value["mime"]
    mime = mime.lower() if isinstance(mime, str) and MIME.fullmatch(mime.lower()) else OCTET_STREAM
    file_id, deleted_at = value["file_id"], value["deleted_at"]
    if file_id is None:
        if not _stamp(deleted_at):
            raise Invalid(f"{path}.deleted_at is required when file_id is null")
    elif not (isinstance(file_id, str) and FILE_ID.fullmatch(file_id)):
        raise Invalid(f"{path}.file_id must be letters, digits, - and _ (at most 200), or null")
    elif deleted_at is not None:
        raise Invalid(f"{path}.file_id must be null when deleted_at is set")
    reason = value["deleted_reason"]
    if reason is not None and (deleted_at is None or reason not in reasons):
        raise Invalid(f"{path}.deleted_reason must be null or one of {', '.join(reasons)}, with deleted_at set")
    if not _stamp(value["uploaded_at"]):
        raise Invalid(f"{path}.uploaded_at must be an ISO-8601 timestamp")
    own(value)
    for key in ("downloaded_at", "blocked_at"):
        if value[key] is not None and not _stamp(value[key]):
            raise Invalid(f"{path}.{key} must be an ISO-8601 timestamp or null")
    return {**{key: value[key] for key in keys}, "name": clean_name(value["name"]), "mime": mime}


def _item(value, answered_at, index: int) -> dict:
    """One well-formed item of an answer in ITEM_KEYS order with its name cleaned and its mime normalised; Invalid,
    with the field's path, for anything else (an answered_at that is not the ask's included)."""
    path = f"{FIELD}[{index}]"

    def own(value):
        if not _stamp(value["answered_at"]):
            raise Invalid(f"{path}.answered_at must be an ISO-8601 timestamp")
        if value["answered_at"] != answered_at:
            raise Invalid(f"{path}.answered_at must be the ask's answered_at")
    return _checked(value, ITEM_KEYS, DELETED_REASONS, path, own)


def _reply_item(value, index: int) -> dict:
    """One well-formed item of a task's reply_files in REPLY_ITEM_KEYS order, cleaned as _item cleans an answer's;
    Invalid, with the field's path, for anything else. Which reply it names is clean_reply_files' check."""
    path = f"{REPLY_FILES}[{index}]"

    def own(value):
        if not _count(value["reply_id"], 1, 2**53):
            raise Invalid(f"{path}.reply_id must be a positive integer")
        if not _stamp(value["replied_at"]):
            raise Invalid(f"{path}.replied_at must be an ISO-8601 timestamp")
    return _checked(value, REPLY_ITEM_KEYS, REPLY_DELETED_REASONS, path, own)


def validate_files(value, answered_at: str) -> list[dict]:
    """The list the bridge is about to write for the answer made at `answered_at`: the items cleaned, in order.
    Invalid (a field path first, `answer_files[2].bytes ...`) for a list of more than MAX_FILES items, a duplicate id,
    or a bad item."""
    if not isinstance(value, list):
        raise Invalid(f"{FIELD} must be a list")
    if len(value) > MAX_FILES:
        raise Invalid(f"{FIELD} has {len(value)} files, max {MAX_FILES}")
    items, seen = [], set()
    for index, raw in enumerate(value):
        one = _item(raw, answered_at, index)
        if one["id"] in seen:
            raise Invalid(f"{FIELD}[{index}].id {one['id']} is used twice")
        seen.add(one["id"])
        items.append(one)
    return items


def clean_files(value, answered_at, status) -> list[dict]:
    """The ask's answer_files as the bridge reads it: [] on an ask that is not answered or closed (an older app
    reopened it and kept the list), [] for anything that isn't a list, and per item: dropped when it is malformed,
    belongs to another answer (its answered_at is not the ask's), repeats an id, or comes beyond MAX_FILES. A closed
    ask's list is as stored."""
    if status not in ("answered", "closed") or not isinstance(answered_at, str) or not isinstance(value, list):
        return []
    items, seen = [], set()
    for index, raw in enumerate(value):
        try:
            one = _item(raw, answered_at, index)
        except Invalid:
            continue
        if one["id"] in seen or len(items) >= MAX_FILES:
            continue
        seen.add(one["id"])
        items.append(one)
    return items


def _replies(events) -> set[tuple[int, str]]:
    """(id, at) of every `replied` event of the user (actor denis) among a task's events; junk is no event."""
    if not isinstance(events, list):
        return set()
    return {(e["id"], e["at"]) for e in events
            if isinstance(e, dict) and e.get("kind") == "replied" and e.get("actor") == "denis"
            and isinstance(e.get("id"), int) and not isinstance(e.get("id"), bool) and isinstance(e.get("at"), str)}


def clean_reply_files(value, events) -> list[dict]:
    """A task's reply_files as the bridge reads it (SPEC 8.1 «Which items count»), checked against the task file's own
    `events`: [] for anything that isn't a list (an older task has none), and per item: dropped when it is malformed,
    when its (reply_id, replied_at) is not the (id, at) of a `replied` event of the user, when it repeats an id, when
    it would be the seventh kept of its reply (MAX_FILES), or after MAX_TASK_FILES kept ones. A reply is never
    replaced, so no later reply makes an item stale, and the task's status never empties the list: on a finished task
    it is as stored (the route gives no download then)."""
    if not isinstance(value, list):
        return []
    replies = _replies(events)
    items, seen, per_reply = [], set(), Counter()
    for index, raw in enumerate(value):
        try:
            one = _reply_item(raw, index)
        except Invalid:
            continue
        if (one["reply_id"], one["replied_at"]) not in replies:
            continue
        if one["id"] in seen or per_reply[one["reply_id"]] >= MAX_FILES or len(items) >= MAX_TASK_FILES:
            continue
        seen.add(one["id"])
        per_reply[one["reply_id"]] += 1
        items.append(one)
    return items


# ---------- lifetimes (SPEC 7.6: bounded by Drive's createdTime, never by a client's clock alone) ----------

def created_epoch(meta: dict) -> float | None:
    """Drive's createdTime of a file (RFC 3339) as an epoch, None if it has none or it is not a time."""
    value = meta.get("createdTime")
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def lifetime_end(since: float, created: float | None, ttl: float) -> float:
    """When something that lives `ttl` seconds from the client's stamp `since` ends. The stamps are the user's
    device's clock; Drive's `created` (None: Drive gave none, the stamp alone) bounds them. A stamp more than
    CLOCK_SLACK before `created` is a clock that is behind: Drive's time is the floor, so a file is never deleted
    early. A stamp after it can't make the end later than created + ttl + CLOCK_SLACK."""
    if created is None:
        return since + ttl
    if since < created - CLOCK_SLACK:
        since = created
    return min(since + ttl, created + ttl + CLOCK_SLACK)


# ---------- the owner agent's link ----------

class FileLinks:
    """Links to the normal files of an answer for the agent that asked (SPEC 7.4): made on each read, opened by
    GET /media/v1/<token>. A separate context and claim set from the voice links (voice.VoiceLinks), so a voice token
    and a file token open nothing of each other's. `sealer` is the bridge's Keyring (encrypt/decrypt with a context)."""

    def __init__(self, sealer, base_url: str, clock=time.time):
        self.sealer = sealer
        self.base_url = base_url
        self.clock = clock

    def url_for(self, user: str, agent: str, ask_id: int, answered_at: str, file_id: str, n: int) -> tuple[str, str]:
        """(url, url_expires_at) of a new link to file `n` of the answer made at `answered_at`, valid LINK_TTL."""
        exp = int(self.clock()) + LINK_TTL
        claims = {"user": user, "agent": agent, "ask_id": ask_id, "answered_at": answered_at, "file_id": file_id,
                  "n": n, "exp": exp}
        token = self.sealer.encrypt(json.dumps(claims, separators=(",", ":")), LINK_CONTEXT)
        return f"{self.base_url}{MEDIA_PATH}{token}", _iso(exp)

    def open(self, token) -> dict | None:
        """The seven claims of a token this bridge sealed as a file link and that has not expired; None for anything
        else (not a token, tampered, a voice or other token, a key no longer configured, expired, a claim missing,
        extra or of the wrong type)."""
        if not isinstance(token, str) or len(token) > TOKEN_MAX:
            return None
        try:
            claims = json.loads(self.sealer.decrypt(token, LINK_CONTEXT))
        except (CryptoError, ValueError):
            return None
        if not isinstance(claims, dict) or set(claims) != set(CLAIMS):
            return None
        for key, kind in CLAIMS.items():
            value = claims[key]
            if not isinstance(value, kind) or isinstance(value, bool) or value in ("", 0):
                return None
        if claims["n"] < 1 or claims["exp"] <= self.clock():
            return None
        return claims


# ---------- the claim of a secret file (SPEC 7.4 «Secret file») ----------

CLAIMS_COLLECTION = "fileclaims"
LEASE = REQUEST_TIMEOUT + 60  # seconds a held claim lives: a slow-but-alive send never outlives it
DONE_TTL = 90 * 86400  # seconds a done claim is kept (expires_at: the sweep, or Firestore's TTL on expire_time)


def claim_key(user: str, ask_id: int, file_id: str) -> str:
    """The id of the claim document: a digest, so no id or name is in the key in clear. Not keyed by `answered_at`: a
    Drive file is sent at most once, whatever version of the answer names it (a re-answer that kept the item, after a
    send whose every later write failed, must not send it again; an older held lease simply runs out)."""
    return hashlib.sha256(json.dumps([user, ask_id, file_id]).encode("utf-8")).hexdigest()


class FileClaims:
    """Claims of secret files in the metadata store, collection `fileclaims`, changed only in store transactions (a
    Firestore transaction in production, a lock in MemoryStore), so two requests at the same instant on two instances
    cannot both take one. A document: {user, ask_id, state: "held" | "done", until, owner}. States: none (no document),
    held until `until` by `owner`, done (the file has been sent; `expires_at` 90 days on)."""

    def __init__(self, store: Store):
        self.store = store

    def take(self, key: str, user: str, ask_id: int, now: float) -> tuple[str, str | None]:
        """none -> held, or an expired held -> held (taken over): ("taken", owner id of the new lease); a held claim
        that has not expired: ("held", None); done: ("done", None)."""
        owner = secrets.token_hex(8)

        def decide(doc):
            if doc is not None and doc.get("state") == "done":
                return UNCHANGED, ("done", None)
            if doc is not None and doc.get("state") == "held" and doc.get("until", 0) > now:
                return UNCHANGED, ("held", None)
            return ({"user": user, "ask_id": ask_id, "state": "held", "until": now + LEASE, "owner": owner},
                    ("taken", owner))
        return self.store.transact(CLAIMS_COLLECTION, key, decide)

    def release(self, key: str, owner: str) -> None:
        """held -> none, only if the lease is still this owner's (a takeover's is left alone)."""
        def decide(doc):
            if doc is not None and doc.get("state") == "held" and doc.get("owner") == owner:
                return DELETE, None
            return UNCHANGED, None
        self.store.transact(CLAIMS_COLLECTION, key, decide)

    def done(self, key: str, owner: str, user: str, ask_id: int, now: float | None = None) -> None:
        """held -> done, only if the claim still exists, is held and is `owner`'s: a close, a reopen or a re-answer
        that deleted it came first (no orphan is made), a takeover's lease is not overwritten. A done claim has
        `expires_at` = now + DONE_TTL: by then the file is long gone, and the claim document goes with it."""
        stamp = (time.time() if now is None else now) + DONE_TTL

        def decide(doc):
            if doc is not None and doc.get("state") == "held" and doc.get("owner") == owner:
                return {"user": user, "ask_id": ask_id, "state": "done", "expires_at": stamp}, None
            return UNCHANGED, None
        self.store.transact(CLAIMS_COLLECTION, key, decide)

    def delete_for_ask(self, user: str, ask_id: int) -> None:
        """The claims of one ask of one user (the ask was closed or reopened)."""
        for key, doc in self.store.find(CLAIMS_COLLECTION, "ask_id", ask_id):
            if doc.get("user") == user:
                self.store.delete(CLAIMS_COLLECTION, key)
