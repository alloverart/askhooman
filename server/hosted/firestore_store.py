"""Store (store.py) on Google Cloud Firestore in Native mode. Imported only when ASKS_STORE=firestore, so the
single-tenant bridge never needs google-cloud-firestore (deploy/hosted/requirements.txt installs it).

Credentials: Application Default Credentials (on Cloud Run: the service's service account, which needs the
role roles/datastore.user). Documents whose `expires_at` (epoch seconds) is set also get `expire_time`
(a timestamp), so a Firestore TTL policy on `expire_time` can delete stale codes, requests and tokens.
"""

from datetime import datetime, timezone

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .store import DELETE, UNCHANGED


def _with_ttl(data: dict) -> dict:
    expires = data.get("expires_at")
    if isinstance(expires, (int, float)) and "expire_time" not in data:
        data = {**data, "expire_time": datetime.fromtimestamp(expires, tz=timezone.utc)}
    return data


def _plain(data: dict | None) -> dict | None:
    if data is None:
        return None
    data.pop("expire_time", None)  # a datetime: callers only use expires_at
    return data


class FirestoreStore:
    def __init__(self, project: str | None = None, database: str = "(default)", client=None):
        self.client = client or firestore.Client(project=project, database=database)

    def _ref(self, collection: str, key: str):
        return self.client.collection(collection).document(key)

    def get(self, collection: str, key: str) -> dict | None:
        snap = self._ref(collection, key).get()
        return _plain(snap.to_dict()) if snap.exists else None

    def put(self, collection: str, key: str, data: dict) -> None:
        self._ref(collection, key).set(_with_ttl(data))

    def create(self, collection: str, key: str, data: dict) -> bool:
        try:
            self._ref(collection, key).create(_with_ttl(data))
        except gexc.AlreadyExists:
            return False
        return True

    def update(self, collection: str, key: str, fields: dict) -> bool:
        try:
            self._ref(collection, key).update(_with_ttl(fields))
        except gexc.NotFound:
            return False
        return True

    def delete(self, collection: str, key: str) -> None:
        self._ref(collection, key).delete()

    def find(self, collection: str, field: str, value) -> list[tuple[str, dict]]:
        query = self.client.collection(collection).where(filter=FieldFilter(field, "==", value))
        return [(snap.id, _plain(snap.to_dict())) for snap in query.stream()]

    def all(self, collection: str) -> dict[str, dict]:
        return {snap.id: _plain(snap.to_dict()) for snap in self.client.collection(collection).stream()}

    def transact(self, collection: str, key: str, fn):
        ref = self._ref(collection, key)

        @firestore.transactional
        def run(transaction):
            snap = ref.get(transaction=transaction)
            new, result = fn(_plain(snap.to_dict()) if snap.exists else None)
            if new is DELETE:
                transaction.delete(ref)
            elif new is not UNCHANGED:
                transaction.set(ref, _with_ttl(new))
            return result

        return run(self.client.transaction())
