"""Pictures and video on hosted asks (SPEC v5 step 4): the attachment objects an ask carries, how one is prepared
from an agent's `ask_create` item or `ask_attach` arguments, the pure rules that add one to or remove one from an
ask, and AskMedia, which writes the files into the user's Drive appDataFolder next to the ask file.

An attach writes in this order. A read-only pre-check against the ask (owner, open, count, size, the next id, the
poster's target) refuses bad requests before any byte goes to Drive. Then the media file is written, named after the
id the item will get. Then the ask file goes through DriveCollection.mutate, whose plan (plan_attach) runs the same
rules again on the fresh file: if the ask changed meanwhile (the user answered it, another attachment took the id)
the plan refuses and the file just written is deleted. The ask write may start over on a modifiedTime change; the
media file is written once, outside that loop. A create writes every file first and the ask file last, and a
failure anywhere deletes what it wrote. Files an ask names (an old poster on replace, a detached item's files) are
deleted after the ask write, and only when Drive confirms they are this ask's media (appProperties kind "media" and
ask_id): the owner can edit their own ask file to name anything.

Ids are 1, 2, 3… per ask and never reused: the next one is 1 + the highest id in the ask's attachments or in its
attached/detached events, so an option that says "Take #2" keeps pointing at the same picture. `added_at` is when
the item's file was written (for a link card: when the card was made). Every refusal is a rules error (422/403/404/409
over REST, a tool error over MCP); Drive trouble is Unavailable (503).
"""

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from ..db import utc_now
from ..rules import (
    ID_MAX,
    LINK_TITLE_MAX,
    URL_MAX,
    AskError,
    Change,
    Conflict,
    Invalid,
    Loader,
    NotFound,
    Unavailable,
    _text,
    check_owner_open,
    plan_create,
)
from ..tokens import Identity
from .media import (
    ASK_MEDIA_MAX,
    ATTACH_MAX,
    CAPTION_MAX,
    POSTER_MAX_BYTES,
    VIDEO_TYPE,
    Checked,
    clean_link_url,
    decode_data,
    ext_of,
    provider_of,
)
from .media_fetch import CREATE_FETCH_DEADLINE, fetch_media
from .records import ATTACHMENTS, HostedAsks, UserRecords, new_hosted_record

log = logging.getLogger("asks.hosted.media")

ITEM_KEYS = frozenset(("url", "data", "link", "title", "caption", "source_url"))
POSTER_TARGETS = ("video", "link")  # a poster is a still for one of these
STORED_KINDS = ("image", "video")  # kinds with a file of their own
_MB = 1024 * 1024


@dataclass(frozen=True)
class Item:
    """One attachment ready to be written: a checked image or video (`checked`) or a link card (`url`, `title`),
    with the caption and source_url the agent gave."""
    kind: str  # image | video | link
    checked: Checked | None
    url: str | None
    title: str | None
    caption: str | None
    source_url: str | None


# ---------- preparing an item: texts, URLs, then the bytes ----------

def _string(item: dict, key: str) -> str | None:
    """A string argument, stripped; None when absent or empty."""
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f"{key} must be a string")
    return value.strip() or None


def prepare(item, http: httpx.Client | None, until: float) -> Item:
    """One item of ask_create's `attachments`, or ask_attach's arguments as one object: `{url, link?, title?,
    caption?, source_url?}` or `{data, caption?, source_url?}`, exactly one of url and data. The texts are cleaned
    like every field and the URLs checked before the bytes are fetched (`http`: the public-only client, `until`:
    when the caller's time for fetching ends) or decoded, then checked like any attachment. Invalid otherwise."""
    if hasattr(item, "model_dump"):
        item = item.model_dump()
    if not isinstance(item, dict):
        raise Invalid("an attachment must be an object with url or data")
    unknown = set(item) - ITEM_KEYS
    if unknown:
        raise Invalid(f"unknown fields: {', '.join(sorted(unknown))}")
    link = item.get("link")
    link = False if link is None else link
    if not isinstance(link, bool):
        raise Invalid("link must be true or false")
    url, data = _string(item, "url"), _string(item, "data")
    if (url is None) == (data is None):
        raise Invalid("exactly one of url and data is needed")
    if link and data is not None:
        raise Invalid("link=true needs url: a link card points at a page")
    caption = _text(item.get("caption"), "caption", CAPTION_MAX)
    source_url = _text(item.get("source_url"), "source_url", URL_MAX)
    if source_url is not None:
        source_url = clean_link_url(source_url, "source_url")
    title = _text(item.get("title"), "title", LINK_TITLE_MAX)
    if title is not None and not link:
        raise Invalid("title is for link cards (link=true); a picture or a video has a caption")
    if link:
        url = clean_link_url(url, "url")
        return Item("link", None, url, title or urlsplit(url).hostname or url, caption, source_url)
    checked = decode_data(data) if data is not None else fetch_media(http, url, until=until)
    return Item("video" if checked.mime == VIDEO_TYPE else "image", checked, None, None, caption, source_url)


def attachment_list(items) -> list:
    """The `attachments` of one ask_create as a list of at most ATTACH_MAX items, before any item is looked at
    (None: none); Invalid otherwise."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise Invalid("attachments must be a list of objects")
    if len(items) > ATTACH_MAX:
        raise Invalid(f"attachments: at most {ATTACH_MAX} items, got {len(items)}")
    return items


def prepare_all(items, http: httpx.Client | None, until: float | None = None) -> list[Item]:
    """The `attachments` of one ask_create: at most ATTACH_MAX items, prepared in order with one fetch budget
    (CREATE_FETCH_DEADLINE from now, or `until`). A bad item fails the call as `attachments[i]: …` before anything
    is written; the bytes are added up as they come, and the items after the ASK_MEDIA_MAX cap are not fetched."""
    items = attachment_list(items)
    until = time.monotonic() + CREATE_FETCH_DEADLINE if until is None else until
    prepared: list[Item] = []
    total = 0
    for i, item in enumerate(items):
        try:
            prepared.append(prepare(item, http, until))
            total += prepared[-1].checked.bytes if prepared[-1].checked else 0
            if total > ASK_MEDIA_MAX:
                raise _too_much(total)
        except Invalid as exc:
            raise Invalid(f"attachments[{i}]: {exc}") from None
    return prepared


def check_new(items: list[Item]) -> None:
    """The attachments of a new ask: the per-ask caps (count, bytes), before any file is written."""
    if len(items) > ATTACH_MAX:
        raise Invalid(f"attachments: at most {ATTACH_MAX} items, got {len(items)}")
    total = sum(item.checked.bytes for item in items if item.checked is not None)
    if total > ASK_MEDIA_MAX:
        raise _too_much(total)


def check_poster(item: Item) -> None:
    """What may become a poster (poster_for): an image of at most POSTER_MAX_BYTES, with no caption or source_url
    of its own (they belong to the attachment it is for)."""
    if item.kind == "link":
        raise Invalid("a poster needs the image's url or data, not link=true")
    if item.kind != "image":
        raise Invalid("a poster is a still image (PNG, JPEG or WebP), not a video")
    if item.checked.bytes > POSTER_MAX_BYTES:
        raise Invalid(f"the poster must be at most {POSTER_MAX_BYTES // _MB} MB (it is {item.checked.bytes} bytes)")
    if item.caption is not None or item.source_url is not None:
        raise Invalid("a poster takes no caption or source_url: they belong to the attachment it is for")


# ---------- the stored objects (SPEC "Kinds": shapes and key order) ----------

def _stored(item: Item, n: int, file_id: str | None, added_at: str) -> dict:
    if item.kind == "link":
        return {"id": n, "kind": "link", "url": item.url, "title": item.title, "provider": provider_of(item.url),
                "caption": item.caption, "poster": None, "added_at": added_at}
    checked = item.checked
    stored = {"id": n, "kind": item.kind, "mime": checked.mime, "width": checked.width, "height": checked.height}
    if item.kind == "video":
        stored["duration_ms"] = checked.duration_ms
    stored.update(bytes=checked.bytes, caption=item.caption, source_url=item.source_url, file_id=file_id)
    if item.kind == "video":
        stored["poster"] = None
    stored["added_at"] = added_at
    return stored


def _poster(checked: Checked, file_id: str | None) -> dict:
    return {"file_id": file_id, "mime": checked.mime, "width": checked.width, "height": checked.height,
            "bytes": checked.bytes}


def _too_much(total: int) -> Invalid:
    return Invalid(f"the attachments would take {total} bytes; at most {ASK_MEDIA_MAX // _MB} MB are stored per ask, "
                   "posters included: attach a bigger video as a Drive link with a poster")


# ---------- the rules (pure; the ask files are the user's, so every value is checked) ----------

def _int_id(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= ID_MAX else None


def _size(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _objects(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def next_attachment_id(ask: dict) -> int:
    """1 + the highest attachment id this ask ever had: its items now and every attached/detached event."""
    seen = [item.get("id") for item in _objects(ask.get(ATTACHMENTS))]
    for event in _objects(ask.get("events")):
        if event.get("kind") in ("attached", "detached"):
            payload = event.get("payload")
            item = payload.get("attachment") if isinstance(payload, dict) else None
            if isinstance(item, dict):
                seen.append(item.get("id"))
    return max((item_id for item_id in map(_int_id, seen) if item_id is not None), default=0) + 1


def stored_bytes(attachments: list) -> int:
    """What the attachments hold in Drive: images and videos by their size, posters included."""
    total = 0
    for item in _objects(attachments):
        if item.get("kind") in STORED_KINDS:
            total += _size(item.get("bytes"))
        poster = item.get("poster")
        if isinstance(poster, dict):
            total += _size(poster.get("bytes"))
    return total


def _index_of(ask: dict, attachments: list[dict], n) -> int:
    if _int_id(n) is not None:
        for index, item in enumerate(attachments):
            if item.get("id") == n:
                return index
    raise NotFound(f"ask #{ask['id']} has no attachment #{n}")


def plan_attach(who: Identity, item_json: dict, poster_for: int, load: Loader, now: str) -> tuple[dict, Change]:
    """Add `item_json` to the ask `load` returns (with its events): an attachment object carrying its id, or, with
    `poster_for`, a poster object for that attachment. The owner agent only, open asks only (as plan_update); the
    id must still be the next one (Conflict: another attachment took it while the file was being written); at most
    ATTACH_MAX attachments and ASK_MEDIA_MAX bytes afterwards; a poster goes on a video or a link card that exists."""
    ask = load()
    check_owner_open(who, ask)
    attachments = [dict(item) for item in _objects(ask.get(ATTACHMENTS))]
    if poster_for:
        index = _index_of(ask, attachments, poster_for)
        kind = attachments[index].get("kind")
        if kind not in POSTER_TARGETS:
            raise Invalid(f"attachment #{poster_for} is {'an' if kind == 'image' else 'a'} {kind}: a poster is a "
                          "still for a video or a link card")
        attached = {**attachments[index], "poster": item_json}
        attachments[index] = attached
    else:
        if item_json.get("id") != next_attachment_id(ask):
            raise Conflict(f"ask #{ask['id']} got another attachment while this one was being written; try again")
        if len(attachments) >= ATTACH_MAX:
            raise Invalid(f"ask #{ask['id']} already has {ATTACH_MAX} attachments: at most {ATTACH_MAX} per ask "
                          "(posters don't count); detach one first")
        attached = item_json
        attachments.append(attached)
    total = stored_bytes(attachments)
    if total > ASK_MEDIA_MAX:
        raise _too_much(total)
    return ask, Change({ATTACHMENTS: attachments, "updated_at": now}, "attached", {"attachment": attached}, now)


def plan_detach(who: Identity, n, load: Loader, now: str) -> tuple[dict, Change]:
    """Remove attachment `n` from the ask `load` returns: the owner agent only, open asks only; NotFound if the ask
    has no such attachment. The event carries the item as it was stored, poster included (its files go after)."""
    ask = load()
    check_owner_open(who, ask)
    attachments = _objects(ask.get(ATTACHMENTS))
    removed = attachments.pop(_index_of(ask, attachments, n))
    return ask, Change({ATTACHMENTS: attachments, "updated_at": now}, "detached", {"attachment": removed}, now)


def file_ids(items: list[dict]) -> list:
    """The Drive files of stored attachment objects: each item's own file and its poster's."""
    found = []
    for item in _objects(items):
        if item.get("kind") in STORED_KINDS:
            found.append(item.get("file_id"))
        poster = item.get("poster")
        if isinstance(poster, dict):
            found.append(poster.get("file_id"))
    return found


# ---------- the Drive side ----------

class AskMedia:
    """The attachments of one user's asks: files in their Drive appDataFolder next to the ask files, written before
    the ask file and deleted only by detach, a poster's replacement, or the undo of a failed write."""

    def __init__(self, records: UserRecords):
        self.records = records
        self.asks = HostedAsks(records)
        self.items = self.asks.items
        self.drive = records.drive

    # create

    def create(self, who: Identity, data: dict, items: list[Item]) -> dict:
        """A new ask with `items` (prepare_all's) as attachments 1..n: their files first, then the ask file with
        `created` and one `attached` event per item; if anything fails, the files are deleted again. An
        `attachments` key in `data` is ignored: the items come prepared."""
        fields = plan_create(who, {key: value for key, value in data.items() if key != ATTACHMENTS})
        check_new(items)
        if not items:
            return self.items.create(who, fields, new_hosted_record)
        return self.items.create(who, fields, new_hosted_record,
                                 media=lambda ask_id: self._write_all(ask_id, items),
                                 rollback=lambda written: self._delete_own(file_ids(written)))

    def _write_all(self, ask_id: int, items: list[Item]) -> list[dict]:
        written: list[dict] = []
        try:
            for n, item in enumerate(items, start=1):
                written.append(self._write_item(ask_id, n, item))
        except BaseException:
            self._delete_own(file_ids(written))
            raise
        return written

    # attach / detach

    def attach(self, who: Identity, ask_id: int, item: Item, poster_for: int = 0) -> dict:
        """Add `item` to the ask as its next attachment, or with `poster_for` as the poster of that video or link
        card (replacing an earlier one, whose file goes after the ask is written). Returns the ask."""
        if poster_for in (0, None):
            poster_for = 0
        elif _int_id(poster_for) is None:
            raise Invalid("poster_for must be the id of one of the ask's attachments (0 or omitted: a new one)")
        else:
            check_poster(item)
        ask = self.asks.get(ask_id)  # fresh: find() lists Drive, and the cache is keyed by modifiedTime
        now = utc_now()
        n = poster_for or next_attachment_id(ask)
        draft = _poster(item.checked, None) if poster_for else _stored(item, n, None, now)
        plan_attach(who, draft, poster_for, lambda: ask, now)  # the pre-check: a refusal writes nothing
        if poster_for:
            stored = self._write_poster(ask_id, n, item)
        else:
            stored = self._write_item(ask_id, n, item)
        own = [stored["file_id"]] if item.checked is not None else []
        before: dict = {}

        def plan(load):
            current, change = plan_attach(who, stored, poster_for, load, utc_now())
            before["ask"] = current
            return current, change

        try:
            result = self.items.mutate(who, ask_id, plan, with_events=True)
        except BaseException:
            self._delete_own(own)  # the ask changed under us (answered, the id taken) or Drive failed
            raise
        if poster_for:
            previous = [item for item in _objects(before["ask"].get(ATTACHMENTS)) if item.get("id") == poster_for]
            old = previous[0].get("poster") if previous and isinstance(previous[0].get("poster"), dict) else None
            if old is not None and old.get("file_id") not in own:
                self._delete_named(ask_id, [old.get("file_id")])
        return result

    def detach(self, who: Identity, ask_id: int, n: int) -> dict:
        """Remove attachment `n` and delete its files (the poster too) once the ask is written. Returns the ask."""
        removed: dict = {}

        def plan(load):
            current, change = plan_detach(who, n, load, utc_now())
            removed["item"] = change.payload["attachment"]
            return current, change

        ask = self.items.mutate(who, ask_id, plan)
        self._delete_named(ask_id, file_ids([removed["item"]]))
        return ask

    # files

    def _write_item(self, ask_id: int, n: int, item: Item) -> dict:
        if item.checked is None:
            return _stored(item, n, None, utc_now())
        name = f"media-{ask_id}-{n}.{ext_of(item.checked.mime)}"
        return _stored(item, n, self._upload(ask_id, n, name, item.checked), utc_now())

    def _write_poster(self, ask_id: int, n: int, item: Item) -> dict:
        name = f"media-{ask_id}-{n}-poster.{ext_of(item.checked.mime)}"
        return _poster(item.checked, self._upload(ask_id, n, name, item.checked))

    def _upload(self, ask_id: int, n: int, name: str, checked: Checked) -> str:
        """The file in Drive; only the id Drive returns is relied on (a resumable upload's answer may carry less)."""
        props = {"ask_id": str(ask_id), "kind": "media", "attachment": str(n)}
        meta = self.records.call(self.drive.upload_media, name, checked.raw, checked.mime, props)
        file_id = meta.get("id") if isinstance(meta, dict) else None
        if not isinstance(file_id, str) or not file_id:
            log.warning("hosted: Drive gave no id for %s of ask #%s; the file may be left behind", name, ask_id)
            raise Unavailable(f"Google Drive did not confirm the upload of {name}, try again")
        log.info("hosted: wrote %s (%d bytes) for ask #%s", name, checked.bytes, ask_id)
        return file_id

    def _delete_own(self, ids: list) -> None:
        """Files this call has just written, after a failure: deleted outright, best effort (logged)."""
        for file_id in ids:
            try:
                self.records.call(self.drive.delete, file_id)
            except AskError as exc:
                log.warning("hosted: could not delete the media file %s just written: %s", file_id, exc)

    def _delete_named(self, ask_id: int, ids: list) -> None:
        """Files an ask file names, after the ask is written (a detached item's, a replaced poster's): deleted only
        when Drive says they are this ask's media; anything else stays and is logged, since the owner can edit their
        ask file to name the ask file itself or another ask's picture. Best effort: the ask is already written."""
        for file_id in ids:
            if not isinstance(file_id, str) or not file_id:
                log.warning("hosted: ask #%s names %r as a media file: not deleted", ask_id, file_id)
                continue
            try:
                meta = self.records.call(self.drive.get_meta, file_id)
                if meta is None:
                    continue  # already gone
                props = meta.get("appProperties") or {}
                if props.get("kind") != "media" or props.get("ask_id") != str(ask_id):
                    log.warning("hosted: file %s named by ask #%s is not its media (%r): not deleted", file_id, ask_id,
                                meta.get("name"))
                    continue
                self.records.call(self.drive.delete, file_id)
            except AskError as exc:
                log.warning("hosted: could not delete media file %s of ask #%s: %s", file_id, ask_id, exc)
