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
