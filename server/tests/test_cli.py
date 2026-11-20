"""cli/asks.py task commands against the real server (stdlib urllib over localhost)."""

import importlib.util
import json

from .conftest import ALLEN, ASKS_DIR, DENIS

spec = importlib.util.spec_from_file_location("asks_cli", ASKS_DIR / "cli" / "asks.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(capsys, monkeypatch, base_url, token, *argv):
    monkeypatch.setenv("ASKS_TOKEN", token)
    code = cli.main(["--url", base_url, *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_task_question(live_server, capsys, monkeypatch):
    """SPEC v5 "6. Task questions": `task ask` / `task unask`, and `task show` prints the question, the reply and
    the history lines with their text."""
    def allen(*argv):
        return run(capsys, monkeypatch, live_server, ALLEN, *argv)

    allen("task", "create", "--title", "Sew the sample")
    code, out, _ = allen("task", "ask", "1", "--question", "Which fabric?", "--option", "Cotton", "--option", "Linen")
    assert code == 0 and out.startswith("asked on task #1 [active] Sew the sample")
    code, out, _ = allen("task", "show", "1")
    assert code == 0 and "Open question: Which fabric?  [Cotton | Linen]" in out
    assert "asked: Which fabric?  [Cotton | Linen]" in out
    code, out, _ = run(capsys, monkeypatch, live_server, DENIS, "task", "show", "1", "--json")
    assert json.loads(out)["question_open"] is True
    code, out, _ = allen("task", "unask", "1", "--note", "Answered in chat: linen")
    assert code == 0 and out.startswith("question taken back on task #1")
    code, out, _ = allen("task", "show", "1")
    assert "Open question" not in out and "unasked: Answered in chat: linen" in out
    code, _, err = allen("task", "unask", "1")
    assert code == 1 and "HTTP 409" in err and "no open question" in err
    with pytest.raises(SystemExit):   # a question is required
        allen("task", "ask", "1")


def test_cli_task_commands(live_server, capsys, monkeypatch):
    def allen(*argv):
        return run(capsys, monkeypatch, live_server, ALLEN, *argv)

    code, out, _ = allen("task", "create", "--title", "Get the supplier to confirm the fabric", "--status", "waiting",
                         "--waiting-on", "Supplier support reply", "--next-check", "2000-01-01",
                         "--link", "Order|https://example.com/order", "--ref", "TG 5591")
    assert code == 0 and out.startswith("created task #1 [waiting] Get the supplier to confirm the fabric")
    code, out, _ = allen("tasks", "--mine")
    assert code == 0 and "#1" in out and "waiting on Supplier support reply · check 2000-01-01 (overdue)" in out
    assert "active 0 · waiting 1 · done 0 · dropped 0" in out
    code, out, _ = allen("task", "update", "1", "--status", "active", "--next-check", "", "--context", "line 1")
    assert code == 0 and out.startswith("updated task #1 [active]")
    code, out, _ = allen("task", "show", "1")
    assert code == 0 and "Waiting on: Supplier support reply" in out and "Next check" not in out
    assert "status: waiting -> active; context, next_check" in out and "Order — https://example.com/order" in out
    code, out, _ = allen("task", "done", "1", "--resolution", "Supplier confirmed")
    assert code == 0 and "=  Supplier confirmed" in out
    code, out, _ = allen("--json", "tasks", "--status", "finished")
    data = json.loads(out)
    assert [t["id"] for t in data["tasks"]] == [1] and data["counts"]["done"] == 1
    code, out, _ = allen("task", "reopen", "1", "--json")
    assert code == 0 and json.loads(out)["status"] == "active"

    code, _, err = run(capsys, monkeypatch, live_server, DENIS, "task", "drop", "1")
    assert code == 1 and err.startswith("error: HTTP 403:")
    code, _, err = allen("task", "update", "1")
    assert code == 1 and "nothing to update" in err
    code, _, err = allen("task", "create", "--title", "x", "--next-check", "Nov 3")
    assert code == 1 and "HTTP 422" in err and "next_check" in err


# ---------- listen --exec (SPEC v5 "Waking the agent") ----------

import http.server  # noqa: E402
import threading  # noqa: E402
import urllib.parse  # noqa: E402
from contextlib import contextmanager  # noqa: E402


@contextmanager
def feed_stub(replies):
    """A bridge that answers GET /api/agent/events with `replies` in turn: (status, body). Records the queries."""
    queries = []
    replies = list(replies)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            queries.append({**dict(urllib.parse.parse_qsl(parts.query)), "path": parts.path,
                            "auth": self.headers.get("Authorization")})
            reply = replies.pop(0) if replies else (200, {"events": [], "cursor": "0"})
            if reply == "cut":  # headers promise a body that never comes: the client sees IncompleteRead
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"events": [')
                self.close_connection = True
                return
            status, body = reply
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", queries
    finally:
        server.shutdown()


def event(n, kind="ask.answered", **ids):
    return {"id": n, "type": kind, "event": {"id": 2, "at": "2026-11-06T10:00:00.000Z"}, "at": "x", **ids}


def test_listen_runs_command_per_event(tmp_path, capsys, monkeypatch):
    out = tmp_path / "runs.txt"
    replies = [(200, {"events": [], "cursor": "0"}),
               (200, {"events": [event(1, ask_id=7), event(2, "task.replied", task_id=3)], "cursor": "2"})]
    with feed_stub(replies) as (url, queries):
        code, stdout, _ = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--exec",
                              f'echo "$ASKHOOMAN_EVENT|$ASKHOOMAN_ASK_ID|$ASKHOOMAN_TASK_ID" >> {out}')
    assert code == 0
    assert out.read_text().splitlines() == ["ask.answered|7|", "task.replied||3"]
    assert [q.get("after") for q in queries] == [None, "0"] and queries[1]["wait"] == "50"
    assert queries[0]["path"] == "/api/agent/events" and queries[0]["auth"] == "Bearer qtk_key"
    assert [json.loads(line)["id"] for line in stdout.splitlines()] == [1, 2]


def test_listen_resumes_from_a_cursor_file(tmp_path, capsys, monkeypatch):
    """`--once` exits on the first events; what the user did before the next start must not be lost (15.11.2026: an
    answer given 3 s before a restart was seen 18 minutes later). With --cursor-file the listener starts after the
    cursor it stored and stores the new one after every page."""
    cursor = tmp_path / "state" / "cursor"
    with feed_stub([(200, {"events": [], "cursor": "5"}), (200, {"events": [event(6, ask_id=7)], "cursor": "6"})]) as (
            url, queries):
        code, stdout, _ = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--cursor-file", str(cursor))
    assert code == 0 and [q.get("after") for q in queries] == [None, "5"]     # no file yet: starts now
    assert cursor.read_text() == "6" and [json.loads(line)["id"] for line in stdout.splitlines()] == [6]
    # the next start asks for what happened after the stored cursor and gets it at once
    with feed_stub([(200, {"events": [event(7, ask_id=8), event(8, "task.replied", task_id=3)], "cursor": "8"})]) as (
            url, queries):
        code, stdout, _ = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--cursor-file", str(cursor))
    assert code == 0 and [q.get("after") for q in queries] == ["6"]
    assert cursor.read_text() == "8" and [json.loads(line)["id"] for line in stdout.splitlines()] == [7, 8]
    # a cursor the bridge no longer keeps: one `missed` event, and the file moves on to the bridge's cursor
    with feed_stub([(200, {"events": [], "cursor": "40", "missed": True})]) as (url, queries):
        code, stdout, _ = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--cursor-file", str(cursor))
    assert code == 0 and queries[0]["after"] == "8" and json.loads(stdout)["type"] == "missed"
    assert cursor.read_text() == "40"
    # an empty or unreadable file is "start now", not a crash
    cursor.write_text("  \n")
    with feed_stub([(200, {"events": [event(41, ask_id=1)], "cursor": "41"})]) as (url, queries):
        code, _, _ = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--cursor-file", str(cursor))
    assert code == 0 and queries[0].get("after") is None and cursor.read_text() == "41"


def test_listen_survives_failing_command_and_network_error(tmp_path, capsys, monkeypatch):
    slept = []
    monkeypatch.setattr(cli, "SLEEP", slept.append)
    replies = [(200, {"events": [], "cursor": "4"}), (503, {"detail": "busy"}),
               (200, {"events": [event(5, ask_id=1)], "cursor": "5"})]
    with feed_stub(replies) as (url, queries):
        code, _, err = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--exec", "exit 3")
    assert code == 0 and "command exited 3" in err and "HTTP 503" in err
    assert slept == [5] and [q.get("after") for q in queries] == [None, "4", "4"]  # resumed from the same cursor


def test_listen_missed_and_401(tmp_path, capsys, monkeypatch):
    out = tmp_path / "runs.txt"
    with feed_stub([(200, {"events": [], "cursor": "9", "missed": True})]) as (url, _):
        code, _, _ = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--exec",
                         f'echo "$ASKHOOMAN_EVENT|$ASKHOOMAN_ASK_ID" >> {out}')
    assert code == 0 and out.read_text().splitlines() == ["missed|"]
    with feed_stub([(401, {"detail": "invalid token"})]) as (url, _):
        code, _, err = run(capsys, monkeypatch, url, "qtk_bad", "listen")
    assert code == 1 and "HTTP 401" in err


def test_listen_kills_a_command_that_hangs(tmp_path, capsys, monkeypatch):
    with feed_stub([(200, {"events": [event(1, ask_id=1)], "cursor": "1"})]) as (url, _):
        code, _, err = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once", "--timeout", "0.5",
                           "--exec", "sleep 30")
    assert code == 0 and "timed out after 0.5 s" in err


def test_listen_survives_a_cut_response(capsys, monkeypatch):
    """IncompleteRead / BadStatusLine are not OSErrors: a body cut by an instance shutdown must back off and resume,
    not crash the listener (review 06.11.2026)."""
    slept = []
    monkeypatch.setattr(cli, "SLEEP", slept.append)
    replies = [(200, {"events": [], "cursor": "4"}), "cut", (200, {"events": [event(5, ask_id=1)], "cursor": "5"})]
    with feed_stub(replies) as (url, queries):
        code, out, err = run(capsys, monkeypatch, url, "qtk_key", "listen", "--once")
    assert code == 0 and "IncompleteRead" in err and slept == [5]
    assert [q.get("after") for q in queries] == [None, "4", "4"] and json.loads(out)["id"] == 5


# ---------- attach / detach (hosted bridge, pictures and video) ----------

import socket  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

from .hosted_support import DENIS_SUB, build, connect, register  # noqa: E402
from .fake_google import FakeGoogle  # noqa: E402
from .media_samples import mp4, png  # noqa: E402


@pytest.fixture
def live_hosted():
    """The hosted bridge (fake Google, in-memory store) under uvicorn; yields (base URL, an agent's access token, the user's Drive)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    google = FakeGoogle(f"{base}/oauth/google/callback")
    app, _ = build(google, public=base)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        with httpx.Client(base_url=base) as client:  # the user signs an agent in: that also gives the bridge the Drive
            token = connect(client, google, register(client)["client_id"], agent="Allen", origin=base)["access_token"]
        yield base, token, google.drive(DENIS_SUB)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def new_hosted_ask(base, key):
    response = httpx.post(f"{base}/api/asks", json={"title": "Which one?"}, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_cli_attach_file(live_hosted, tmp_path, capsys, monkeypatch):
    base, key, drive = live_hosted
    ask_id = new_hosted_ask(base, key)
    picture = tmp_path / "cat.png"
    picture.write_bytes(png(64, 48))
    code, out, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id), str(picture), "--caption", "The cat")
    assert code == 0, err
    assert out.startswith(f"attached #1 to ask #{ask_id}: image image/png (64x48, ") and '"The cat"' in out
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(mp4(320, 240, 2.0))
    code, out, err = run(capsys, monkeypatch, base, key, "--json", "attach", str(ask_id), str(clip))
    assert code == 0, err
    item = json.loads(out)
    assert item["id"] == 2 and item["kind"] == "video" and item["mime"] == "video/mp4"
    assert sorted(f["name"] for f in drive.files.values() if f["name"].startswith("media-")) == \
        [f"media-{ask_id}-1.png", f"media-{ask_id}-2.mp4"]
    code, out, _ = run(capsys, monkeypatch, base, key, "--json", "show", str(ask_id))
    assert [a["id"] for a in json.loads(out)["attachments"]] == [1, 2]


def test_cli_attach_url_link(live_hosted, capsys, monkeypatch):
    base, key, _ = live_hosted
    ask_id = new_hosted_ask(base, key)
    code, out, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id), "--url",
                         "https://drive.google.com/file/d/abc/view", "--link", "--title", "Mockup",
                         "--caption", "v2")
    assert code == 0, err
    assert out.startswith(f"attached #1 to ask #{ask_id}: link drive: Mockup <https://drive.google.com/file/d/abc/view>")
    code, _, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id), "--url", "http://x.example/a.png")
    assert code == 1 and "HTTP 422" in err
    code, _, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id), "--url", "https://x.example/a",
                       "--link", "--source", "https://example.com/orig")
    assert code == 1 and "HTTP 422: source_url is for pictures and videos; a link card's url is the original" in err
    code, _, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id))
    assert code == 2 and "FILE or --url" in err


def test_cli_detach(live_hosted, tmp_path, capsys, monkeypatch):
    base, key, drive = live_hosted
    ask_id = new_hosted_ask(base, key)
    picture = tmp_path / "cat.png"
    picture.write_bytes(png(8, 8))
    assert run(capsys, monkeypatch, base, key, "attach", str(ask_id), str(picture))[0] == 0
    code, out, err = run(capsys, monkeypatch, base, key, "detach", str(ask_id), "1")
    assert code == 0, err
    assert out == f"detached #1 from ask #{ask_id}\n"
    assert not [f for f in drive.files.values() if f["name"].startswith("media-")]
    code, _, err = run(capsys, monkeypatch, base, key, "detach", str(ask_id), "1")
    assert code == 1 and "HTTP 404" in err


def test_cli_attach_unknown_type(live_hosted, tmp_path, capsys, monkeypatch):
    base, key, _ = live_hosted
    ask_id = new_hosted_ask(base, key)
    gif = tmp_path / "a.gif"
    gif.write_bytes(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff;")
    code, out, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id), str(gif))
    assert code == 2 and "convert to PNG, JPEG or WebP; send motion as MP4" in err and out == ""
    code, _, err = run(capsys, monkeypatch, base, key, "attach", str(ask_id), str(tmp_path / "missing.png"))
    assert code == 1 and "cannot read" in err


# ---------- files (answer_files, SPEC 7.8): a fake server ----------

import os  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402

FILE_A = b"A" * 5000
FILE_B = b"secret-bytes"


def serve_part(handler, body, header):
    """A closed Range answered as the bridge does: 206, Content-Range and Content-Length of exactly that part."""
    start, end = (int(x) for x in re.fullmatch(r"bytes=(\d+)-(\d+)", header).groups())
    end = min(end, len(body) - 1)
    handler.send_response(206)
    handler.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.end_headers()
    handler.wfile.write(body[start:end + 1])


@contextmanager
def files_stub(items, bodies, secret_bodies=None, ranges=None, part=None, ask_extra=None, ask_raw=None):
    """A bridge: GET /api/asks/5 (answer_files `items`, read at each request, and the fields of `ask_extra`; BASE in a
    url becomes this server; a body put in the list `ask_raw` is sent once instead), the normal files at /dl/<n> and
    the bearer route /api/asks/5/files/<n>/content (410 when `secret_bodies` has no such file). A /dl/ request with a
    Range gets serve_part, unless `part(handler, body, header)` answered it (True); `ranges` collects the Range header
    (or None) of every /dl/ request."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            seen.append((path, self.headers.get("Authorization")))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            if path == "/api/asks/5" and ask_raw:
                self.reply(200, ask_raw.pop(0))
            elif path == "/api/asks/5":
                shown = [{**i, "url": i["url"].replace("BASE", base) if i.get("url") else None} for i in items]
                ask = {"id": 5, "status": "answered", **(ask_extra or {}), "answer_files": shown}
                self.reply(200, json.dumps(ask).encode())
            elif path.startswith("/dl/"):
                n, header = int(path[4:]), self.headers.get("Range")
                if ranges is not None:
                    ranges.append(header)
                if header is None:
                    self.reply(200, bodies[n])
                elif not (part and part(self, bodies[n], header)):
                    serve_part(self, bodies[n], header)
            elif path.startswith("/api/asks/5/files/") and path.endswith("/content"):
                body = (secret_bodies or {}).get(int(path.split("/")[5]))
                self.reply(410, b'{"detail": "gone"}') if body is None else self.reply(200, body)
            else:
                self.reply(404, b"{}")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()


def file_item(n, size, name="../../etc/passwd.PDF", secret=False, **over):
    return {"id": n, "name": name, "mime": "application/pdf", "bytes": size, "secret": secret,
            "url": None if secret else f"BASE/dl/{n}?t=sealed-{n}", **over}


def test_cli_files_saves_with_fixed_names_and_checks_size(tmp_path, capsys, monkeypatch):
    items = [file_item(1, len(FILE_A), name="my report.pdf"), file_item(2, 9999, name="noext")]
    out_dir = tmp_path / "out"
    with files_stub(items, {1: FILE_A, 2: b"short"}) as (base, seen):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(out_dir))
    assert code == 1  # file 2 is the wrong size
    assert (out_dir / "ask-5-file-1.pdf").read_bytes() == FILE_A
    assert os.listdir(out_dir) == ["ask-5-file-1.pdf"]  # the short download is removed
    assert "ask-5-file-2" in err and "5 of 9999" in err
    assert "ask-5-file-1.pdf" in out and "sealed" not in out + err and "tok" not in out + err
    assert "my report" not in out + err
    # a normal file's url is the credential: no bearer goes to it
    assert [a for p, a in seen if p.startswith("/dl/")] == [None, None]


def test_cli_files_long_download_is_an_error_and_removed(tmp_path, capsys, monkeypatch):
    with files_stub([file_item(1, 100)], {1: b"x" * 200}) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1 and "ask-5-file-1.PDF" in err
    assert os.listdir(tmp_path) == []


def test_cli_files_secret_uses_bearer_route(tmp_path, capsys, monkeypatch):
    items = [file_item(1, len(FILE_A)), file_item(2, len(FILE_B), name="key.txt", secret=True)]
    with files_stub(items, {1: FILE_A}, {2: FILE_B}) as (base, seen):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
        assert code == 0, err
        assert os.listdir(tmp_path) == ["ask-5-file-1.PDF"]  # without --secret: listed as skipped
        assert "file 2" in out and "skipped" in out and "--secret" in out
        assert not any(p.endswith("/content") for p, _ in seen)
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path), "--secret")
    assert code == 0, err
    assert (tmp_path / "ask-5-file-2.txt").read_bytes() == FILE_B
    assert ("/api/asks/5/files/2/content", "Bearer tok") in seen
    assert FILE_B.decode() not in out + err and "tok" not in out + err


def test_cli_files_secret_second_request_is_410_and_leaves_nothing(tmp_path, capsys, monkeypatch):
    with files_stub([file_item(1, 5, secret=True)], {}, {}) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path), "--secret")
    assert code == 1 and "410" in err
    assert os.listdir(tmp_path) == []


def test_cli_files_a_short_secret_fetch_says_to_ask_for_it_again(tmp_path, capsys, monkeypatch):
    """Final review M5: a secret file is deleted once sent, so a short or cut body can't be fetched again."""
    items = [file_item(1, 100, name="key.txt", secret=True), file_item(2, 100, name="plain.txt")]
    with files_stub(items, {2: b"x" * 10}, {1: b"s" * 10}) as (base, _):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path), "--secret")
    assert code == 1
    secret_line = next(line for line in err.splitlines() if "ask-5-file-1" in line)
    assert "10 of 100" in secret_line and "ask your user to send it again" in secret_line
    assert "deleted" in secret_line  # why it can't be fetched again
    normal_line = next(line for line in err.splitlines() if "ask-5-file-2" in line)
    assert "10 of 100" in normal_line and "send it again" not in normal_line  # a normal file is fetched again
    assert os.listdir(tmp_path) == []


def test_cli_files_a_cut_secret_fetch_says_it_too(tmp_path, capsys, monkeypatch):
    items = [file_item(1, 100, name="key.txt", secret=True)]
    with files_stub(items, {}, {1: b"s" * 200}) as (base, _):  # more than the ask says: not a file we keep
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path), "--secret")
    assert code == 1 and "ask your user to send it again" in err


def test_cli_files_a_refused_secret_fetch_does_not_say_it(tmp_path, capsys, monkeypatch):
    """A 410 means the server did not send it (or already did and says so itself): no advice to ask again."""
    with files_stub([file_item(1, 5, secret=True)], {}, {}) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path), "--secret")
    assert code == 1 and "410" in err and "send it again" not in err


def test_cli_files_never_uses_name_as_path(tmp_path, capsys, monkeypatch):
    bad = ["../../evil", "/abs/path/x.sh", "a/b\\c.txt", "..", "x.tar.gz", "weird.ex e", "dots.verylongextension1"]
    items = [file_item(i + 1, 4, name=n) for i, n in enumerate(bad)]
    out_dir = tmp_path / "out"
    with files_stub(items, {i + 1: b"data" for i in range(len(bad))}) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(out_dir))
    assert code == 0, err
    assert sorted(os.listdir(out_dir)) == sorted(
        ["ask-5-file-1", "ask-5-file-2.sh", "ask-5-file-3.txt", "ask-5-file-4", "ask-5-file-5.gz", "ask-5-file-6",
         "ask-5-file-7"])
    assert os.listdir(tmp_path) == ["out"]


def test_cli_files_failure_removes_only_its_own_file(tmp_path, capsys, monkeypatch):
    (tmp_path / "other.txt").write_text("keep")
    with files_stub([file_item(1, 100)], {1: b"x" * 10}) as (base, _):
        code, _, _ = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1 and sorted(os.listdir(tmp_path)) == ["other.txt"]


def test_cli_files_no_files(tmp_path, capsys, monkeypatch):
    with files_stub([], {}) as (base, _):
        code, out, _ = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 0 and "no files" in out


def test_cli_files_short_redownload_keeps_the_earlier_good_copy(tmp_path, capsys, monkeypatch):
    (tmp_path / "ask-5-file-1.PDF").write_bytes(b"good copy")
    with files_stub([file_item(1, 100)], {1: b"x" * 10}) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1 and "10 of 100" in err
    assert (tmp_path / "ask-5-file-1.PDF").read_bytes() == b"good copy"
    assert os.listdir(tmp_path) == ["ask-5-file-1.PDF"]  # the .part file is gone


def test_cli_files_refuses_a_non_https_url_for_a_remote_server(tmp_path, capsys, monkeypatch):
    items = [file_item(1, 4)]
    items[0]["url"] = "http://files.example.com/dl/1?t=sealed-1"
    with files_stub(items, {1: b"data"}) as (base, seen):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
        assert code == 1 and "not https" in err and "sealed" not in out + err
        assert os.listdir(tmp_path) == [] and not any(p.startswith("/dl/") for p, _ in seen)
    assert cli.url_allowed("https://x.example/a", "https://askhooman.com")
    assert not cli.url_allowed("http://x.example/a", "https://askhooman.com")
    assert cli.url_allowed("http://127.0.0.1:9/a", "http://127.0.0.1:9")
    assert not cli.url_allowed("http://127.0.0.1:9/a", "https://askhooman.com")


# ---------- Task 11: a normal file over PART_BYTES is fetched in closed Range parts (spike 1) ----------

SOURCE = random.Random(5).randbytes(3500)  # with PART_BYTES at 1000: four parts, the last one 500 bytes
PARTS = ["bytes=0-999", "bytes=1000-1999", "bytes=2000-2999", "bytes=3000-3499"]


def reply_raw(handler, status, headers, body):
    handler.send_response(status)
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def short_part(handler, body, header):
    """The part's headers, then less than its Content-Length, and the connection closes."""
    start, end = (int(x) for x in re.fullmatch(r"bytes=(\d+)-(\d+)", header).groups())
    headers = {"Content-Range": f"bytes {start}-{end}/{len(body)}", "Content-Length": str(end - start + 1)}
    reply_raw(handler, 206, headers, body[start:start + 400])
    return True


@pytest.fixture
def small_parts(monkeypatch):
    monkeypatch.setattr(cli, "PART_BYTES", 1000)
    pauses = []
    monkeypatch.setattr(cli, "SLEEP", pauses.append)
    return pauses


def test_cli_part_bytes_is_the_servers():
    from server.hosted.files import PART_BYTES
    assert cli.PART_BYTES == PART_BYTES == 32505856


def test_big_file_is_fetched_in_parts_and_checked(tmp_path, capsys, monkeypatch, small_parts):
    ranges = []
    with files_stub([file_item(1, len(SOURCE), name="clip.mp4")], {1: SOURCE}, ranges=ranges) as (base, seen):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 0, err
    assert (tmp_path / "ask-5-file-1.mp4").read_bytes() == SOURCE
    assert os.listdir(tmp_path) == ["ask-5-file-1.mp4"]
    assert ranges == PARTS  # closed parts, in order
    assert f"saved {tmp_path / 'ask-5-file-1.mp4'} ({len(SOURCE)} bytes)" in out
    assert [a for p, a in seen if p.startswith("/dl/")] == [None] * 4  # the link is the credential: no bearer
    assert "sealed" not in out + err and small_parts == []


def test_a_failed_part_is_retried_then_succeeds(tmp_path, capsys, monkeypatch, small_parts):
    tries = []

    def flaky(handler, body, header):
        if header != "bytes=1000-1999":
            return False
        tries.append(header)
        if len(tries) == 1:
            return short_part(handler, body, header)
        if len(tries) == 2:
            busy = b'{"detail": "busy, try again in a few seconds"}'
            reply_raw(handler, 503, {"Retry-After": "5", "Content-Length": str(len(busy))}, busy)
            return True
        return False  # the third try goes through

    ranges = []
    with files_stub([file_item(1, len(SOURCE), name="clip.mp4")], {1: SOURCE}, ranges=ranges, part=flaky) as (base, _):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 0, err
    assert (tmp_path / "ask-5-file-1.mp4").read_bytes() == SOURCE  # the short try was cut back off the .part
    assert ranges == [PARTS[0], PARTS[1], PARTS[1], PARTS[1], PARTS[2], PARTS[3]]
    assert small_parts == [5]  # the 503 asked for 5 s
    assert os.listdir(tmp_path) == ["ask-5-file-1.mp4"]


def test_a_part_failing_three_times_fails_cleanly_and_removes_the_part_file(tmp_path, capsys, monkeypatch,
                                                                           small_parts):
    (tmp_path / "other.txt").write_text("keep")
    (tmp_path / "ask-5-file-1.mp4").write_bytes(b"good copy")  # an earlier download survives a failed one
    tries = []

    def broken(handler, body, header):
        if header != "bytes=2000-2999":
            return False
        tries.append(header)
        if len(tries) == 2:
            reply_raw(handler, 502, {"Content-Length": "2"}, b"{}")
            return True
        return short_part(handler, body, header)

    ranges = []
    items = [file_item(1, len(SOURCE), name="clip.mp4"), file_item(2, 4, name="b.txt")]
    with files_stub(items, {1: SOURCE, 2: b"data"}, ranges=ranges, part=broken) as (base, _):
        code, out, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1
    assert ranges == [PARTS[0], PARTS[1], PARTS[2], PARTS[2], PARTS[2], None]  # three tries, then the next file
    line = next(line for line in err.splitlines() if "ask-5-file-1.mp4" in line)
    assert f"got 2400 of {len(SOURCE)} bytes" in line and "send it again" not in line
    assert sorted(os.listdir(tmp_path)) == ["ask-5-file-1.mp4", "ask-5-file-2.txt", "other.txt"]  # no .part left
    assert (tmp_path / "ask-5-file-1.mp4").read_bytes() == b"good copy"
    assert (tmp_path / "other.txt").read_text() == "keep"
    assert "1 of 2 files not saved" in err


@pytest.mark.parametrize("status, content_range, body", [
    (206, "bytes 1000-1999/9999", SOURCE[1000:2000]),  # another total
    (206, "bytes 0-999/3500", SOURCE[:1000]),  # another start
    (206, "bytes 1000-1998/3500", SOURCE[1000:1999]),  # a shorter part, as the server says
    (206, None, SOURCE[1000:2000]),  # no Content-Range
    (200, None, SOURCE),  # the Range was ignored: the whole file
    (206, "bytes 1000-1999/3500", SOURCE[1000:1999]),  # a Content-Length that is not the part's
])
def test_wrong_content_range_is_refused(tmp_path, capsys, monkeypatch, small_parts, status, content_range, body):
    def lying(handler, whole, header):
        if header != "bytes=1000-1999":
            return False
        headers = {"Content-Length": str(len(body))}
        if content_range:
            headers["Content-Range"] = content_range
        reply_raw(handler, status, headers, body)
        return True

    ranges = []
    with files_stub([file_item(1, len(SOURCE), name="clip.mp4")], {1: SOURCE}, ranges=ranges, part=lying) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1 and "ask-5-file-1.mp4" in err and "refused" in err
    assert ranges == PARTS[:2]  # not retried: a server that answers another range will do it again
    assert os.listdir(tmp_path) == []


def test_small_file_is_one_get(tmp_path, capsys, monkeypatch, small_parts):
    ranges = []
    with files_stub([file_item(1, 1000, name="a.bin")], {1: SOURCE[:1000]}, ranges=ranges) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 0, err
    assert ranges == [None]  # exactly PART_BYTES: one GET without a Range
    assert (tmp_path / "ask-5-file-1.bin").read_bytes() == SOURCE[:1000]


def test_a_big_secret_file_is_still_one_get(tmp_path, capsys, monkeypatch, small_parts):
    items = [file_item(1, len(SOURCE), name="key.bin", secret=True)]
    with files_stub(items, {}, {1: SOURCE}) as (base, seen):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path), "--secret")
    assert code == 0, err
    assert (tmp_path / "ask-5-file-1.bin").read_bytes() == SOURCE
    assert [p for p, _ in seen if p.endswith("/content")] == ["/api/asks/5/files/1/content"]


# ---------- Task 11, fix round 1: a 429 on a part is waited out; an expired link is refreshed once ----------

ANSWERED = "2026-11-20T10:00:00.000Z"
LIMITED = b'{"detail": "too many requests for files: at most 20 a minute per agent"}'
EXPIRED = b'{"detail": "no such file, or the link has expired: read the ask again for a new link"}'


def too_many(handler, retry_after=None):
    headers = {"Content-Length": str(len(LIMITED))}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    reply_raw(handler, 429, headers, LIMITED)
    return True


def link_token(handler) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)["t"][0]


def test_a_429_on_a_part_is_waited_out_and_the_file_completes(tmp_path, capsys, monkeypatch, small_parts):
    script = [short_part, lambda h, b, r: too_many(h, "7"), lambda h, b, r: too_many(h),
              lambda h, b, r: too_many(h, "60"), short_part]  # then the part goes through

    def limited(handler, body, header):
        if header != "bytes=1000-1999" or not script:
            return False
        return script.pop(0)(handler, body, header)

    ranges = []
    items = [file_item(1, len(SOURCE), name="clip.mp4")]
    with files_stub(items, {1: SOURCE}, ranges=ranges, part=limited) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 0, err
    assert (tmp_path / "ask-5-file-1.mp4").read_bytes() == SOURCE
    # two short tries and the good one are the part's three: the three 429s in between did not use any of them
    assert ranges == [PARTS[0]] + [PARTS[1]] * 6 + PARTS[2:]
    assert small_parts == [7, 10, 60]  # Retry-After, none given: 10 s, 60 s is still waited (61 fails: round 2)
    assert os.listdir(tmp_path) == ["ask-5-file-1.mp4"]


def test_too_many_429s_fail_cleanly(tmp_path, capsys, monkeypatch, small_parts):
    (tmp_path / "ask-5-file-1.mp4").write_bytes(b"good copy")
    count = {"bytes=1000-1999": 0}

    def limited(handler, body, header):
        if header == "bytes=1000-1999" and count[header] < 3:  # three waits on this part, then it goes through
            count[header] += 1
            return too_many(handler, "1")
        if header == "bytes=2000-2999":  # always limited: the file's other three waits, then it fails
            return too_many(handler, "1")
        return False

    ranges = []
    items = [file_item(1, len(SOURCE), name="clip.mp4")]
    with files_stub(items, {1: SOURCE}, ranges=ranges, part=limited) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1
    assert ranges == [PARTS[0]] + [PARTS[1]] * 4 + [PARTS[2]] * 4  # six waits a file, then the 429 is the answer
    assert small_parts == [1] * 6
    line = next(line for line in err.splitlines() if "ask-5-file-1.mp4" in line)
    assert "HTTP 429" in line and "at most 20 a minute" in line
    assert os.listdir(tmp_path) == ["ask-5-file-1.mp4"] and (tmp_path / "ask-5-file-1.mp4").read_bytes() == b"good copy"


def test_an_expired_link_mid_file_is_refreshed_once_and_the_file_completes(tmp_path, capsys, monkeypatch,
                                                                         small_parts):
    items = [file_item(1, len(SOURCE), name="clip.mp4")]
    tokens = []

    def expiring(handler, body, header):
        tokens.append(link_token(handler))
        if tokens[-1] == "sealed-1" and header == PARTS[2]:  # the hour ran out after two parts
            items[0]["url"] = "BASE/dl/1?t=fresh-1"  # what the next read of the ask carries
            reply_raw(handler, 404, {"Content-Length": str(len(EXPIRED))}, EXPIRED)
            return True
        return False

    ranges = []
    with files_stub(items, {1: SOURCE}, ranges=ranges, part=expiring, ask_extra={"answered_at": ANSWERED}) as (
            base, seen):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 0, err
    assert (tmp_path / "ask-5-file-1.mp4").read_bytes() == SOURCE
    assert ranges == [PARTS[0], PARTS[1], PARTS[2], PARTS[2], PARTS[3]]  # on from the part that failed
    assert tokens == ["sealed-1"] * 3 + ["fresh-1"] * 2
    assert [p for p, _ in seen].count("/api/asks/5") == 2  # the ask read again, once
    assert "fresh-1" not in err and "sealed" not in err


@pytest.mark.parametrize("change", ["expires again", "item gone", "other bytes", "answered again", "now secret",
                                    "deleted", "not an ask"])
def test_an_expired_link_that_cannot_be_refreshed_fails_cleanly(tmp_path, capsys, monkeypatch, small_parts, change):
    (tmp_path / "other.txt").write_text("keep")
    items = [file_item(1, len(SOURCE), name="clip.mp4")]
    extra, raw = {"answered_at": ANSWERED}, []
    fresh = "BASE/dl/1?t=fresh-1"  # every changed item still carries a link: the check named by the case refuses it

    def expiring(handler, body, header):
        if header != PARTS[2]:
            return False
        if change == "item gone":
            items.clear()
        elif change == "other bytes":
            items[0] = file_item(1, len(SOURCE) + 1, name="clip.mp4")  # another file under the same item
        elif change == "answered again":
            extra["answered_at"] = "2026-11-20T11:00:00.000Z"  # a new answer: it may name another file
        elif change == "now secret":
            items[0] = file_item(1, len(SOURCE), name="clip.mp4", secret=True, url=fresh)
        elif change == "deleted":
            items[0] = file_item(1, len(SOURCE), name="clip.mp4", url=fresh, deleted_at="2026-11-20T11:00:00.000Z")
        elif change == "not an ask":
            raw.append(b'["not", "an", "ask"]')
        reply_raw(handler, 404, {"Content-Length": str(len(EXPIRED))}, EXPIRED)  # "expires again": every time
        return True

    ranges = []
    with files_stub(items, {1: SOURCE}, ranges=ranges, part=expiring, ask_extra=extra, ask_raw=raw) as (base, seen):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1
    assert [p for p, _ in seen].count("/api/asks/5") == 2  # read again once, never more
    assert ranges == ([PARTS[0], PARTS[1], PARTS[2], PARTS[2]] if change == "expires again"
                      else [PARTS[0], PARTS[1], PARTS[2]])  # no part from a file that is not the same
    line = next(line for line in err.splitlines() if "ask-5-file-1.mp4" in line)
    expected = {"expires again": "HTTP 404", "not an ask": "not an ask"}.get(change, "not the same")
    assert expected in line and "Traceback" not in err
    assert sorted(os.listdir(tmp_path)) == ["other.txt"]  # its .part is gone, nothing else touched


# ---------- Task 11, fix round 2: a 429 that asks for more than a minute, a 410 mid-file ----------

SPENT = b'{"detail": "too many bytes downloaded: at most 1 GiB an hour per agent"}'


@pytest.mark.parametrize("retry_after", ["3600", "61"])  # 3600: the 1 GiB an hour byte budget
def test_a_429_asking_for_more_than_a_minute_fails_at_once(tmp_path, capsys, monkeypatch, small_parts, retry_after):
    def spent(handler, body, header):
        if header != PARTS[1]:
            return False
        reply_raw(handler, 429, {"Retry-After": retry_after, "Content-Length": str(len(SPENT))}, SPENT)
        return True

    ranges = []
    items = [file_item(1, len(SOURCE), name="clip.mp4")]
    with files_stub(items, {1: SOURCE}, ranges=ranges, part=spent) as (base, _):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1
    assert ranges == PARTS[:2] and small_parts == []  # no wait, no second request
    line = next(line for line in err.splitlines() if "ask-5-file-1.mp4" in line)
    assert "HTTP 429" in line and "at most 1 GiB an hour per agent" in line
    assert os.listdir(tmp_path) == []


def test_a_part_answered_410_fails_at_once(tmp_path, capsys, monkeypatch, small_parts):
    gone = b'{"detail": "the file was deleted"}'

    def closed(handler, body, header):  # the ask was closed while the file came down
        if header != PARTS[1]:
            return False
        reply_raw(handler, 410, {"Content-Length": str(len(gone))}, gone)
        return True

    ranges = []
    items = [file_item(1, len(SOURCE), name="clip.mp4")]
    with files_stub(items, {1: SOURCE}, ranges=ranges, part=closed) as (base, seen):
        code, _, err = run(capsys, monkeypatch, base, "tok", "files", "5", "--out", str(tmp_path))
    assert code == 1
    assert ranges == PARTS[:2] and small_parts == []  # one request for that part
    assert [p for p, _ in seen].count("/api/asks/5") == 1  # a 410 is not an expired link: the ask is not read again
    line = next(line for line in err.splitlines() if "ask-5-file-1.mp4" in line)
    assert "HTTP 410" in line and "the file was deleted" in line
    assert os.listdir(tmp_path) == []
