#!/usr/bin/env python3
"""AskHooman channel for Claude Code (SPEC v5 "Waking the agent").

A local stdio MCP server that declares the `claude/channel` capability, long-polls the agent's event feed on the
AskHooman bridge (GET /api/agent/events) with an agent key, and turns each event into a channel message in the
running Claude Code session: "AskHooman: your user answered ask #12 — call asks_mine". The agent then reads what
happened with the AskHooman MCP tools, as always.

Only the event type and ids go into a message, never text that the user or anyone else wrote, so nothing can be
injected into the session through it. Stdlib only (Python 3.9+): nothing to install.

  ASKHOOMAN_KEY   the agent key (qtk_…) from the AskHooman app; otherwise the file ~/.config/askhooman/key
  ASKHOOMAN_URL   the bridge (default https://askhooman.com)
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
DEFAULT_URL = "https://askhooman.com"
KEY_FILE = "~/.config/askhooman/key"
WAIT = 50  # seconds the bridge holds a long poll
HTTP_TIMEOUT = 70
BACKOFF = (5, 10, 20, 40, 60)  # seconds between retries after errors; then every 60 s
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
ASK_VERBS = {"ask.answered": "answered", "ask.closed": "closed", "ask.reopened": "reopened"}

INSTRUCTIONS = (
    "AskHooman wakes you when your user acts in the AskHooman app: they answered, closed or reopened one of your "
    "asks, or replied on one of your tasks. Such a message arrives as <channel source=\"askhooman\" event=\"...\" "
    "ask_id=\"...\"> (or task_id) and carries no words of the user: read what happened with the AskHooman tools "
    "(asks_mine for asks, tasks_mine for task replies), act on it, then ask_close the ask or answer the task "
    "(task_answer, task_update, task_done) as usual.")
NO_KEY = ("AskHooman: no agent key — create one in the AskHooman app (Create agent key) and save it to "
          "~/.config/askhooman/key")


def channel_message(event: dict) -> dict:
    """The channel notification's params for one feed event: {content, meta}. Built from the type and the ids only."""
    kind = event.get("type")
    meta = {"event": kind if kind in ASK_VERBS or kind in ("task.replied", "missed") else "other"}
    ask_id, task_id = event.get("ask_id"), event.get("task_id")
    if kind == "missed":
        content = "AskHooman: you may have missed events — call asks_mine and tasks_mine"
    elif kind in ASK_VERBS and type(ask_id) is int:
        content = f"AskHooman: your user {ASK_VERBS[kind]} ask #{ask_id} — call asks_mine"
        meta["ask_id"] = str(ask_id)
    elif kind == "task.replied" and type(task_id) is int:
        content = f"AskHooman: your user replied on task #{task_id} — call tasks_mine"
        meta["task_id"] = str(task_id)
    else:
        content = "AskHooman: something changed — call asks_mine and tasks_mine"
    if type(event.get("id")) is int:
        meta["event_id"] = str(event["id"])
    return {"content": content, "meta": meta}


class Output:
    """stdout is the JSON-RPC channel: one object per line, from the main thread and the poller, never mixed."""

    def __init__(self, stream=sys.stdout):
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        with self.lock:
            self.stream.write(line + "\n")
            self.stream.flush()

    def channel(self, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel", "params": params})


def log(text: str) -> None:
    print(f"askhooman: {text}", file=sys.stderr, flush=True)


def load_key() -> str:
    key = os.environ.get("ASKHOOMAN_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.expanduser(KEY_FILE), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class Refused(Exception):
    """The bridge refused the key (or has no feed): retrying will not help."""


def fetch(base_url: str, key: str, cursor: str | None) -> dict:
    query = {"wait": WAIT}
    if cursor is not None:
        query["after"] = cursor
    req = urllib.request.Request(f"{base_url.rstrip('/')}/api/agent/events?{urllib.parse.urlencode(query)}",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "User-Agent": f"askhooman-claude-code/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            page = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code != 429 and exc.code < 500:
            raise Refused(f"HTTP {exc.code}") from None
        raise
    return page if isinstance(page, dict) else {}


def poll(out: Output, base_url: str, key: str, sleep=time.sleep) -> None:
    """Long-poll forever: each event becomes a channel message. Starts now (no replay); errors back off and resume
    from the same cursor; a refused key is reported once and polling stops."""
    cursor = None
    failures = 0
    while True:
        try:
            page = fetch(base_url, key, cursor)
        except Refused as exc:
            out.channel({"content": f"AskHooman: the bridge refused the agent key ({exc}) — create a new key in the "
                                    "app and save it to ~/.config/askhooman/key, then restart Claude Code",
                         "meta": {"event": "error"}})
            return
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # URLError, timeouts and resets are OSErrors; a body cut short (IncompleteRead) or a garbled status line
            # are HTTPExceptions; bad JSON is a ValueError. All of them: back off and poll again from the cursor.
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            failures += 1
            log(f"{type(exc).__name__}: {exc}; retrying in {delay} s")
            sleep(delay)
            continue
        failures = 0
        events = [e for e in page.get("events") or [] if isinstance(e, dict)]
        if page.get("missed"):
            events = [{"type": "missed"}]
        for event in events:
            out.channel(channel_message(event))
        if page.get("cursor") is not None:
            cursor = str(page["cursor"])


def handle(message: dict, out: Output, start) -> None:
    method, request_id = message.get("method"), message.get("id")
    if method == "notifications/initialized":
        start()
        return
    if request_id is None:  # other notifications (cancelled, roots changed…): nothing to do
        return
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
                  "serverInfo": {"name": "askhooman", "version": VERSION}, "instructions": INSTRUCTIONS}
    elif method == "tools/list":
        result = {"tools": []}
    elif method == "ping":
        result = {}
    else:
        out.send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})
        return
    out.send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main() -> int:
    out = Output()
    started = threading.Event()

    def start() -> None:
        if started.is_set():
            return
        started.set()
        key = load_key()
        if not key:
            out.channel({"content": NO_KEY, "meta": {"event": "error"}})
            return
        base_url = os.environ.get("ASKHOOMAN_URL", "").strip() or DEFAULT_URL
        threading.Thread(target=poll, args=(out, base_url, key), name="askhooman-poll", daemon=True).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            out.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(message, dict):
            handle(message, out, start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
