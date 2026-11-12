"""One-time uploads (SPEC v5 step 4, "How the bytes get in"): a file on the agent's disk reaches an ask through its
shell, not through the model's output. ask_upload (or POST /api/asks/{id}/uploads) issues an upload token, the agent
sends the file with curl to PUT /api/uploads, and the bridge attaches it like any other attachment.

An upload token is `qtu_` + 43 URL-safe characters (secrets.token_urlsafe(32)), sent in the Authorization header
(never in the URL, so request logs don't keep it). Only its SHA-256 is stored, in `uploads/{digest}`, with what it
is bound to: the user, the agent, the ask, the type, the size, caption, source_url and poster_for. It lives 15
minutes and is used once: PUT /api/uploads claims it (deletes it in a transaction) before anything else, whatever
the outcome. It is valid on that route only, and that route takes nothing else (hosted/app.py). Delete Account
deletes the user's tokens with the rest (accounts.delete_user); a token of an agent that was disconnected meanwhile
is refused when it is claimed.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .accounts import HostedIdentity
from .crypto import digest, new_secret
from .store import DELETE, UNCHANGED, Store

UPLOADS = "uploads"  # the store collection
UPLOAD_PATH = "/api/uploads"
UPLOAD_PREFIX = "qtu_"
UPLOAD_FORMAT = re.compile(r"^qtu_[A-Za-z0-9_-]{43}$")  # new_secret(UPLOAD_PREFIX): 32 random bytes
UPLOAD_TTL = 15 * 60  # seconds


@dataclass(frozen=True)
class UploadGrant:
    """What a claimed upload token allows: one PUT of exactly `bytes` bytes of type `mime` onto ask `ask_id` of
    `user`, by agent `agent_id` (slug `agent`). Set as the request's identity by the bearer check, so RequestGuard
    caps the body at `max_body` instead of the 256 KB of every other route."""
    user: str
    agent_id: str
    agent: str
    ask_id: int
    mime: str
    bytes: int
    caption: str | None
    source_url: str | None
    poster_for: int
    expires_at: float

    @property
    def max_body(self) -> int:
        return self.bytes

    @property
    def identity(self) -> HostedIdentity:
        """The agent the upload is for, as the attachment rules see it (owner, open ask)."""
        return HostedIdentity("agent", self.agent, user=self.user, agent_id=self.agent_id, via="upload")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _grant(doc: dict) -> UploadGrant | None:
    """The grant a stored document describes, or None if it is not one (the store is ours, but check anyway)."""
    try:
        grant = UploadGrant(**{key: doc[key] for key in UploadGrant.__dataclass_fields__})
    except (KeyError, TypeError):
        return None
    ints_ok = all(isinstance(v, int) and not isinstance(v, bool) for v in (grant.ask_id, grant.bytes, grant.poster_for))
    strings_ok = all(isinstance(v, str) and v for v in (grant.user, grant.agent_id, grant.agent, grant.mime))
    if not (ints_ok and strings_ok and grant.bytes > 0 and isinstance(grant.expires_at, (int, float))):
        return None
    return grant


class UploadGrants:
    """Upload tokens in the metadata store (collection `uploads`)."""

    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.clock = clock

    def issue(self, who: HostedIdentity, ask_id: int, mime: str, bytes: int, caption: str | None,  # noqa: A002
              source_url: str | None, poster_for: int) -> tuple[str, str]:
        """A new token for one upload onto `ask_id` by `who` (arguments already checked) -> (token, ISO expiry)."""
        token = new_secret(UPLOAD_PREFIX)
        expires_at = self.clock() + UPLOAD_TTL
        self.store.put(UPLOADS, digest(token), {
            "user": who.user, "agent_id": who.agent_id, "agent": who.agent, "ask_id": ask_id, "mime": mime,
            "bytes": bytes, "caption": caption, "source_url": source_url, "poster_for": poster_for,
            "expires_at": expires_at})
        return token, _iso(expires_at)

    def claim(self, token: str) -> UploadGrant | None:
        """Take the token (once: it is deleted in the same transaction that reads it); None if it is malformed,
        unknown, already used or expired (an expired one is deleted all the same)."""
        if not isinstance(token, str) or not UPLOAD_FORMAT.match(token):
            return None

        def take(doc):
            return (DELETE, doc) if doc is not None else (UNCHANGED, None)
        doc = self.store.transact(UPLOADS, digest(token), take)
        if doc is None or not isinstance(doc.get("expires_at"), (int, float)) or doc["expires_at"] <= self.clock():
            return None
        return _grant(doc)
