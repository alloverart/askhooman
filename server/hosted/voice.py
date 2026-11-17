"""Voice answers on hosted asks (SPEC v5 "5. Voice answers"): what the ask's `answer_voice` may be.

The user's apps record the answer, upload the recording to the user's Drive appDataFolder (voice-<ask id>-<unix ms>.
<m4a|webm>, appProperties ask_id and kind "voice") and write `answer_voice` with the answer, in the same write. No MCP
tool or REST body sets it. The file is the user's, so the bridge never trusts it: clean_voice checks it on every read
(the shape, each field's type and caps, the transcript cleaned like every text field) and ties it to the answer it
belongs to (its answered_at must be the ask's). A bad or stale one reads as null, and the bridge's next write of the
ask drops it (records.py).

    file_id            the recording's Drive file id (letters, digits, - and _); null once it is deleted
    mime               audio/mp4 or audio/webm
    bytes              1 … 5 MB
    duration_ms        1000 … 300000
    lang               BCP-47 (ru-RU), at most 35 chars
    transcript_source  on_device (the device's recognizer made the text, then the user checked it) or none
    transcript         what the recognizer heard, at most 4000 chars; null with none
    answered_at        the ask's answered_at: the answer it belongs to
    deleted_at         null, or when the recording was deleted (exactly when file_id is null)

All pure; nothing here touches Drive or logs anything of a voice answer.
"""

import re

from ..rules import AskError, _text

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


def _count(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _stamp(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= STAMP_MAX


def clean_voice(value, answered_at) -> dict | None:
    """The ask's answer_voice as the bridge reads it: the nine fields in their order, the transcript cleaned; None for
    null, for anything that isn't a well-formed voice answer, and for one that belongs to another answer (its
    answered_at is not the ask's `answered_at`). Keys beyond the nine are left out."""
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
