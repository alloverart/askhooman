"""Voice answers on hosted asks (SPEC v5 "5. Voice answers"): what the ask's `answer_voice` may be.

The user's apps record the answer, upload the recording to the user's Drive appDataFolder (voice-<ask id>-<unix ms>.
<m4a|webm>, appProperties ask_id and kind "voice") and write `answer_voice` with the answer, in the same write. No MCP
tool or REST body sets it. The file is the user's, so the bridge never trusts it: clean_voice checks it on every read
(the shape, each field's type and caps, the transcript cleaned like every text field) and ties it to the answer it
belongs to (its answered_at must be the ask's). The ask's status decides the rest, since an app that doesn't know
voice yet keeps the field as an unknown one when it closes or reopens: on an `answered` ask it stands; on a `closed`
one it is shown as stored but gives no link (the recording goes with the close); on an `open` one it counts as null.
A bad, stale or open-ask one reads as null, and the bridge's next write of the ask drops it (records.py).

    file_id            the recording's Drive file id (letters, digits, - and _); null once it is deleted
    mime               audio/mp4 or audio/webm
    bytes              1 … 5 MB
    duration_ms        1000 … 300000
    lang               BCP-47 (ru-RU), at most 35 chars
    transcript_source  on_device (the device's recognizer made the text, then the user checked it) or none
    transcript         what the recognizer heard, at most 4000 chars; null with none
    answered_at        the ask's answered_at: the answer it belongs to
    deleted_at         null, or when the recording was deleted (exactly when file_id is null)

What the agent gets (SPEC "What the agent gets"): for the agent that asked, and only while the ask is answered and
the recording exists, answer_voice adds `url` and `url_expires_at` (for_reader): <public URL>/media/v1/<token>,
where the token is {user, agent, ask_id, file_id, exp} sealed with the bridge's key (VoiceLinks: Keyring, AES-GCM,
context "media"), so it reveals nothing and can't be made or changed outside the bridge. It is valid for an hour and
made anew on every read; GET /media/v1/<token> (voice_api.py) sends the recording from the user's Drive. Everyone
else gets url null, and transcript null too, in answer_voice and in the history (the voice, previous_voice and
answer_voice of event payloads): the user edits the heard text before sending, so only the agent that asked sees it.

Nothing here touches Drive or logs anything of a voice answer.
"""

import json
import re
import time
from datetime import datetime, timezone

from ..rules import ANSWER_VOICE, AskError, _text
from .crypto import CryptoError, Keyring

VOICE_KEYS = ("file_id", "mime", "bytes", "duration_ms", "lang", "transcript_source", "transcript", "answered_at",
              "deleted_at")
VOICE_TYPES = {"audio/mp4": "m4a", "audio/webm": "webm"}  # the allowlist, and the file extension of each
VOICE_MAX_BYTES = 5 * 1024 * 1024
VOICE_MIN_MS, VOICE_MAX_MS = 1000, 300_000
LANG_MAX = 35
TRANSCRIPT_MAX = 4000
TRANSCRIPT_SOURCES = ("on_device", "none")
STAMP_MAX = 40  # an ISO-8601 timestamp is 24 chars
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")  # it goes into a Drive URL path: nothing that could change the request
LANG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*")
MEDIA_PATH = "/media/v1/"
LINK_CONTEXT = "media"  # the AEAD context of a link's token: no other sealed value of the bridge opens as one
LINK_TTL = 3600  # seconds
TOKEN_MAX = 2048  # chars; a real token is about 250
CLAIMS = {"user": str, "agent": str, "ask_id": int, "file_id": str, "exp": int}


def _count(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _stamp(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= STAMP_MAX


def clean_voice(value, answered_at, status) -> dict | None:
    """The ask's answer_voice as the bridge reads it: the nine fields in their order, the transcript cleaned; None for
    null, for anything that isn't a well-formed voice answer, for one that belongs to another answer (its answered_at
    is not the ask's `answered_at`), and on an ask whose `status` is not answered or closed (an older app reopened it
    and kept the field). Keys beyond the nine are left out."""
    if status not in ("answered", "closed"):
        return None
    if not isinstance(value, dict) or any(key not in value for key in VOICE_KEYS):
        return None
    file_id, deleted_at = value["file_id"], value["deleted_at"]
    if file_id is None:
        if not _stamp(deleted_at):
            return None
    elif not (isinstance(file_id, str) and FILE_ID.fullmatch(file_id) and deleted_at is None):
        return None
    if not isinstance(value["mime"], str) or value["mime"] not in VOICE_TYPES:
        return None
    if not _count(value["bytes"], 1, VOICE_MAX_BYTES) or not _count(value["duration_ms"], VOICE_MIN_MS, VOICE_MAX_MS):
        return None
    lang = value["lang"]
    if not (isinstance(lang, str) and len(lang) <= LANG_MAX and LANG.fullmatch(lang)):
        return None
    source, transcript = value["transcript_source"], value["transcript"]
    if not isinstance(source, str) or source not in TRANSCRIPT_SOURCES:
        return None
    if source == "none" and transcript is not None:
        return None
    try:
        transcript = _text(transcript, "transcript", TRANSCRIPT_MAX)
    except AskError:  # not a string, not valid Unicode, too long
        return None
    if not _stamp(value["answered_at"]) or value["answered_at"] != answered_at:
        return None
    return {**{key: value[key] for key in VOICE_KEYS}, "transcript": transcript}


# ---------- the owner agent's link ----------

def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class VoiceLinks:
    """Links to voice recordings for the agent that asked: made on each read, opened by GET /media/v1/<token>."""

    def __init__(self, keyring: Keyring, public_url: str, clock=time.time):
        self.keyring = keyring
        self.public_url = public_url
        self.clock = clock

    def link(self, user: str, agent_id: str, ask_id: int, file_id: str) -> tuple[str, str]:
        """(url, url_expires_at) of a new link, valid LINK_TTL from now."""
        exp = int(self.clock()) + LINK_TTL
        sealed = {"user": user, "agent": agent_id, "ask_id": ask_id, "file_id": file_id, "exp": exp}
        token = self.keyring.encrypt(json.dumps(sealed, separators=(",", ":")), LINK_CONTEXT)
        return f"{self.public_url}{MEDIA_PATH}{token}", _iso(exp)

    def open(self, token) -> dict | None:
        """{user, agent, ask_id, file_id, exp} of a token this bridge sealed and that has not expired; None for
        anything else (not a token, tampered, sealed with a key no longer configured, expired)."""
        if not isinstance(token, str) or len(token) > TOKEN_MAX:
            return None
        try:
            sealed = json.loads(self.keyring.decrypt(token, LINK_CONTEXT))
        except (CryptoError, ValueError):
            return None
        if not isinstance(sealed, dict) or set(sealed) != set(CLAIMS):
            return None
        for key, kind in CLAIMS.items():
            if not isinstance(sealed[key], kind) or isinstance(sealed[key], bool) or sealed[key] in ("", 0):
                return None
        if sealed["exp"] <= self.clock():
            return None
        return sealed


VOICE_PARTS = ("voice", "previous_voice", ANSWER_VOICE)  # where event payloads carry a voice answer


FILE_PARTS = ("files", "previous_files", "answer_files")  # where event payloads carry answer_files items


AGENT_FILE_KEYS = ("id", "name", "mime", "bytes", "secret", "downloaded_at", "deleted_at", "deleted_reason",
                   "blocked_at")  # SPEC 7.4: what an agent sees of a file; file_id, uploaded_at, answered_at stay stored


def _agent_items(items, named: bool = True):
    """Files as an agent sees them (SPEC 7.4): only AGENT_FILE_KEYS, so the Drive file id never leaves the bridge;
    another agent gets no name and no mime, as the type says what the file is."""
    shown = []
    for item in items:
        if isinstance(item, dict):
            item = {key: item[key] for key in AGENT_FILE_KEYS if key in item}
            if not named:
                item = {**item, "name": None, "mime": None}
        shown.append(item)
    return shown


def _unheard(event, owner: bool = False):
    """An event as an agent sees it: the files in its payload as _agent_items; for another agent also the voice
    answers without their transcript and the files without name and mime."""
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return event
    hidden = {} if owner else {key: {**payload[key], "transcript": None} for key in VOICE_PARTS
                               if isinstance(payload.get(key), dict)}
    hidden.update({key: _agent_items(payload[key], owner) for key in FILE_PARTS if isinstance(payload.get(key), list)})
    return {**event, "payload": {**payload, **hidden}} if hidden else event


def _file_view(ask: dict, items: list, reader, owner: bool, file_urls: bool, file_links) -> list:
    """answer_files as `reader` gets it (_agent_items). The owner, with `file_urls` and a `file_links`, on an answered
    ask: a normal file that exists and is not blocked also gets `url` and `url_expires_at`. A secret file never has a
    url (the agent fetches it through the bearer route). Without `file_urls` (the webhook) no item has a url and a
    secret file is only {id, secret: true}."""
    if not owner:
        return _agent_items(items, False)
    if not file_urls:
        return [{"id": item.get("id"), "secret": True} if isinstance(item, dict) and item.get("secret") is True
                else item for item in _agent_items(items)]
    shown = _agent_items(items)
    if file_links is None or ask.get("status") != "answered":
        return shown
    for raw, item in zip(items, shown):
        if (isinstance(raw, dict) and raw.get("secret") is False and raw.get("file_id") is not None
                and raw.get("blocked_at") is None):
            item["url"], item["url_expires_at"] = file_links.url_for(reader.user, reader.agent_id, ask["id"],
                                                                     ask["answered_at"], raw["file_id"], raw["id"])
    return shown


def for_reader(ask: dict, links: VoiceLinks, reader, file_urls: bool = True, file_links=None) -> dict:
    """The ask as `reader` gets it. For the agent that asked (`reader` a HostedIdentity of that agent): answer_voice,
    when there is one, with url and url_expires_at, a link while the ask is answered and the recording exists (a
    closed ask's never has one, whatever an older app left in it); answer_files with a link on each normal file
    (files.FileLinks, only when `file_links` is given and `file_urls` is true; the webhook passes file_urls=False and
    gets no url and a secret file as {id, secret: true}). For everyone else: url null, and transcript null in
    answer_voice and in the history, no name and no mime in the files."""
    owner = bool(getattr(reader, "role", None) == "agent" and reader.agent == ask.get("agent")
                 and getattr(reader, "user", "") and getattr(reader, "agent_id", ""))
    shown = ask
    voice = ask.get(ANSWER_VOICE)
    if isinstance(voice, dict):
        url = expires = None
        if owner and ask.get("status") == "answered" and voice["file_id"] is not None:
            url, expires = links.link(reader.user, reader.agent_id, ask["id"], voice["file_id"])
        voice = {**voice, "url": url, "url_expires_at": expires}
        if not owner:
            voice["transcript"] = None
        shown = {**shown, ANSWER_VOICE: voice}
    if isinstance(ask.get("answer_files"), list):
        shown = {**shown, "answer_files": _file_view(ask, ask["answer_files"], reader, owner, file_urls, file_links)}
    if isinstance(ask.get("events"), list):
        shown = {**shown, "events": [_unheard(event, owner) for event in ask["events"]]}
    return shown
