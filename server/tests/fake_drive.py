"""In-memory Google Drive v3 (appDataFolder only) + OAuth token endpoint behind httpx.MockTransport.

Implements just what server/drive.py uses: token refresh, files.list (q, paging), files.get (metadata
and alt=media), multipart create (JSON or binary content) and multipart PATCH, resumable uploads (a session POST
with the metadata, then one PUT of all the bytes to the session's Location) and files.delete. `name contains 'x'`
matches names that start with x, as Drive's prefix matching of name terms does (so 'ask-' does not find task-1.json).
files.get alt=media streams the body in 64 KiB pieces (never one big copy) and honours `Range: bytes=N-` and
`bytes=N-M` (206 with Content-Range; an end past the file is clamped to its last byte). Every request is recorded
(`requests`, and `headers` for the request headers); `down` simulates an outage, `fail_next` refuses one chosen request.
`app_action` plays the Swift app: a read-modify-write of a file as the human (SPEC "Who writes what");
`app_task_reply` and `app_task_reply_files` play the apps' replies on a task, without and with files (SPEC 8.2).
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
from server.task_rules import PRESETS

CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-client-secret"
REFRESH_TOKEN = "1//fake-refresh-token-SECRET"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_multipart(request: httpx.Request) -> tuple[dict, bytes, str]:
    """A multipart/related upload -> (metadata, content, the content part's type). The metadata part is JSON; the
    content is JSON (ask and task files) or any bytes (media), taken whole up to the closing delimiter, so bytes that
    look like CR LF or a boundary don't cut it."""
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/related; boundary="), content_type
    delimiter = b"--" + content_type.split("boundary=", 1)[1].encode()
    body = request.content
    assert int(request.headers["content-length"]) == len(body), request.headers
    opening, closing = delimiter + b"\r\n", b"\r\n" + delimiter + b"--\r\n"
    assert body.startswith(opening) and body.endswith(closing), body[:200]
    metadata_part, between, content_part = body[len(opening):-len(closing)].partition(b"\r\n" + delimiter + b"\r\n")
    assert between, "one metadata part and one content part"
    head, _, metadata = metadata_part.partition(b"\r\n\r\n")
    assert head == b"Content-Type: application/json; charset=UTF-8", head
    head, _, content = content_part.partition(b"\r\n\r\n")
    assert head.startswith(b"Content-Type: ") and b"\r\n" not in head, head
    return json.loads(metadata), content, head.removeprefix(b"Content-Type: ").decode()


class _Pieces(httpx.SyncByteStream):
    """A response body made of 64 KiB slices of `content`, copied one at a time."""

    def __init__(self, content: memoryview):
        self.content = content

    def __iter__(self):
        for at in range(0, len(self.content), 65536):
            yield bytes(self.content[at:at + 65536])


class FakeDrive:
    def __init__(self, page_size: int = 1000):
        self.files: dict[str, dict] = {}  # id -> {id, name, mimeType, parents, appProperties, modifiedTime, content, trashed}
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, query params)
        self.headers: list[dict] = []  # the request headers of each recorded request, same order
        self.down = False
        self.page_size = page_size
        self.valid_tokens: set[str] = set()
        self.token_calls = 0
        self.refresh_error: tuple[int, dict] | None = None
        self.sessions: dict[str, dict] = {}  # upload_id -> {metadata, mime, length, fields}: resumable uploads begun
        self.failures: list[list] = []  # [method, path_prefix, status, after, reason]: see fail_next
        self.lock = threading.RLock()
        self._ids = 0
        self._sessions = 0
        self._clock = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    # ---------- store helpers (also used by tests to play other clients) ----------

    def _tick(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def add(self, name: str, content, app_properties: dict | None = None, parents=("appDataFolder",),
            mime: str = "application/json", created: float | str | None = None) -> str:
        """`created`: Drive's createdTime, an epoch or an ISO string (default: the real now)."""
        if created is None:
            created = datetime.now(timezone.utc).timestamp()
        if not isinstance(created, str):
            created = datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z")
        with self.lock:
            self._ids += 1
            file_id = f"file{self._ids:03d}"
            raw = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
            self.files[file_id] = {"id": file_id, "name": name, "mimeType": mime,
                                   "parents": list(parents), "appProperties": dict(app_properties or {}),
                                   "modifiedTime": self._tick(), "createdTime": created, "content": raw,
                                   "trashed": False}
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

    def fail_next(self, method: str, path_prefix: str, status: int, after: int = 0, reason: str = "") -> None:
        """Refuse one request with HTTP `status` (Drive's error shape, nothing applied): the next authorized request
        with this method whose path starts with path_prefix, after letting `after` such requests through.
        `reason`: Drive's errors[0].reason (e.g. cannotDownloadAbusiveFile on a 403).
        Several failures queue up in the order they were set; a request counts only for the first one it matches."""
        with self.lock:
            self.failures.append([method, path_prefix, status, after, reason])

    def _injected(self, method: str, path: str) -> httpx.Response | None:
        for failure in self.failures:
            if failure[0] == method and path.startswith(failure[1]):
                if failure[3] > 0:
                    failure[3] -= 1
                    return None
                self.failures.remove(failure)
                status = failure[2]
                error = {"code": status, "message": f"fake: injected HTTP {status}"}
                if failure[4]:
                    error["errors"] = [{"reason": failure[4]}]
                return httpx.Response(status, json={"error": error})
        return None

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
            self.headers.append(dict(request.headers))
            if url.host == "oauth2.googleapis.com":
                return self._token(request)
            if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid_tokens:
                return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
            path = url.path
            injected = self._injected(request.method, path)
            if injected is not None:
                return injected
            if request.method == "GET" and path == "/drive/v3/files":
                return self._list(params)
            match = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
            if request.method == "GET" and match:
                return self._get(match.group(1), params, request.headers.get("range"))
            if request.method == "DELETE" and match:
                return self._delete(match.group(1))
            if request.method == "POST" and path == "/upload/drive/v3/files":
                return self._create(request, params)
            if request.method == "PUT" and path == "/upload/drive/v3/files":
                return self._finish_session(request, params)
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
                                        "nextPageToken,files(id,name,appProperties,modifiedTime,size)",
                                        "nextPageToken,files(id,name,appProperties,modifiedTime,size,createdTime)"
                                        ), params
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

    def _get(self, file_id: str, params: dict, request_range: str | None = None) -> httpx.Response:
        file = self.files.get(file_id)
        if file is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"File not found: {file_id}."}})
        if params.get("alt") == "media":
            return self._media(file, request_range)
        return httpx.Response(200, json=self._fields(file, params.get("fields")))

    @staticmethod
    def _media(file: dict, request_range: str | None) -> httpx.Response:
        content, headers, status = memoryview(file["content"]), {"Content-Type": file["mimeType"]}, 200
        if request_range:
            found = re.fullmatch(r"bytes=(\d+)-(\d*)", request_range)
            start = int(found.group(1))
            end = min(int(found.group(2)), len(content) - 1) if found.group(2) else len(content) - 1
            if start >= len(content) or end < start:
                return httpx.Response(416, json={"error": {"code": 416, "message": "fake: range not satisfiable"}})
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            content, status = content[start:end + 1], 206
        headers["Content-Length"] = str(len(content))
        return httpx.Response(status, headers=headers, stream=_Pieces(content))

    def _create(self, request: httpx.Request, params: dict) -> httpx.Response:
        if params.get("uploadType") == "resumable":
            return self._start_session(request, params)
        assert params.get("uploadType") == "multipart", params
        metadata, content, part_type = parse_multipart(request)
        assert metadata["parents"] == ["appDataFolder"] and metadata["mimeType"] == part_type, (metadata, part_type)
        file_id = self.add(metadata["name"], content, metadata.get("appProperties"), mime=metadata["mimeType"])
        return httpx.Response(200, json=self._fields(self.files[file_id], params.get("fields")))

    def _start_session(self, request: httpx.Request, params: dict) -> httpx.Response:
        """A resumable upload's first request: the metadata only. 200, an empty body and the session URL."""
        assert request.headers["content-type"] == "application/json; charset=UTF-8", request.headers
        metadata = json.loads(request.content)
        mime, length = request.headers["x-upload-content-type"], int(request.headers["x-upload-content-length"])
        assert metadata["parents"] == ["appDataFolder"] and metadata["mimeType"] == mime, (metadata, mime)
        self._sessions += 1
        upload_id = f"upload{self._sessions:03d}"
        self.sessions[upload_id] = {"metadata": metadata, "mime": mime, "length": length,
                                    "fields": params.get("fields")}
        location = f"https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id={upload_id}"
        return httpx.Response(200, headers={"Location": location})

    def _finish_session(self, request: httpx.Request, params: dict) -> httpx.Response:
        """All the bytes in one PUT to the session URL: the file is created, 200 + its metadata."""
        session = self.sessions.get(params.get("upload_id", ""))
        if session is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": "fake: no such upload session"}})
        content = request.content
        assert params.get("uploadType") == "resumable" and len(content) == session["length"], (params, len(content))
        assert int(request.headers["content-length"]) == len(content), request.headers
        assert request.headers.get("content-type", session["mime"]) == session["mime"], request.headers
        del self.sessions[params["upload_id"]]
        metadata = session["metadata"]
        file_id = self.add(metadata["name"], content, metadata.get("appProperties"), mime=metadata["mimeType"])
        return httpx.Response(200, json=self._fields(self.files[file_id], session["fields"]))

    def _delete(self, file_id: str) -> httpx.Response:
        """For good, skipping the trash, as files.delete does: 204, or 404 if there is no such file."""
        if self.files.pop(file_id, None) is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"File not found: {file_id}."}})
        return httpx.Response(204)

    def _update(self, file_id: str, request: httpx.Request, params: dict) -> httpx.Response:
        assert params.get("uploadType") == "multipart"
        file = self.files.get(file_id)
        if file is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"File not found: {file_id}."}})
        metadata, content, part_type = parse_multipart(request)
        assert set(metadata) <= {"appProperties"} and part_type == "application/json", (metadata, part_type)
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


def app_task_reply_files(fake: FakeDrive, task_id: int, items: list, text: str | None = None,
                         preset: str | None = None, base: dict | None = None) -> dict:
    """What the apps do for a reply with files on a hosted task (SPEC 8.2 step 3), the files being in Drive already:
    a fresh download (or a stale copy `base`); E = the highest event id + 1 and T = now; the items of `reply_files`
    that have `deleted_at` are dropped (pruning), every other one is kept untouched; each of `items` gets `reply_id`
    E and `replied_at` T; the `replied` event {id: E, task_id, at: T, actor: "denis", payload: {text, preset, files}}
    carries them as written. With no text the text is the preset's, or with neither "1 file attached" / "N files
    attached". rev+1, writer "app", appProperties unchanged except rev. Returns the content written."""
    file = fake.file(f"task-{task_id}.json")
    doc = copy.deepcopy(base) if base is not None else json.loads(file["content"])
    time.sleep(0.002)
    at = now_iso()
    event_id = max(e["id"] for e in doc["events"]) + 1
    written = [{**item, "reply_id": event_id, "replied_at": at} for item in items]
    if text is None:
        text = PRESETS[preset] if preset is not None else (
            "1 file attached" if len(written) == 1 else f"{len(written)} files attached")
    stored = doc.get("reply_files") if isinstance(doc.get("reply_files"), list) else []
    kept = [i for i in stored if not (isinstance(i, dict) and i.get("deleted_at") is not None)]
    doc["reply_files"] = [*kept, *written]
    doc["events"].append({"id": event_id, "task_id": task_id, "at": at, "actor": "denis", "kind": "replied",
                          "payload": {"text": text, "preset": preset, "files": written}})
    doc["updated_at"] = at
    doc["rev"] += 1
    doc["writer"] = "app"
    fake.write(file["id"], doc, {**file["appProperties"], "rev": str(doc["rev"])})
    return doc
