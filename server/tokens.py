"""Bearer tokens: file-backed, reloaded when the file changes, compared in constant time.

File format: {"<token>": {"role": "denis"}, "<token2>": {"role": "agent", "agent": "research-bot"}, ...}
Token values are never logged. A file that stops parsing fails closed after a short grace period.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("asks.tokens")

AGENT_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
BROKEN_GRACE = 5.0  # seconds a changed-but-unparseable file keeps the previous tokens
BROKEN_LOG_EVERY = 60.0  # repeat the "no tokens accepted" error this often while the file stays broken


@dataclass(frozen=True)
class Identity:
    role: str  # "denis" or "agent"
    agent: str | None = None  # agent slug for role "agent"

    @property
    def is_denis(self) -> bool:
        return self.role == "denis"

    @property
    def actor(self) -> str:
        """Name written to closed_by / ask_events.actor."""
        return "denis" if self.is_denis else self.agent

    def as_json(self) -> dict:
        return {"role": self.role, "agent": self.agent}


def _digest(token: str) -> bytes:
    # Hashing first gives fixed-length inputs to compare_digest.
    return hashlib.sha256(token.encode("utf-8")).digest()


def parse_tokens(data: object) -> list[tuple[bytes, Identity]]:
    if not isinstance(data, dict):
        raise ValueError("tokens file must contain a JSON object")
    entries = []
    for index, (token, info) in enumerate(data.items(), start=1):
        role = info.get("role") if isinstance(info, dict) else None
        if not isinstance(token, str) or len(token) < 16:
            log.warning("tokens entry #%d skipped: token too short", index)
        elif role == "denis":
            entries.append((_digest(token), Identity("denis")))
        elif role == "agent" and isinstance(info.get("agent"), str) and AGENT_SLUG.match(info["agent"]):
            entries.append((_digest(token), Identity("agent", info["agent"])))
        else:
            log.warning("tokens entry #%d skipped: bad role/agent", index)
    return entries


class TokenStore:
    def __init__(self, path: Path | None, broken_grace: float = BROKEN_GRACE):
        self.path = path
        self.broken_grace = broken_grace
        self._lock = threading.Lock()
        self._signature = "not loaded yet"  # (mtime_ns, size, inode) of the loaded file, None if missing
        self._entries: list[tuple[bytes, Identity]] = []
        self._broken_since: float | None = None  # monotonic time the file was first seen unparseable
        self._broken_logged = 0.0
        if path is None:
            log.error("ASKS_TOKENS is not set: every authenticated request will get 401")

    def _file_signature(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def refresh(self) -> None:
        """Re-read the file if it changed since the last look (cheap: one stat call)."""
        if self.path is None:
            return
        with self._lock:
            self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        signature = self._file_signature()
        # While the file is broken it is re-read on every call, so a fix is picked up at once.
        if signature == self._signature and self._broken_since is None:
            return
        self._signature = signature
        if signature is None:
            log.warning("tokens file %s not found: no tokens accepted", self.path)
            self._entries = []
            self._broken_since = None
            return
        try:
            entries = parse_tokens(json.loads(Path(self.path).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            self._file_broken(type(exc).__name__)
            return
        self._broken_since = None
        self._entries = entries
        log.info("tokens loaded from %s: %d entries", self.path, len(entries))

    def _file_broken(self, reason: str) -> None:
        """The file changed but does not parse. Keep the previous tokens only for a short grace period
        (an editor may still be writing it), then accept none: a revocation saved with a typo must
        not leave the revoked token, and every other one, working."""
        now = time.monotonic()
        if self._broken_since is None:
            self._broken_since = now
            log.warning("tokens file %s unreadable (%s): keeping previous tokens for %.0f s",
                        self.path, reason, self.broken_grace)
            return
        if now - self._broken_since < self.broken_grace:
            return
        if self._entries or now - self._broken_logged >= BROKEN_LOG_EVERY:
            self._entries = []
            self._broken_logged = now
            log.error("tokens file %s still unreadable (%s): no tokens accepted until it is fixed",
                      self.path, reason)

    def identify(self, token: str | None) -> Identity | None:
        if not token or self.path is None:
            return None
        self.refresh()
        entries = self._entries
        given = _digest(token)
        found = None
        for digest, identity in entries:
            # No early exit: every entry is compared.
            if hmac.compare_digest(given, digest):
                found = identity
        return found


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None
