"""Metadata store of the hosted bridge: a few document collections with atomic read-modify-write.

Production: Firestore (firestore_store.py). Tests and local development: MemoryStore below. Everything the
hosted bridge keeps outside the users' Drives goes through this interface (accounts.py builds on it):

  users/{google sub}        email, encrypted Google refresh token, id counters
  agents/{agent id}         user, name, slug, last client, last seen, webhook, revoked_at, look (colour, icon)
  clients/{client id}       dynamically registered OAuth clients (hashed secret)
  requests/{sha256(id)}     pending authorizations (Google sign-in + consent), short-lived
  codes/{sha256(code)}      authorization codes, short-lived
  tokens/{sha256(token)}    access and refresh tokens (only hashes are stored)
  keys/{sha256(key)}        agent keys
  uploads/{sha256(token)}   one-time upload tokens for pictures and video in asks (uploads.py), 15 minutes

Documents are plain dicts of JSON-like values. `expires_at` (epoch seconds) is checked by the readers; Firestore
can also delete such documents itself through a TTL policy on `expire_time` (see deploy/hosted/README.md).
"""

import copy
import threading
from typing import Any, Callable, Protocol, TypeVar

T = TypeVar("T")


class _Unchanged:
    def __repr__(self) -> str:
        return "UNCHANGED"


UNCHANGED: Any = _Unchanged()  # returned by a transact() function that writes nothing
DELETE: Any = object()  # returned by a transact() function to delete the document


class Store(Protocol):
    def get(self, collection: str, key: str) -> dict | None: ...

    def put(self, collection: str, key: str, data: dict) -> None: ...

    def create(self, collection: str, key: str, data: dict) -> bool:
        """Write only if the document doesn't exist yet; False if it does."""

    def update(self, collection: str, key: str, fields: dict) -> bool:
        """Merge fields into an existing document; False if there is none."""

    def delete(self, collection: str, key: str) -> None: ...

    def find(self, collection: str, field: str, value) -> list[tuple[str, dict]]:
        """(key, document) of every document whose top-level `field` equals `value`."""

    def all(self, collection: str) -> dict[str, dict]:
        """Every document of a collection by key (the operator's stats read users and agents this way)."""

    def transact(self, collection: str, key: str, fn: Callable[[dict | None], tuple[Any, T]]) -> T:
        """Atomically: fn(current document or None) -> (new document | UNCHANGED | DELETE, result); returns
        result. fn may run more than once (Firestore retries contended transactions), so it must not have
        side effects; an exception raised by fn aborts without writing."""


class MemoryStore:
    """In-process Store: one lock, deep copies in and out (so callers can't mutate stored values)."""

    def __init__(self):
        self._data: dict[str, dict[str, dict]] = {}
        self._lock = threading.RLock()

    def _collection(self, name: str) -> dict[str, dict]:
        return self._data.setdefault(name, {})

    def get(self, collection: str, key: str) -> dict | None:
        with self._lock:
            doc = self._collection(collection).get(key)
            return copy.deepcopy(doc) if doc is not None else None

    def put(self, collection: str, key: str, data: dict) -> None:
        with self._lock:
            self._collection(collection)[key] = copy.deepcopy(data)

    def create(self, collection: str, key: str, data: dict) -> bool:
        with self._lock:
            docs = self._collection(collection)
            if key in docs:
                return False
            docs[key] = copy.deepcopy(data)
            return True

    def update(self, collection: str, key: str, fields: dict) -> bool:
        with self._lock:
            doc = self._collection(collection).get(key)
            if doc is None:
                return False
            doc.update(copy.deepcopy(fields))
            return True

    def delete(self, collection: str, key: str) -> None:
        with self._lock:
            self._collection(collection).pop(key, None)

    def find(self, collection: str, field: str, value) -> list[tuple[str, dict]]:
        with self._lock:
            return [(key, copy.deepcopy(doc)) for key, doc in self._collection(collection).items()
                    if doc.get(field) == value]

    def transact(self, collection: str, key: str, fn):
        with self._lock:
            docs = self._collection(collection)
            current = docs.get(key)
            new, result = fn(copy.deepcopy(current) if current is not None else None)
            if new is DELETE:
                docs.pop(key, None)
            elif new is not UNCHANGED:
                docs[key] = copy.deepcopy(new)
            return result

    def all(self, collection: str) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._collection(collection))
