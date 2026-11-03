"""In-memory Google Drive v3 (appDataFolder only) + OAuth token endpoint behind httpx.MockTransport.

Implements just what server/drive.py uses: token refresh, files.list (q, paging), files.get (metadata
and alt=media), multipart create and multipart PATCH. `name contains 'x'` matches names that start with x,
as Drive's prefix matching of name terms does (so 'ask-' does not find task-1.json). Every request is recorded; `down` simulates an
outage. `app_action` plays the Swift app: a read-modify-write of a file as the human (SPEC "Who writes what").
"""

import copy
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx

from server.drive import DriveClient

CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-client-secret"
REFRESH_TOKEN = "1//fake-refresh-token-SECRET"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_multipart(request: httpx.Request) -> tuple[dict, bytes]:
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/related; boundary="), content_type
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = request.content.split(b"--" + boundary)
    assert len(parts) == 4 and parts[0] == b"" and parts[3] == b"--\r\n", parts

    def body(part: bytes) -> bytes:
        head, _, data = part.partition(b"\r\n\r\n")
        assert b"Content-Type: application/json" in head
        assert data.endswith(b"\r\n")
        return data[:-2]

    return json.loads(body(parts[1])), body(parts[2])


class FakeDrive:
    def __init__(self, page_size: int = 1000):
        self.files: dict[str, dict] = {}  # id -> {id, name, mimeType, parents, appProperties, modifiedTime, content, trashed}
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, query params)
        self.down = False
        self.page_size = page_size
        self.valid_tokens: set[str] = set()
        self.token_calls = 0
        self.refresh_error: tuple[int, dict] | None = None
        self.lock = threading.RLock()
        self._ids = 0
        self._clock = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    # ---------- store helpers (also used by tests to play other clients) ----------

    def _tick(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def add(self, name: str, content, app_properties: dict | None = None, parents=("appDataFolder",)) -> str:
        with self.lock:
            self._ids += 1
            file_id = f"file{self._ids:03d}"
            raw = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
            self.files[file_id] = {"id": file_id, "name": name, "mimeType": "application/json",
                                   "parents": list(parents), "appProperties": dict(app_properties or {}),
                                   "modifiedTime": self._tick(), "content": raw, "trashed": False}
            return file_id

    def write(self, file_id: str, doc: dict, app_properties: dict) -> None:
        with self.lock:
            file = self.files[file_id]
            file["content"] = json.dumps(doc, ensure_ascii=False).encode()
            file["appProperties"].update(app_properties)
            file["modifiedTime"] = self._tick()

    def named(self, name: str) -> list[dict]:
        with self.lock:
            return [f for f in self.files.values()
                    if f["name"] == name and not f["trashed"] and "appDataFolder" in f["parents"]]

    def file(self, name: str) -> dict:
        found = self.named(name)
        assert len(found) == 1, f"{len(found)} files named {name}"
        return found[0]

    def doc(self, ask_id: int) -> dict:
        return json.loads(self.file(f"ask-{ask_id}.json")["content"])

    def task_doc(self, task_id: int) -> dict:
        return json.loads(self.file(f"task-{task_id}.json")["content"])

    def calls(self, method: str | None = None, path_prefix: str = "") -> list[tuple[str, str, dict]]:
        with self.lock:
            return [r for r in self.requests if (method is None or r[0] == method) and r[1].startswith(path_prefix)]

    # ---------- HTTP ----------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if self.down:
                raise httpx.ConnectError("simulated Drive outage", request=request)
            url = request.url
            params = dict(url.params)
            self.requests.append((request.method, url.path, params))
            if url.host == "oauth2.googleapis.com":
                return self._token(request)
            if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid_tokens:
                return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
            path = url.path
            if request.method == "GET" and path == "/drive/v3/files":
                return self._list(params)
            match = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
            if request.method == "GET" and match:
                return self._get(match.group(1), params)
            if request.method == "POST" and path == "/upload/drive/v3/files":
                return self._create(request, params)
            match = re.fullmatch(r"/upload/drive/v3/files/([^/]+)", path)
            if request.method == "PATCH" and match:
                return self._update(match.group(1), request, params)
            return httpx.Response(400, json={"error": {"message": f"fake: unsupported {request.method} {path}"}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form == {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                        "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token"}, form
        if self.refresh_error:
            return httpx.Response(self.refresh_error[0], json=self.refresh_error[1])
        self.token_calls += 1
        token = f"ya29.access-{self.token_calls}"
        self.valid_tokens.add(token)
        return httpx.Response(200, json={"access_token": token, "expires_in": 3599, "token_type": "Bearer",
                                         "scope": "https://www.googleapis.com/auth/drive.appdata"})

    @staticmethod
    def _fields(file: dict, fields: str | None) -> dict:
        keys = re.findall(r"\w+", fields or "id,name")
        found = {k: file[k] for k in keys if k in file and k != "content"}
        if "size" in keys:  # Drive reports int64 values as strings
            found["size"] = str(len(file["content"]))
        return found

    def _list(self, params: dict) -> httpx.Response:
        assert params.get("spaces") == "appDataFolder", params
        assert params.get("fields") in ("nextPageToken,files(id,name,appProperties,modifiedTime)",  # single-tenant
                                        "nextPageToken,files(id,name,appProperties,modifiedTime,size)"), params
        query = params.get("q", "")
        exact = re.fullmatch(r"name = '([^']*)' and trashed = false", query)
        terms = re.findall(r"name contains '([^']*)'", query)
        if exact:
            def matches(name):
                return name == exact.group(1)
        else:
            either = " or ".join(f"name contains '{t}'" for t in terms)
            assert terms and query in (f"{either} and trashed = false", f"({either}) and trashed = false"), query

            def matches(name):
                return name.startswith(tuple(terms))
        found = [f for f in self.files.values()
                 if matches(f["name"]) and not f["trashed"] and "appDataFolder" in f["parents"]]
        size = min(int(params.get("pageSize", 100)), self.page_size)
        start = int(params.get("pageToken", 0))
        page = found[start:start + size]
        body = {"files": [self._fields(f, params["fields"][len("nextPageToken,files("):-1]) for f in page]}
        if start + size < len(found):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    def _get(self, file_id: str, params: dict) -> httpx.Response:
        file = self.files.get(file_id)
        if file is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"File not found: {file_id}."}})
        if params.get("alt") == "media":
            return httpx.Response(200, content=file["content"], headers={"Content-Type": "application/json"})
        return httpx.Response(200, json=self._fields(file, params.get("fields")))

    def _create(self, request: httpx.Request, params: dict) -> httpx.Response:
        assert params.get("uploadType") == "multipart"
        metadata, content = parse_multipart(request)
        assert metadata["parents"] == ["appDataFolder"] and metadata["mimeType"] == "application/json", metadata
        file_id = self.add(metadata["name"], content, metadata.get("appProperties"))
        return httpx.Response(200, json=self._fields(self.files[file_id], params.get("fields")))

    def _update(self, file_id: str, request: httpx.Request, params: dict) -> httpx.Response:
        assert params.get("uploadType") == "multipart"
        file = self.files.get(file_id)
        if file is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"File not found: {file_id}."}})
        metadata, content = parse_multipart(request)
        assert set(metadata) <= {"appProperties"}, metadata
        file["content"] = content
        file["appProperties"].update(metadata.get("appProperties") or {})
        file["modifiedTime"] = self._tick()
        return httpx.Response(200, json=self._fields(file, params.get("fields")))


def make_client(fake: FakeDrive, client_file, token_file) -> DriveClient:
    return DriveClient(client_file, token_file, transport=fake.transport())


def app_action(fake: FakeDrive, ask_id: int, kind: str, answer: str | None = None, resolution: str | None = None,
               base: dict | None = None, extra: dict | None = None) -> dict:
    """What the Swift app does for the human: download the file (or use a stale copy `base`), apply the state
    machine, append a denis event with id max+1, rev+1, writer "app", upload content + appProperties."""
    file = fake.file(f"ask-{ask_id}.json")
    doc = copy.deepcopy(base) if base is not None else json.loads(file["content"])
    time.sleep(0.002)  # the app's clock: a moment after the bridge's last event
    at = now_iso()
    status = doc["status"]
    if kind == "answered":
        assert status in ("open", "answered")
        payload = {"answer": answer, "previous_answer": doc["answer"]}
        doc.update(status="answered", answer=answer, answered_at=at)
    elif kind == "closed":
        assert status in ("open", "answered")
        if status == "open" and not resolution:
            resolution = "Not relevant (Denis)"
        payload = {"resolution": resolution, "previous_status": status}
        doc.update(status="closed", resolution=resolution, closed_by="denis", closed_at=at)
    else:
        assert kind == "reopened" and status in ("closed", "answered")
        cleared = ("answer", "answered_at", "resolution", "closed_by", "closed_at")
        payload = {"previous_status": status, **{field: doc[field] for field in cleared}}
        doc.update(status="open", **{field: None for field in cleared})
    doc["updated_at"] = at
    doc["events"].append({"id": max(e["id"] for e in doc["events"]) + 1, "ask_id": ask_id, "at": at,
                          "actor": "denis", "kind": kind, "payload": payload})
    doc["rev"] += 1
    doc["writer"] = "app"
    doc.update(extra or {})
    fake.write(file["id"], doc, {"ask_id": str(ask_id), "status": doc["status"], "agent": doc["agent"],
                                 "rev": str(doc["rev"])})
    return doc


def app_task_reply(fake: FakeDrive, task_id: int, text: str, preset: str | None = None,
                   base: dict | None = None) -> dict:
    """What the apps do for a reply on a task (SPEC v5): fresh download (or a stale copy `base`), a denis
    `replied` event with id max+1, rev+1, writer "app"; appProperties unchanged except rev."""
    file = fake.file(f"task-{task_id}.json")
    doc = copy.deepcopy(base) if base is not None else json.loads(file["content"])
    time.sleep(0.002)
    at = now_iso()
    doc["events"].append({"id": max(e["id"] for e in doc["events"]) + 1, "task_id": task_id, "at": at,
                          "actor": "denis", "kind": "replied", "payload": {"text": text, "preset": preset}})
    doc["updated_at"] = at
    doc["rev"] += 1
    doc["writer"] = "app"
    fake.write(file["id"], doc, {**file["appProperties"], "rev": str(doc["rev"])})
    return doc
