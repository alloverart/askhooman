"""Minimal Google Drive v3 REST client for the bridge's mirror in the appDataFolder (SPEC.md, "Drive storage").

Only what the mirror needs: refresh the access token, list / read / create / update JSON files in
appDataFolder, and for the hosted bridge write media files (pictures, video) there and delete files.
Credentials are read from their files at refresh time, so re-linking the account
(writing a new refresh token file) needs no restart. No token or secret ever appears in an error message or log.
"""

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
UPLOAD_SESSION = tuple(urlsplit(f"{UPLOAD_URL}/files")[:3])  # (scheme, netloc, path) a resumable session URL must have
SCOPE = "https://www.googleapis.com/auth/drive.appdata"
FILE_FIELDS = "id,name,appProperties,modifiedTime"
LIST_QUERY = "(name contains 'ask-' or name contains 'task-') and trashed = false"  # ask-<id>.json, task-<id>.json
MULTIPART_MAX = 5 * 1024 * 1024  # bytes: a media file up to this goes up in one multipart request, larger resumable
REFRESH_MARGIN = 60.0  # refresh the access token this many seconds before Google says it expires
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


class DriveError(Exception):
    """A failed Drive call. transient: Drive is unreachable or overloaded (network error, 5xx, 429),
    so retrying the whole sync later is the cure; otherwise the request itself was refused."""

    def __init__(self, message: str, transient: bool = False, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.reason = reason  # e.g. "invalid_grant" from a refused token refresh


class DriveNotFound(DriveError):
    """404: the file is gone."""


class DriveBadFile(DriveError):
    """The file's content is not a JSON object."""


def read_client(path: Path | None) -> tuple[str, str]:
    """client_id and client_secret of the TV/limited-input OAuth client. Accepts a flat
    {"client_id", "client_secret"} file or the JSON Google Cloud console downloads ({"installed": {...}})."""
    if path is None:
        raise DriveError("ASKS_DRIVE_CLIENT is not set")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DriveError(f"cannot read OAuth client file {path}: {exc.strerror}") from None
    except ValueError:
        raise DriveError(f"OAuth client file {path} is not valid JSON") from None
    if isinstance(data, dict):
        data = data.get("installed") or data.get("web") or data
    client_id = data.get("client_id") if isinstance(data, dict) else None
    client_secret = data.get("client_secret") if isinstance(data, dict) else None
    if not isinstance(client_id, str) or not client_id or not isinstance(client_secret, str) or not client_secret:
        raise DriveError(f"OAuth client file {path} has no client_id/client_secret")
    return client_id, client_secret


def read_refresh_token(path: Path | None) -> str:
    if path is None:
        raise DriveError("ASKS_DRIVE_TOKEN is not set")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DriveError(f"no Drive token at {path}: link the Google account and save its refresh token there") from None
    except OSError as exc:
        raise DriveError(f"cannot read Drive token file {path}: {exc.strerror}") from None
    except ValueError:
        raise DriveError(f"Drive token file {path} is not valid JSON") from None
    token = data.get("refresh_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise DriveError(f"Drive token file {path} has no refresh_token: link the Google account again")
    return token


def _quote(value: str) -> str:
    """A string literal for a Drive query."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


RELINK_HINT = "the link was revoked or expired: link the Google account again and replace the token file"


class DriveClient:
    def __init__(self, client_file: Path | None, token_file: Path | None, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None, *,
                 credentials: Callable[[], tuple[str, str, str]] | None = None,
                 http: httpx.Client | None = None, relink_hint: str = RELINK_HINT,
                 file_fields: str = FILE_FIELDS, max_file_bytes: int | None = None, max_files: int | None = None):
        """Credentials come from client_file + token_file, or (hosted mode, one client per user) from
        `credentials()` -> (client_id, client_secret, refresh_token), called at each token refresh.
        `http`: a shared HTTP client (not closed by close()); otherwise the client owns one.
        Hosted mode also bounds what it reads from users' Drives: `file_fields` adds `size`, downloads are read
        raw (no decompression) up to `max_file_bytes`, listings stop after `max_files` files."""
        self.client_file = client_file
        self.token_file = token_file
        self._credentials = credentials or self._file_credentials
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout, transport=transport)
        self.relink_hint = relink_hint
        self.file_fields = file_fields
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.truncated = False  # the last listing stopped at max_files
        self._lock = threading.Lock()
        self._access: str | None = None
        self._expires = 0.0
        self._secrets: tuple[str, ...] = ()  # values scrubbed from every error message

    # ---------- auth ----------

    def _file_credentials(self) -> tuple[str, str, str]:
        client_id, client_secret = read_client(self.client_file)
        return client_id, client_secret, read_refresh_token(self.token_file)

    def config_problem(self) -> str | None:
        """Why every call would fail without even trying the network (missing/broken files), or None."""
        try:
            self._credentials()
        except DriveError as exc:
            return str(exc)
        return None

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    def _token(self) -> str:
        with self._lock:
            if self._access is None or time.monotonic() >= self._expires:
                self._refresh()
            return self._access

    def _refresh(self) -> None:
        client_id, client_secret, refresh_token = self._credentials()
        self._secrets = (refresh_token, client_secret, self._access or "")
        data = {"client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh_token, "grant_type": "refresh_token"}
        try:
            response = self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise DriveError(self._scrub(f"token refresh failed: {type(exc).__name__}: {exc}"), transient=True) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            error = str(body.get("error", "")) if isinstance(body, dict) else ""
            transient = response.status_code >= 500 or response.status_code == 429
            hint = f": {self.relink_hint}" if error == "invalid_grant" else ""
            raise DriveError(self._scrub(f"token refresh refused (HTTP {response.status_code} {error}){hint}"),
                             transient=transient, status=response.status_code, reason=error)
        self._access = body["access_token"]
        self._secrets = (refresh_token, client_secret, self._access)
        try:
            lifetime = float(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._expires = time.monotonic() + max(lifetime - REFRESH_MARGIN, 0.0)

    # ---------- requests ----------

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """One API call; a 401 drops the access token and retries once with a fresh one."""
        headers = kwargs.pop("headers", {})
        for attempt in (1, 2):
            token = self._token()
            try:
                response = self._http.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"},
                                              **kwargs)
            except httpx.HTTPError as exc:
                raise DriveError(self._scrub(f"Drive {method} failed: {type(exc).__name__}: {exc}"),
                                 transient=True) from None
            if response.status_code == 401 and attempt == 1:
                with self._lock:
                    if self._access == token:
                        self._access = None
                continue
            break
        if response.status_code >= 400:
            raise self._http_error(method, response)
        return response

    def _http_error(self, method: str, response: httpx.Response) -> DriveError:
        message, reason = response.text[:300], ""
        try:
            error = response.json()["error"]
            message = str(error.get("message", ""))[:300]
            reason = str((error.get("errors") or [{}])[0].get("reason", ""))
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            pass
        status = response.status_code
        transient = status >= 500 or status == 429 or (status == 403 and reason in RATE_LIMIT_REASONS)
        cls = DriveNotFound if status == 404 else DriveError
        return cls(self._scrub(f"Drive {method} HTTP {status}: {message}"), transient=transient, status=status)

    @staticmethod
    def _multipart(metadata: dict, content: dict) -> tuple[bytes, str]:
        boundary = f"askhooman-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(content, ensure_ascii=False)}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        return body, f"multipart/related; boundary={boundary}"

    @staticmethod
    def _media_multipart(metadata: dict, raw: bytes, mime: str) -> tuple[tuple[bytes, bytes, bytes], str, int]:
        """A multipart/related body as (head, raw, tail) + its type and length: httpx sends the three pieces one after
        another, so the media bytes are never copied into one body."""
        boundary = f"askhooman-{uuid.uuid4().hex}"
        head = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        return (head, raw, tail), f"multipart/related; boundary={boundary}", len(head) + len(raw) + len(tail)

    # ---------- files in appDataFolder ----------

    def list_files(self, query: str | None = None) -> list[dict]:
        """Every matching file in appDataFolder (all pages): [{id, name, appProperties, modifiedTime}].
        Default: the ask and task files (Drive matches `name contains` by prefix, so both terms are needed)."""
        files, page = [], None
        self.truncated = False
        while True:
            params = {"spaces": "appDataFolder", "q": query or LIST_QUERY, "pageSize": 1000,
                      "fields": f"nextPageToken,files({self.file_fields})"}
            if page:
                params["pageToken"] = page
            data = self._request("GET", f"{API_URL}/files", params=params).json()
            files.extend(data.get("files") or [])
            page = data.get("nextPageToken")
            if self.max_files is not None and len(files) >= self.max_files:
                self.truncated = bool(page) or len(files) > self.max_files
                return files[:self.max_files]
            if not page:
                return files

    def find(self, name: str) -> list[dict]:
        """Files in appDataFolder with exactly this name (normally zero or one)."""
        return self.list_files(f"name = {_quote(name)} and trashed = false")

    def get_meta(self, file_id: str) -> dict | None:
        """{id, name, appProperties, modifiedTime} of a file, or None if it is gone (deleted or trashed)."""
        try:
            meta = self._request("GET", f"{API_URL}/files/{file_id}",
                                 params={"fields": f"{self.file_fields},trashed"}).json()
        except DriveNotFound:
            return None
        return None if meta.get("trashed") else meta

    def download(self, file_id: str) -> dict:
        if self.max_file_bytes is not None:
            raw = self._download_capped(file_id)
        else:
            raw = self._request("GET", f"{API_URL}/files/{file_id}", params={"alt": "media"}).content
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            doc = None
        if not isinstance(doc, dict):
            raise DriveBadFile(f"file {file_id} is not a JSON object")
        return doc

    def _download_capped(self, file_id: str) -> bytes:
        """The file's bytes, read raw (no decompression) and at most max_file_bytes; DriveBadFile if larger."""
        limit = self.max_file_bytes
        for attempt in (1, 2):
            token = self._token()
            try:
                with self._http.stream("GET", f"{API_URL}/files/{file_id}", params={"alt": "media"},
                                       headers={"Authorization": f"Bearer {token}",
                                                "Accept-Encoding": "identity"}) as response:
                    if response.status_code == 401 and attempt == 1:
                        with self._lock:
                            if self._access == token:
                                self._access = None
                        continue
                    if response.status_code >= 400:
                        response.read()
                        raise self._http_error("GET", response)
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise DriveBadFile(f"file {file_id} came compressed")
                    body = bytearray()
                    chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in chunks:
                        body.extend(chunk)
                        if len(body) > limit:
                            raise DriveBadFile(f"file {file_id} is larger than {limit} bytes")
                    return bytes(body)
            except httpx.HTTPError as exc:
                message = self._scrub(f"Drive GET failed: {type(exc).__name__}: {exc}")
                raise DriveError(message, transient=True) from None
        raise DriveError("Drive GET refused the access token twice", transient=True)

    def create(self, name: str, content: dict, app_properties: dict) -> dict:
        metadata = {"name": name, "mimeType": "application/json", "parents": ["appDataFolder"],
                    "appProperties": app_properties}
        body, content_type = self._multipart(metadata, content)
        return self._request("POST", f"{UPLOAD_URL}/files", params={"uploadType": "multipart", "fields": FILE_FIELDS},
                             content=body, headers={"Content-Type": content_type}).json()

    def update(self, file_id: str, content: dict, app_properties: dict) -> dict:
        body, content_type = self._multipart({"appProperties": app_properties}, content)
        return self._request("PATCH", f"{UPLOAD_URL}/files/{file_id}",
                             params={"uploadType": "multipart", "fields": FILE_FIELDS},
                             content=body, headers={"Content-Type": content_type}).json()

    def upload_media(self, name: str, raw: bytes, mime: str, app_properties: dict) -> dict:
        """A new binary file (a picture, a video, a poster) in appDataFolder -> {id, name, appProperties, modifiedTime}.
        Up to MULTIPART_MAX bytes in one multipart request; larger in a resumable session: a POST with the metadata,
        then one PUT of all the bytes to the session URL Drive answers with. `raw` goes into the request as it is,
        never copied, so a 20 MB video is held once."""
        metadata = {"name": name, "mimeType": mime, "parents": ["appDataFolder"], "appProperties": app_properties}
        params = {"uploadType": "multipart", "fields": FILE_FIELDS}
        if len(raw) <= MULTIPART_MAX:
            parts, content_type, length = self._media_multipart(metadata, raw, mime)
            return self._request("POST", f"{UPLOAD_URL}/files", params=params, content=parts,
                                 headers={"Content-Type": content_type, "Content-Length": str(length)}).json()
        session = self._request("POST", f"{UPLOAD_URL}/files", params={**params, "uploadType": "resumable"},
                                content=json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
                                headers={"Content-Type": "application/json; charset=UTF-8",
                                         "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(raw))})
        location = session.headers.get("location", "")
        place = urlsplit(location)
        if (place.scheme, place.netloc, place.path) != UPLOAD_SESSION:  # the bytes and the token go to Drive only
            raise DriveError(f"Drive POST HTTP {session.status_code}: the upload session URL for {name} is missing "
                             "or not Drive's", status=session.status_code)
        response = self._request("PUT", location, content=raw, headers={"Content-Type": mime})
        if response.status_code not in (200, 201):  # e.g. 308: Drive holds only part of the bytes
            raise DriveError(f"Drive PUT HTTP {response.status_code}: the upload of {name} did not complete",
                             transient=True, status=response.status_code)
        return response.json()

    def delete(self, file_id: str) -> None:
        """Delete a file for good (files.delete skips the trash). Already gone (404) counts as deleted."""
        try:
            self._request("DELETE", f"{API_URL}/files/{file_id}")
        except DriveNotFound:
            pass

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


OUR_FILE = re.compile(r"^(ask|task)-([1-9][0-9]{0,18})\.json$")


def parse_name(name: str) -> tuple[str, int] | None:
    """ask-17.json -> ("ask", 17), task-3.json -> ("task", 3); any other name -> None (not ours, ignored)."""
    match = OUR_FILE.match(name or "")
    return (match.group(1), int(match.group(2))) if match else None


def file_name(item_id: int, kind: str = "ask") -> str:
    return f"{kind}-{item_id}.json"
