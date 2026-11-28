#!/usr/bin/env python3
"""asks — command line client for the Asks registry (questions waiting for the human's decision, and the agents'
tasks).

Stdlib only. Talks to the REST API described in SPEC.md.

  ASKS_URL         server (default http://127.0.0.1:8790)
  ASKS_TOKEN       bearer token; otherwise read from ASKS_TOKEN_FILE
                   (default ~/.config/asks/token)

Examples:
  asks.py list --mine                 my open + answered asks (check at session start)
  asks.py show 12
  asks.py create --title "Ship X today?" --context "..." --option "Yes" --option "No" --ref "chat 5487"
  asks.py create --json asks.json
  asks.py close 12 --resolution "shipped X"
  asks.py --json list --status all    raw JSON (for create, --json before the command)

Tasks (long-running work an agent does itself; the human only reads them):
  asks.py tasks --mine                my open tasks (active + waiting), next check first
  asks.py task create --title "Get the supplier to confirm the new fabric" --status waiting \
          --waiting-on "Supplier support reply" --next-check 2026-11-03 --ref "chat 5591"
  asks.py task update 3 --status active --next-check ""     ("" clears a field)
  asks.py task done 3 --resolution "Supplier confirmed"
  asks.py task ask 3 --question "Is the sample with you?" --option "Yes" --option "Not yet"
          the task shows among the human's questions until they reply; `task unask 3 --note "..."` takes it back

Waking up (the hosted bridge, ASKS_URL=https://askhooman.com with an agent key): wait for the user's answers and
replies and run a command per event, with ASKHOOMAN_EVENT and ASKHOOMAN_ASK_ID / ASKHOOMAN_TASK_ID set:
  asks.py listen --exec 'claude -p "check asks_mine and tasks_mine"'
  asks.py listen --once --cursor-file ~/.config/asks/cursor    exit on the next events; the next run starts after them

Pictures and video (the hosted bridge; PNG, JPEG, WebP up to 5 MB and 4096 px, MP4 up to 20 MB, 6 per ask):
  asks.py attach 12 cat.png --caption "Option 1"      upload a file (the server numbers it 1, 2, 3...)
  asks.py attach 12 --url https://example.com/a.png    the bridge fetches it
  asks.py attach 12 --url https://drive.google.com/file/d/x/view --link --title "Mockup"   a link card
  asks.py detach 12 2

Files your user attached to an answer (the hosted bridge): saved as ask-<id>-file-<n>[.ext] in --out (default the
current directory), never under the user's name for them; the size is checked against the one the ask says. A file over
31 MiB comes in parts of 31 MiB (Range requests, each checked and tried up to three times; a 429 is waited out, and a
link that expires mid-file is renewed once by reading the ask again).
  asks.py files 12 --out ./in           the normal files
  asks.py files 12 --secret             also the secret ones: each can be fetched once, then it is deleted

Files your user attached to their replies on your task (the hosted bridge): the same, saved as task-<id>-file-<n>[.ext],
every file of the task that still exists (not only the latest reply's). A task holds up to 30 files and the bridge
serves 20 file requests a minute, so here every fetch waits out a 429 and a normal file whose link expired takes a new
one by reading the task again, not only a file in parts. `task show` lists each reply's files under it.
  asks.py task files 7 --out ./in       the normal files of all the replies
  asks.py task files 7 --reply 12       only those of the reply with event id 12 (the items' reply_id)
  asks.py task files 7 --secret         also the secret ones: each can be fetched once, then it is deleted
"""

from __future__ import annotations  # keeps it runnable on macOS /usr/bin/python3 (3.9)

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

DEFAULT_URL = "http://127.0.0.1:8790"
DEFAULT_TOKEN_FILE = "~/.config/asks/token"
TIMEOUT = 20
STATUSES = ("active", "open", "answered", "closed", "all")
TASK_STATUSES = ("open", "active", "waiting", "done", "dropped", "finished", "all")
# C0/C1 control characters except tab and newline. Asks hold agent-written text that ends up in a
# terminal, where escape sequences would be executed. The server drops them too; this covers old data.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class CliError(Exception):
    """Printed as `error: ...` with exit code 1. `status`: the HTTP status, if the server answered."""

    def __init__(self, message: str, status: int | None = None, code: int = 1):
        super().__init__(message)
        self.status = status
        self.code = code  # the process exit code


def scrub(value):
    """Drop terminal control characters from every string in a JSON value."""
    if isinstance(value, str):
        return CONTROL_CHARS.sub("", value)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


# ---------- HTTP ----------

def load_token() -> str:
    token = os.environ.get("ASKS_TOKEN", "").strip()
    if token:
        return token
    path = os.path.expanduser(os.environ.get("ASKS_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except FileNotFoundError:
        token = ""
    except OSError as exc:
        raise CliError(f"cannot read token file {path}: {exc.strerror}")
    if not token:
        raise CliError(f"no token: set ASKS_TOKEN or put your token into {path} (chmod 600)")
    return token


class Client:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body=None, query: dict | None = None, timeout: float = TIMEOUT):
        url = self.base_url + path
        if query:
            query = {k: v for k, v in query.items() if v is not None}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "asks-cli/0.1",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise CliError(f"HTTP {exc.code}: {error_detail(exc)}", status=exc.code)
        except urllib.error.URLError as exc:
            raise CliError(f"cannot reach {self.base_url}: {exc.reason} (is the server running? is ASKS_URL right?)")
        except (TimeoutError, socket.timeout):  # separate classes before Python 3.10
            raise CliError(f"{self.base_url} did not answer in {timeout:g} s")
        except OSError as exc:  # e.g. connection reset mid-response
            raise CliError(f"connection to {self.base_url} failed: {exc}")
        except http.client.HTTPException as exc:  # IncompleteRead, BadStatusLine: not OSErrors
            raise CliError(f"connection to {self.base_url} failed: {type(exc).__name__}")
        if not raw:
            return None
        try:
            return scrub(json.loads(raw))
        except ValueError:
            raise CliError(f"server returned non-JSON for {method} {path}: {raw[:200]!r}")


def error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except OSError:
        raw = b""
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):  # FastAPI-style validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    if detail:
        return scrub(str(detail))
    text = scrub(raw.decode("utf-8", "replace")).strip()
    return text[:300] or exc.reason or "no details"


# ---------- formatting ----------

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age(value: str | None) -> str:
    dt = parse_time(value)
    if dt is None:
        return "?"
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def local_time(value: str | None) -> str:
    dt = parse_time(value)
    if dt is None:
        return value or "-"
    dt = dt.astimezone()
    return f"{dt:%Y-%m-%d %H:%M} {dt:%Z}".strip()


def first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def clip(text: str, width: int | None) -> str:
    if width is None or len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def print_ask_list(asks: list, counts: dict | None) -> None:
    if not asks:
        print("no asks")
    else:
        agent_w = max(len(a.get("agent") or "") for a in asks)
        id_w = max(len(str(a["id"])) for a in asks) + 1
        # Clip long titles only when writing to a terminal.
        term_w = shutil.get_terminal_size((0, 0)).columns if sys.stdout.isatty() else 0
        for a in asks:
            mark = "! " if a.get("priority") == "urgent" else ""
            head = f"{'#' + str(a['id']):<{id_w}}  {a.get('agent') or '':<{agent_w}}  {a['status']:<8}  {age(a.get('created_at')):>4}  "
            room = term_w - len(head) if term_w else None
            print(head + clip(mark + a.get("title", ""), room))
            indent = " " * (id_w + 2)
            note = None
            if a["status"] == "answered":
                note = "-> " + first_line(a.get("answer"))
            elif a["status"] == "closed" and a.get("resolution"):
                note = "=  " + first_line(a.get("resolution"))
            if note:
                print(indent + clip(note, term_w - len(indent) if term_w else None))
    if counts:
        print(f"\nopen {counts.get('open', 0)} · answered {counts.get('answered', 0)} · closed {counts.get('closed', 0)}")


def indented(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def event_summary(event: dict) -> str:
    kind = event.get("kind", "?")
    payload = event.get("payload") or {}
    if kind == "answered":
        return f"answered: {first_line(payload.get('answer'))}"
    if kind == "closed":
        resolution = first_line(payload.get("resolution"))
        return f"closed: {resolution}" if resolution else "closed"
    if kind == "edited":
        changed = ", ".join(sorted((payload.get("changes") or {}).keys()))
        return f"edited: {changed}" if changed else "edited"
    if kind == "reopened" and payload.get("previous_status"):
        return f"reopened (was {payload['previous_status']})"
    return kind


def print_ask(a: dict) -> None:
    urgent = "  URGENT" if a.get("priority") == "urgent" else ""
    ago = age(a.get("created_at"))
    print(f"#{a['id']}  [{a['status']}]{urgent}  from {a.get('agent')}  ·  {'just now' if ago == 'now' else ago + ' ago'}")
    print(a.get("title", ""))
    meta = [f"created {local_time(a.get('created_at'))}"]
    if a.get("ref"):
        meta.insert(0, f"ref {a['ref']}")
    print("  ·  ".join(meta))
    if a.get("context"):
        print("\nContext:")
        print(indented(a["context"]))
    if a.get("options"):
        print("\nOptions:")
        for i, option in enumerate(a["options"], 1):
            print(f"  {i}. {option}")
    if a.get("links"):
        print("\nLinks:")
        for link in a["links"]:
            title = link.get("title") or ""
            url = link.get("url", "")
            print(f"  - {title} — {url}" if title and title != url else f"  - {url}")
    if a.get("answer"):
        print(f"\nAnswer ({local_time(a.get('answered_at'))}):")
        print(indented(a["answer"]))
    if a["status"] == "closed":
        who = a.get("closed_by") or "?"
        print(f"\nClosed by {who} ({local_time(a.get('closed_at'))}): {a.get('resolution') or '-'}")
    if a.get("events"):
        print("\nHistory:")
        for event in a["events"]:
            print(f"  {local_time(event.get('at'))}  {event.get('actor', '?'):<9} {event_summary(event)}")


def print_one_line(a: dict, verb: str = "") -> None:
    prefix = f"{verb} " if verb else ""
    print(f"{prefix}#{a['id']} [{a['status']}] {a.get('title', '')}")
    if a["status"] == "answered" and a.get("answer"):
        print(f"  -> {first_line(a['answer'])}")
    if a["status"] == "closed" and a.get("resolution"):
        print(f"  =  {first_line(a['resolution'])}")


def check_note(next_check: str | None) -> str:
    """"check 2026-11-03", with "(overdue)" once the date has passed."""
    if not next_check:
        return ""
    try:
        overdue = date.fromisoformat(next_check) < date.today()
    except ValueError:
        overdue = False
    return f"check {next_check}" + (" (overdue)" if overdue else "")


def print_task_list(tasks: list, counts: dict | None) -> None:
    if not tasks:
        print("no tasks")
    else:
        agent_w = max(len(t.get("agent") or "") for t in tasks)
        id_w = max(len(str(t["id"])) for t in tasks) + 1
        term_w = shutil.get_terminal_size((0, 0)).columns if sys.stdout.isatty() else 0
        for t in tasks:
            head = f"{'#' + str(t['id']):<{id_w}}  {t.get('agent') or '':<{agent_w}}  {t['status']:<7}  {age(t.get('created_at')):>4}  "
            print(head + clip(t.get("title", ""), term_w - len(head) if term_w else None))
            notes = []
            if t["status"] == "waiting" and t.get("waiting_on"):
                notes.append("waiting on " + first_line(t["waiting_on"]))
            if t["status"] in ("active", "waiting") and t.get("next_check"):
                notes.append(check_note(t["next_check"]))
            if t["status"] in ("done", "dropped") and t.get("resolution"):
                notes.append("=  " + first_line(t["resolution"]))
            if notes:
                indent = " " * (id_w + 2)
                print(indent + clip(" · ".join(notes), term_w - len(indent) if term_w else None))
    if counts:
        print(f"\nactive {counts.get('active', 0)} · waiting {counts.get('waiting', 0)} · "
              f"done {counts.get('done', 0)} · dropped {counts.get('dropped', 0)}")


def task_event_summary(event: dict) -> str:
    kind = event.get("kind", "?")
    payload = event.get("payload") or {}
    if kind in ("edited", "status"):
        changes = payload.get("changes") or {}
        parts = []
        status = changes.get("status")
        if isinstance(status, dict):
            parts.append(f"{status.get('from')} -> {status.get('to')}")
        others = sorted(field for field in changes if field != "status")
        if others:
            parts.append(", ".join(others))
        return f"{kind}: {'; '.join(parts)}" if parts else kind
    if kind in ("done", "dropped"):
        resolution = first_line(payload.get("resolution"))
        return f"{kind}: {resolution}" if resolution else kind
    if kind == "reopened" and payload.get("previous_status"):
        return f"reopened (was {payload['previous_status']})"
    if kind == "asked":
        return f"asked: {question_line(payload)}"
    if kind in ("replied", "answered", "unasked"):
        text = first_line(payload.get("note" if kind == "unasked" else "text"))
        return f"{kind}: {text}" if text else kind
    return kind


def question_line(question: dict) -> str:
    """A task question on one line: its text, then the options in brackets."""
    options = [o for o in question.get("options") or [] if isinstance(o, str)]
    return first_line(question.get("text")) + (f"  [{' | '.join(options)}]" if options else "")


def print_task(t: dict) -> None:
    ago = age(t.get("created_at"))
    print(f"task #{t['id']}  [{t['status']}]  by {t.get('agent')}  ·  {'just now' if ago == 'now' else ago + ' ago'}")
    print(t.get("title", ""))
    meta = [f"created {local_time(t.get('created_at'))}", f"updated {local_time(t.get('updated_at'))}"]
    if t.get("ref"):
        meta.insert(0, f"ref {t['ref']}")
    print("  ·  ".join(meta))
    if t.get("waiting_on"):
        print(f"Waiting on: {t['waiting_on']}")
    if t.get("next_check"):
        print(f"Next {check_note(t['next_check'])}")
    if t.get("question_open") and isinstance(t.get("question"), dict):
        print(f"Open question: {question_line(t['question'])}")
    if t.get("context"):
        print("\nContext:")
        print(indented(t["context"]))
    if t.get("links"):
        print("\nLinks:")
        for link in t["links"]:
            title = link.get("title") or ""
            url = link.get("url", "")
            print(f"  - {title} — {url}" if title and title != url else f"  - {url}")
    if t["status"] in ("done", "dropped"):
        print(f"\n{t['status'].capitalize()} {local_time(t.get('finished_at'))}: {t.get('resolution') or '-'}")
    if t.get("events"):
        print("\nHistory:")
        for event in t["events"]:
            head = f"  {local_time(event.get('at'))}  {event.get('actor', '?'):<9} "
            print(head + task_event_summary(event))
            for line in reply_file_lines(event, t.get("reply_files")):
                print(" " * len(head) + line)


def reply_file_lines(event: dict, items) -> list:
    """The files of a `replied` event, one line each (SPEC 8.7): `file <n>: <name> · <bytes> bytes · <state>`, read
    from the item of reply_files with that id and this reply; an id with no such item any more (the user's next reply
    pruned it) is a file that is gone: the event's name and size, `deleted`. Another agent sees no names."""
    payload = event.get("payload")
    if event.get("kind") != "replied" or not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return []
    current = {i.get("id"): i for i in (items if isinstance(items, list) else [])
               if isinstance(i, dict) and i.get("reply_id") == event.get("id")}
    lines = []
    for snap in payload["files"]:
        if not isinstance(snap, dict) or not isinstance(snap.get("id"), int):
            continue
        item = current.get(snap["id"])
        if item is None:
            item, state = snap, "deleted"
        elif item.get("deleted_at"):
            state = f"deleted ({item['deleted_reason']})" if item.get("deleted_reason") else "deleted"
        elif item.get("blocked_at"):
            state = "blocked by Google"
        else:
            state = "downloaded" if item.get("downloaded_at") else "not downloaded yet"
        if item.get("secret") is True:
            state = "secret, " + state
        name = item.get("name")
        name = re.sub(r"\s", " ", name) if isinstance(name, str) and name else "(hidden)"  # one line per file
        size = item.get("bytes")
        size = f"{size} bytes" if isinstance(size, int) and not isinstance(size, bool) else "size unknown"
        lines.append(f"file {snap['id']}: {name} · {size} · {state}")
    return lines


def print_task_one_line(t: dict, verb: str = "") -> None:
    prefix = f"{verb} " if verb else ""
    print(f"{prefix}task #{t['id']} [{t['status']}] {t.get('title', '')}")
    if t["status"] == "waiting" and t.get("waiting_on"):
        print(f"  waiting on {first_line(t['waiting_on'])}")
    if t["status"] in ("done", "dropped") and t.get("resolution"):
        print(f"  =  {first_line(t['resolution'])}")


def print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------- argument helpers ----------

def read_text_arg(value: str | None) -> str | None:
    """`-` means read the text from stdin (handy for multi-line context or answers)."""
    if value == "-":
        return sys.stdin.read()
    return value


def parse_link(value: str) -> dict:
    # "title|url" — the url is after the last "|"; a bare url is also accepted.
    if "|" in value:
        title, url = value.rsplit("|", 1)
        link = {"title": title.strip(), "url": url.strip()}
    else:
        link = {"url": value.strip()}
    if not link["url"].lower().startswith(("http://", "https://")):
        raise CliError(f"--link {value!r}: url must start with http:// or https:// (format: \"title|url\")")
    return link


def fields_from_args(args, partial: bool) -> dict:
    """Collect ask fields from create/update flags. For update only the given flags are sent."""
    body = {}
    if args.title is not None:
        body["title"] = args.title
    if args.context is not None:
        body["context"] = read_text_arg(args.context)
    if args.option:
        body["options"] = args.option
    elif partial and args.clear_options:
        body["options"] = []
    if args.link:
        body["links"] = [parse_link(v) for v in args.link]
    elif partial and args.clear_links:
        body["links"] = []
    if args.priority is not None:
        body["priority"] = args.priority
    if args.ref is not None:
        body["ref"] = args.ref
    return body


def task_fields_from_args(args, partial: bool) -> dict:
    """Collect task fields from `task create` / `task update` flags; "" clears a text field or the date."""
    body = {}
    for field in ("title", "status", "waiting_on", "next_check", "ref"):
        if getattr(args, field) is not None:
            body[field] = getattr(args, field)
    if args.context is not None:
        body["context"] = read_text_arg(args.context)
    if args.link:
        body["links"] = [parse_link(v) for v in args.link]
    elif partial and args.clear_links:
        body["links"] = []
    return body


def load_json_file(path: str):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror}")
    except ValueError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}")


# ---------- commands ----------

def cmd_me(client, args):
    me = client.request("GET", "/api/me")
    if args.raw:
        print_json(me)
    elif me.get("role") == "agent":
        print(f"agent {me.get('agent')}")
    else:
        print(me.get("role"))


def cmd_list(client, args):
    agent = args.agent
    if args.mine:
        # Same as MCP asks_mine: the human has no asks of their own, so for them "mine" is everyone's.
        me = client.request("GET", "/api/me")
        agent = me.get("agent") if me.get("role") == "agent" else None
    result = client.request("GET", "/api/asks", query={"status": args.status, "agent": agent, "limit": args.limit})
    if args.raw:
        print_json(result)
    else:
        print_ask_list(result.get("asks", []), result.get("counts"))


def cmd_show(client, args):
    ask = client.request("GET", f"/api/asks/{args.id}")
    if args.raw:
        print_json(ask)
    else:
        print_ask(ask)


def cmd_create(client, args):
    if args.json_file is not None:
        if fields_from_args(args, partial=True) or args.clear_options or args.clear_links:
            raise CliError("use either --json FILE or the field flags, not both")
        data = load_json_file(args.json_file)
        items = data if isinstance(data, list) else [data]
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                raise CliError(f"{args.json_file}: item {i} must be an object with a non-empty title")
    else:
        if args.title is None:
            raise CliError("create needs --title (or --json FILE)")
        data = fields_from_args(args, partial=False)
        items = [data]

    created = []
    for item in items:
        try:
            created.append(client.request("POST", "/api/asks", body=item))
        except CliError as exc:
            if created:
                ids = ", ".join(f"#{a['id']}" for a in created)
                raise CliError(f"{exc} (failed on {item.get('title')!r}; already created: {ids})")
            raise
    if args.raw:
        print_json(created if isinstance(data, list) else created[0])
    else:
        for ask in created:
            print(f"created #{ask['id']}  {ask.get('title', '')}")


def show_result(ask: dict, args, verb: str = "") -> None:
    if args.raw:
        print_json(ask)
    else:
        print_one_line(ask, verb)


def cmd_update(client, args):
    body = fields_from_args(args, partial=True)
    if not body:
        raise CliError("nothing to update: give at least one of --title --context --option --link --priority --ref")
    show_result(client.request("PATCH", f"/api/asks/{args.id}", body=body), args, "updated")


def cmd_answer(client, args):
    show_result(client.request("POST", f"/api/asks/{args.id}/answer", body={"answer": read_text_arg(args.text)}), args)


def cmd_close(client, args):
    body = {"resolution": args.resolution} if args.resolution is not None else {}
    show_result(client.request("POST", f"/api/asks/{args.id}/close", body=body), args)


def cmd_reopen(client, args):
    show_result(client.request("POST", f"/api/asks/{args.id}/reopen", body={}), args)


# ---------- attachments ----------

NOT_MEDIA = "convert to PNG, JPEG or WebP; send motion as MP4"


def sniff_mime(head: bytes) -> str | None:
    """The type from a file's first bytes: PNG, JPEG, WebP, or MP4 (an ISO BMFF `ftyp` box). The server checks the
    rest (brand, sizes); this only picks the type to announce."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    return None


def describe_attachment(item: dict) -> str:
    if item.get("kind") == "link":
        return f"link {item.get('provider', 'web')}: {item.get('title', '')} <{item.get('url', '')}>"
    size = f"{item['width']}x{item['height']}, " if item.get("width") else ""
    return f"{item.get('kind')} {item.get('mime')} ({size}{item.get('bytes', 0):,} bytes)"


def print_attachment(ask_id: int, item: dict, args) -> None:
    if args.raw:
        print_json(item)
        return
    caption = f"  \"{item['caption']}\"" if item.get("caption") else ""
    print(f"attached #{item.get('id')} to ask #{ask_id}: {describe_attachment(item)}{caption}")


def put_file(client, upload: dict, f, size: int) -> dict:
    """PUT the open file to the one-time upload URL with the returned headers, streamed (no second read into
    memory), with an explicit Content-Length: the server answers 411 without one."""
    headers = {**upload["headers"], "Content-Length": str(size), "Accept": "application/json",
               "User-Agent": "asks-cli/0.1"}
    req = urllib.request.Request(upload["url"], data=f, headers=headers, method=upload.get("method", "PUT"))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise CliError(f"HTTP {exc.code}: {error_detail(exc)}", status=exc.code)
    except urllib.error.URLError as exc:
        raise CliError(f"cannot reach {upload['url']}: {exc.reason}")
    except (TimeoutError, socket.timeout):
        raise CliError("the upload did not finish in time")
    except OSError as exc:
        raise CliError(f"upload failed: {exc}")
    except http.client.HTTPException as exc:
        raise CliError(f"upload failed: {type(exc).__name__}")
    try:
        return scrub(json.loads(raw))
    except ValueError:
        raise CliError(f"server returned non-JSON for the upload: {raw[:200]!r}")


def cmd_attach(client, args):
    if (args.file is None) == (args.media_url is None):
        raise CliError("give a FILE or --url URL (not both)", code=2)
    if args.media_url is not None:
        if args.title is not None and not args.link:
            raise CliError("--title goes with --link", code=2)
        body = {"url": args.media_url, "link": True if args.link else None, "title": args.title,
                "caption": args.caption, "source_url": args.source}
        ask = client.request("POST", f"/api/asks/{args.id}/attachments",
                             body={k: v for k, v in body.items() if v is not None}, timeout=60)
        items = ask.get("attachments") or []
        if not items:
            raise CliError("the server returned no attachments")
        print_attachment(args.id, max(items, key=lambda i: i.get("id", 0)), args)
        return
    if args.link or args.title is not None:
        raise CliError("--link and --title go with --url", code=2)
    try:
        f = open(args.file, "rb")
    except OSError as exc:
        raise CliError(f"cannot read {args.file}: {exc.strerror}")
    with f:
        mime = sniff_mime(f.read(16))
        if mime is None:
            raise CliError(f"{args.file}: not a PNG, JPEG, WebP or MP4 file: {NOT_MEDIA}", code=2)
        size = os.fstat(f.fileno()).st_size
        body = {"mime": mime, "bytes": size, "caption": args.caption, "source_url": args.source}
        issued = client.request("POST", f"/api/asks/{args.id}/uploads",
                                body={k: v for k, v in body.items() if v is not None})
        f.seek(0)
        result = put_file(client, issued["upload"], f, size)
    print_attachment(args.id, result["attachment"], args)


def cmd_detach(client, args):
    ask = client.request("DELETE", f"/api/asks/{args.id}/attachments/{args.n}")
    if args.raw:
        print_json(ask)
    else:
        print(f"detached #{args.n} from ask #{args.id}")


# ---------- files in answers (SPEC 7.8) and in replies on tasks (SPEC 8.7) ----------

FILE_CHUNK = 1 << 20
EXT = re.compile(r"[A-Za-z0-9]{1,10}")
# The most body one file response of the bridge carries (server/hosted/files.py PART_BYTES, 31 MiB: Cloud Run refuses a
# response of 32 MiB or more); a normal file larger than that is fetched in closed Range parts of this size.
PART_BYTES = 32505856
PART_TRIES = 3  # a part that fails (a cut connection, a short body, a 5xx) is fetched again from its start, twice
PART_PAUSE_MAX = 30  # seconds: the longest Retry-After of a 5xx waited for before the next try
# A 429 on a part (the bridge's `files` bucket: 20 requests a minute per agent, and a 100 MiB file is 4 parts) is
# waited out and the same part asked again; these waits don't use up its tries, and a file gets at most PART_WAITS.
PART_WAITS = 6
RATE_PAUSE_MAX = 60  # seconds: the longest Retry-After of a 429 waited for; a longer one (the 1 GiB an hour byte
# budget says 3600) fails at once with the server's message
RATE_PAUSE = 10  # seconds waited after a 429 that names no Retry-After


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the bearer goes to the configured server only


def file_save_name(holder_id: int, item: dict, kind: str = "ask") -> str:
    """<kind>-<id>-file-<n>[.<ext>] (kind: ask or task): the user's name is data, never a path; only its last extension
    (1-10 ASCII letters or digits) is kept, as the bridge's Content-Disposition does."""
    name = f"{kind}-{holder_id}-file-{int(item['id'])}"
    stem, dot, ext = (item.get("name") or "").rpartition(".")
    if dot and stem and EXT.fullmatch(ext):
        name += "." + ext
    return name


SECRET_LOST = " - a secret file is deleted once it is sent: ask your user to send it again"


def _open_part_file(part: str, label: str) -> int:
    """The .part file a download writes: created or truncated, never through a symlink, readable by the owner only."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(part, flags, 0o600)
    except OSError as exc:
        raise CliError(f"{label}: cannot write {part}: {exc.strerror}")


def save_stream(resp, path: str, size: int, label: str, lost: str = "", kind: str = "ask") -> None:
    """Write the open response to `path`.part (no symlinks), check it came to exactly `size` bytes (what the `kind`,
    ask or task, says), then rename it onto `path`. On any failure only the .part file this call wrote is removed, by
    its exact path. `lost` is added to the message of a body that came short, long or broke off (a secret file can't
    be fetched again)."""
    part = path + ".part"
    fd = _open_part_file(part, label)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = resp.read(FILE_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise CliError(f"{label}: more than the {size} bytes the {kind} says{lost}")
                out.write(chunk)
        if total != size:
            raise CliError(f"{label}: got {total} of {size} bytes{lost}")
        os.replace(part, path)  # only a complete file takes the fixed name; an earlier good copy survives a failure
    except (OSError, http.client.HTTPException) as exc:  # a cut connection, a full disk
        _remove_own(part)
        if isinstance(exc, CliError):
            raise
        raise CliError(f"{label}: download failed: {type(exc).__name__}{lost}")
    except CliError:
        _remove_own(part)
        raise


def _remove_own(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def url_allowed(url: str, base_url: str) -> bool:
    """A normal file's link must be https; plain http only when the configured server itself is a local http one."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme == "https":
        return True
    local = ("127.0.0.1", "localhost", "::1")
    base = urllib.parse.urlsplit(base_url)
    return (scheme == "http" and base.scheme == "http" and base.hostname in local
            and urllib.parse.urlsplit(url).hostname in local)


def fetch_file(url: str, path: str, size: int, label: str, token: str | None = None, lost: str = "",
               kind: str = "ask", patience: _Patience | None = None) -> None:
    """One whole file in one GET (save_stream). Without `patience` (askhooman files) any refusal fails; with it
    (askhooman task files, SPEC 8.3) a 429 is waited out and a 404 takes a new link, as for a part (_Patience)."""
    headers = {"User-Agent": "asks-cli/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoRedirect) if token else urllib.request.build_opener()
    while True:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=60) as resp:
                save_stream(resp, path, size, label, lost, kind)
            return
        except urllib.error.HTTPError as exc:
            message = f"{label}: HTTP {exc.code}: {error_detail(exc)}"
            if patience is not None and exc.code == 429:
                patience.wait(message, _rate_wait(exc, message))
            elif patience is not None and exc.code == 404:
                url = patience.new_link(message)
            else:
                raise CliError(message, status=exc.code)
        except urllib.error.URLError as exc:
            raise CliError(f"{label}: cannot reach the server: {exc.reason}")
        except (TimeoutError, socket.timeout):
            raise CliError(f"{label}: the download stalled{lost}")


class _PartFailed(Exception):
    """A part that may be fetched again from its start: the message if it was the last try, and the seconds the
    server asked to wait (Retry-After of a 5xx) before the next one."""

    def __init__(self, message: str, wait: float = 0.0):
        super().__init__(message)
        self.wait = wait


class _PartLimited(_PartFailed):
    """A 429 on a part: wait `wait` seconds and ask for the same part again (not one of its tries)."""


class _LinkExpired(_PartFailed):
    """A 404 on a part: the link's hour may have run out (the bridge says so for a stale link too)."""


def _retry_after(exc: urllib.error.HTTPError, default: float = 0.0) -> float:
    """The seconds a refusal asks the client to wait (not negative); `default` when it names none or no number."""
    try:
        value = exc.headers.get("Retry-After")
        return max(float(value), 0.0) if value else default
    except (TypeError, ValueError):
        return default


def _rate_wait(exc: urllib.error.HTTPError, message: str) -> float:
    """The seconds to wait out a 429 (Retry-After; none given: RATE_PAUSE). One asking for more than RATE_PAUSE_MAX
    is not a busy minute (the byte budget says 3600): CliError at once, waiting would not help."""
    wait = _retry_after(exc, RATE_PAUSE)
    if wait > RATE_PAUSE_MAX:
        raise CliError(message, status=429)
    return wait


class _Patience:
    """What the requests of one file may wait for (SPEC 7.4 «Parts», 8.3): 429s waited out, at most PART_WAITS times
    a file, and one 404 (the link's hour ran out) answered by `refresh()`, which returns a new link or raises CliError
    when the file is not the same any more. Without `refresh` (a secret file has no link) a 404 fails."""

    def __init__(self, refresh=None):
        self.refresh = refresh
        self.waits = 0

    def wait(self, message: str, seconds: float) -> None:
        if self.waits == PART_WAITS:
            raise CliError(message, status=429)
        self.waits += 1
        SLEEP(seconds)

    def new_link(self, message: str) -> str:
        refresh, self.refresh = self.refresh, None  # a second 404 fails
        if refresh is None:
            raise CliError(message, status=404)
        return refresh()


def _fetch_part(opener, url: str, out, start: int, end: int, size: int, label: str) -> None:
    """Append bytes `start`-`end` of the file to `out` with one closed Range GET. The answer must be a 206 with
    Content-Range exactly `bytes start-end/size` and a Content-Length of the part, else CliError (a server that
    answers another range will do so again). A cut connection, a short body or a 5xx raise _PartFailed."""
    want = end - start + 1
    req = urllib.request.Request(url, headers={"User-Agent": "asks-cli/0.1", "Range": f"bytes={start}-{end}"},
                                 method="GET")
    try:
        with opener.open(req, timeout=60) as resp:
            if resp.status != 206:
                raise CliError(f"{label}: the server answered a part with HTTP {resp.status}, not 206: refused")
            if resp.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                raise CliError(f"{label}: the part {start}-{end} came with another Content-Range: refused")
            if resp.headers.get("Content-Length") != str(want):
                raise CliError(f"{label}: the part {start}-{end} came with another Content-Length: refused")
            got = 0
            while got < want:
                chunk = resp.read(min(FILE_CHUNK, want - got))
                if not chunk:
                    break
                got += len(chunk)
                try:
                    out.write(chunk)
                except OSError as exc:  # a full disk: not the network, so not tried again
                    raise CliError(f"{label}: cannot write: {exc.strerror}")
            if got != want:
                raise _PartFailed(f"{label}: got {start + got} of {size} bytes")
    except urllib.error.HTTPError as exc:
        message = f"{label}: HTTP {exc.code}: {error_detail(exc)}"
        if exc.code >= 500:
            raise _PartFailed(message, min(_retry_after(exc), PART_PAUSE_MAX))
        if exc.code == 429:
            raise _PartLimited(message, _rate_wait(exc, message))
        if exc.code == 404:
            raise _LinkExpired(message)
        raise CliError(message, status=exc.code)
    except urllib.error.URLError as exc:
        raise _PartFailed(f"{label}: cannot reach the server: {exc.reason}")
    except (TimeoutError, socket.timeout):
        raise _PartFailed(f"{label}: the download stalled")
    except (OSError, http.client.HTTPException) as exc:  # a connection cut mid-part
        raise _PartFailed(f"{label}: download failed: {type(exc).__name__}")


def fetch_in_parts(url: str, path: str, size: int, label: str, refresh=None) -> None:
    """A normal file larger than PART_BYTES (the bridge sends at most that in one response): closed Range parts of
    PART_BYTES, in order, into one `path`.part (no symlinks). Each part is checked (_fetch_part); one that fails is
    fetched again from its start, the .part cut back to it, up to PART_TRIES times in all. A 429 is waited out
    (Retry-After; none given: RATE_PAUSE; one over RATE_PAUSE_MAX fails at once) and the same part asked again, at
    most PART_WAITS times a file, without using up its tries. A 404 (the link expired) calls `refresh()` once for a
    new link (it raises CliError when the file in the ask or task is not the same any more) and goes on from that
    part; a second 404 fails (_Patience). The total must be `size`, then the .part is renamed onto `path`; on any
    failure only that .part file is removed, by its exact path."""
    part = path + ".part"
    fd = _open_part_file(part, label)
    opener = urllib.request.build_opener()
    patience = _Patience(refresh)
    try:
        with os.fdopen(fd, "wb") as out:
            for start in range(0, size, PART_BYTES):
                end = min(start + PART_BYTES, size) - 1
                tries = 0
                while True:
                    out.seek(start)
                    out.truncate()
                    try:
                        _fetch_part(opener, url, out, start, end, size, label)
                        break
                    except _PartLimited as exc:
                        patience.wait(str(exc), exc.wait)
                    except _LinkExpired as exc:
                        url = patience.new_link(str(exc))
                    except _PartFailed as exc:
                        tries += 1
                        if tries == PART_TRIES:
                            raise CliError(str(exc))
                        if exc.wait:
                            SLEEP(exc.wait)
            total = out.tell()
        if total != size:
            raise CliError(f"{label}: got {total} of {size} bytes")
        os.replace(part, path)  # only a complete file takes the fixed name; an earlier good copy survives a failure
    except CliError:
        _remove_own(part)
        raise
    except OSError as exc:  # the .part file itself (seek, truncate, close)
        _remove_own(part)
        raise CliError(f"{label}: cannot write {part}: {exc.strerror}")


def file_link(client, item: dict, label: str) -> str:
    """The absolute link of a normal file's item: https only, or plain http to a local http server (url_allowed)."""
    url = urllib.parse.urljoin(client.base_url + "/", item["url"])
    if not url_allowed(url, client.base_url):
        raise CliError(f"{label}: the download link is not https; refusing it")
    return url


def _item_again(holder: dict, key: str, n: int, size: int) -> dict | None:
    """Item `n` of the list `key` in an ask or a task read again, if it is still a normal file of `size` bytes, not
    deleted, with a link; else None."""
    items = holder.get(key)
    item = next((i for i in (items if isinstance(items, list) else []) if isinstance(i, dict) and i.get("id") == n),
                None)
    if (item is None or item.get("bytes") != size or item.get("secret") is not False or item.get("deleted_at")
            or not item.get("url")):
        return None
    return item


def fresh_link(client, ask_id: int, answered_at, n: int, size: int, label: str) -> str:
    """A new link for normal file `n` whose link expired mid-download: the ask read again, as cmd_files reads it. The
    file must be the same: still answered at `answered_at` (the agent never sees a file_id, and another file in an
    item comes only with a new answer), the item there, normal, `size` bytes, with a link. Else CliError."""
    ask = client.request("GET", f"/api/asks/{ask_id}")
    if not isinstance(ask, dict):
        raise CliError(f"{label}: the link expired and the server's answer to reading the ask again is not an ask")
    item = _item_again(ask, "answer_files", n, size)
    if item is None or ask.get("answered_at") != answered_at:
        raise CliError(f"{label}: the link expired and the file in the ask is not the same any more: read the ask "
                       "again")
    return file_link(client, item, label)


def fresh_task_link(client, task_id: int, n: int, reply_id, size: int, label: str) -> str:
    """fresh_link for a file of a reply on a task (SPEC 8.7): the task read again, as cmd_task_files reads it. The same
    file is item `n` of the same reply (`reply_id`: a reply is never replaced, and only its own write adds its items)
    with the same `bytes`, normal, not deleted, with a link. Else CliError."""
    task = client.request("GET", f"/api/tasks/{task_id}")
    if not isinstance(task, dict):
        raise CliError(f"{label}: the link expired and the server's answer to reading the task again is not a task")
    item = _item_again(task, "reply_files", n, size)
    if item is None or item.get("reply_id") != reply_id:
        raise CliError(f"{label}: the link expired and the file in the task is not the same any more: read the task "
                       "again")
    return file_link(client, item, label)


def save_files(client, args, kind: str, holder_id: int, items: list, refresh_for, patient: bool) -> None:
    """Save `items` (an answer's files or a task's, `kind` ask or task) into --out as <kind>-<id>-file-<n>[.ext] and
    print a line per file, or with --json the results ({id, path, bytes, status}); CliError when one is not saved.
    `refresh_for(item, name)` gives the callable that renews the expired link of a normal file. `patient` (askhooman
    task files, SPEC 8.3): every fetch waits out a 429, a whole file and a secret one too, and a whole normal file also
    takes a new link after a 404; without it only a file in parts does (askhooman files, as before)."""
    out_dir = args.out or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise CliError(f"cannot create {out_dir}: {exc.strerror}")
    failed, results = 0, []
    for item in items:
        n, size = item["id"], item.get("bytes")
        name = file_save_name(holder_id, item, kind)
        path = os.path.join(out_dir, name)
        result = {"id": n, "path": path, "bytes": size, "status": "saved"}
        if item.get("deleted_at"):
            result["status"] = "deleted"
        elif item.get("secret") and not args.secret:
            result["status"] = "skipped"
        elif not isinstance(size, int) or isinstance(size, bool) or size < 0:
            result["status"] = "failed"
            print(f"error: {name}: the {kind} gives no size", file=sys.stderr)
        else:
            try:
                if item.get("secret"):  # the bearer route: no link to renew
                    fetch_file(f"{client.base_url}/api/{kind}s/{holder_id}/files/{n}/content", path, size, name,
                               client.token, SECRET_LOST, kind, _Patience() if patient else None)
                elif not item.get("url"):
                    raise CliError(f"{name}: no download link (blocked or deleted); read the {kind} again")
                else:
                    url, refresh = file_link(client, item, name), refresh_for(item, name)
                    if size > PART_BYTES:  # more than one response of the bridge carries: in parts
                        fetch_in_parts(url, path, size, name, refresh)
                    else:
                        fetch_file(url, path, size, name, kind=kind, patience=_Patience(refresh) if patient else None)
            except CliError as exc:
                result["status"] = "failed"
                print(f"error: {exc}", file=sys.stderr)
        failed += result["status"] == "failed"
        results.append(result)
        if args.raw:
            continue
        if result["status"] == "saved":
            print(f"saved {path} ({size} bytes)")
        elif result["status"] == "skipped":
            print(f"skipped file {n} (secret): it can be fetched once; add --secret to fetch it")
        elif result["status"] == "deleted":
            print(f"skipped file {n}: already deleted")
    if args.raw:
        print_json(results)
    if failed:
        raise CliError(f"{failed} of {len(items)} files not saved")


def cmd_files(client, args):
    ask = client.request("GET", f"/api/asks/{args.id}")
    items = [i for i in (ask.get("answer_files") or []) if isinstance(i, dict) and isinstance(i.get("id"), int)]
    if not items:
        print("no files" if not args.raw else "[]")
        return
    save_files(client, args, "ask", args.id, items, lambda item, name: lambda: fresh_link(
        client, args.id, ask.get("answered_at"), item["id"], item["bytes"], name), patient=False)


def cmd_task_files(client, args):
    """The files your user attached to replies on your task (SPEC 8.7), read from the task once: all the replies' by
    default (two replies may come before the agent wakes, and a file missed is worse than one fetched twice), with
    --reply N those of the reply with event id N."""
    task = client.request("GET", f"/api/tasks/{args.id}")
    if not isinstance(task, dict):
        raise CliError("the server's answer to reading the task is not a task")
    items = [i for i in (task.get("reply_files") or []) if isinstance(i, dict) and isinstance(i.get("id"), int)]
    if args.reply is not None:
        items = [i for i in items if i.get("reply_id") == args.reply]
    if not items:
        print("[]" if args.raw else "no files" if args.reply is None else f"no files of reply {args.reply}")
        return
    save_files(client, args, "task", args.id, items, lambda item, name: lambda: fresh_task_link(
        client, args.id, item["id"], item.get("reply_id"), item["bytes"], name), patient=True)


# ---------- task commands ----------

def cmd_tasks(client, args):
    agent = args.agent
    if args.mine:
        # Same as MCP tasks_mine: for the human (no tasks of their own) "mine" is everyone's.
        me = client.request("GET", "/api/me")
        agent = me.get("agent") if me.get("role") == "agent" else None
    result = client.request("GET", "/api/tasks", query={"status": args.status, "agent": agent, "limit": args.limit})
    if args.raw:
        print_json(result)
    else:
        print_task_list(result.get("tasks", []), result.get("counts"))


def show_task(task: dict, args, verb: str = "") -> None:
    if args.raw:
        print_json(task)
    else:
        print_task_one_line(task, verb)


def cmd_task_show(client, args):
    task = client.request("GET", f"/api/tasks/{args.id}")
    if args.raw:
        print_json(task)
    else:
        print_task(task)


def cmd_task_create(client, args):
    if args.title is None:
        raise CliError("task create needs --title")
    show_task(client.request("POST", "/api/tasks", body=task_fields_from_args(args, partial=False)), args, "created")


def cmd_task_update(client, args):
    body = task_fields_from_args(args, partial=True)
    if not body:
        raise CliError("nothing to update: give at least one of --title --context --status --waiting-on "
                       "--next-check --link --clear-links --ref")
    show_task(client.request("PATCH", f"/api/tasks/{args.id}", body=body), args, "updated")


def cmd_task_finish(client, args):
    body = {"resolution": args.resolution} if args.resolution is not None else {}
    show_task(client.request("POST", f"/api/tasks/{args.id}/{args.action}", body=body), args)


def cmd_task_reopen(client, args):
    show_task(client.request("POST", f"/api/tasks/{args.id}/reopen", body={}), args, "reopened")


def cmd_task_ask(client, args):
    body = {"question": args.question, "options": args.option or []}
    show_task(client.request("POST", f"/api/tasks/{args.id}/ask", body=body), args, "asked on")


def cmd_task_unask(client, args):
    body = {"note": args.note} if args.note is not None else {}
    show_task(client.request("POST", f"/api/tasks/{args.id}/unask", body=body), args, "question taken back on")


# ---------- listen: wake on the user's actions (hosted bridge, SPEC v5) ----------

LISTEN_WAIT = 50  # seconds the bridge holds each long poll
LISTEN_HTTP_TIMEOUT = 70
LISTEN_BACKOFF = (5, 10, 20, 40, 60)  # seconds between retries after errors; then every 60 s
SLEEP = time.sleep  # tests replace it


def run_for_event(command: str, event: dict, timeout: float) -> None:
    """Run the user's command for one event, with only the event type and ids in the environment (never text the
    user or an agent wrote: nothing to inject). A failing or hanging command is reported and listening goes on."""
    env = dict(os.environ, ASKHOOMAN_EVENT=str(event.get("type", "")))
    for field, name in (("ask_id", "ASKHOOMAN_ASK_ID"), ("task_id", "ASKHOOMAN_TASK_ID")):
        env.pop(name, None)
        if isinstance(event.get(field), int):
            env[name] = str(event[field])
    try:
        done = subprocess.run(command, shell=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"listen: command timed out after {timeout:g} s", file=sys.stderr, flush=True)
        return
    if done.returncode != 0:
        print(f"listen: command exited {done.returncode}", file=sys.stderr, flush=True)


def cmd_listen(client, args):
    """Long-poll the agent's event feed (GET /api/agent/events); per event print one JSON line and, with --exec,
    run the command. Starts now (no replay), or with --cursor-file after the cursor stored there, so what the
    user did between two runs is not lost; network errors, 429 and 5xx back off and retry from the same cursor;
    other HTTP errors (401: the key is wrong or revoked) stop with exit code 1."""
    cursor = read_cursor(args.cursor_file)
    failures = 0
    while True:
        try:
            page = client.request("GET", "/api/agent/events", query={"after": cursor, "wait": LISTEN_WAIT},
                                  timeout=LISTEN_HTTP_TIMEOUT)
        except CliError as exc:
            if exc.status is not None and exc.status != 429 and exc.status < 500:
                raise
            delay = LISTEN_BACKOFF[min(failures, len(LISTEN_BACKOFF) - 1)]
            failures += 1
            print(f"listen: {exc}; retrying in {delay} s", file=sys.stderr, flush=True)
            SLEEP(delay)
            continue
        failures = 0
        page = page if isinstance(page, dict) else {}
        events = [e for e in page.get("events") or [] if isinstance(e, dict)]
        if page.get("missed"):
            events = [{"type": "missed"}]
        for event in events:
            print(json.dumps(event, ensure_ascii=False), flush=True)
            if args.exec:
                run_for_event(args.exec, event, args.timeout)
        cursor = str(page["cursor"]) if page.get("cursor") is not None else cursor
        write_cursor(args.cursor_file, cursor)
        if args.once and events:
            return


def read_cursor(path: str | None) -> str | None:
    """The cursor a previous `listen --cursor-file` left; no file, an empty or an unreadable one = start now."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_cursor(path: str | None, cursor: str | None) -> None:
    """Store the cursor after every page (atomically: a killed listener never leaves half a file)."""
    if not path or cursor is None:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(cursor)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"listen: could not store the cursor in {path}: {exc}", file=sys.stderr, flush=True)


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asks", description="Asks: questions waiting for the human's decision, and the agents' tasks.",
        epilog="Token: ASKS_TOKEN, or the file ASKS_TOKEN_FILE (default ~/.config/asks/token). Server: ASKS_URL.",
    )
    parser.add_argument("--url", default=os.environ.get("ASKS_URL") or DEFAULT_URL, help="server URL (env ASKS_URL)")
    parser.add_argument("--json", dest="raw", action="store_true", help="print raw JSON")

    # --json after a command too (except create, where --json FILE is the input).
    raw = argparse.ArgumentParser(add_help=False)
    raw.add_argument("--json", dest="raw", action="store_true", default=argparse.SUPPRESS, help="print raw JSON")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("me", parents=[raw], help="who am I (by token)")
    p.set_defaults(func=cmd_me)

    p = sub.add_parser("list", parents=[raw], help="list asks (default: open + answered)")
    p.add_argument("--status", choices=STATUSES, default="active")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--agent", help="only this agent's asks")
    group.add_argument("--mine", action="store_true", help="only my asks (agent token)")
    p.add_argument("--limit", type=int, help="max asks (server default 200)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", parents=[raw], help="one ask with its history")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    def field_flags(p, update: bool):
        p.add_argument("--title", help="one decision, phrased as a question (1-200 chars)")
        p.add_argument("--context", help="plain text, line breaks kept; '-' reads stdin")
        p.add_argument("--option", action="append", help="quick answer button (repeat, up to 6)"
                       + ("; replaces the whole list" if update else ""))
        p.add_argument("--link", action="append", metavar='"TITLE|URL"', help="link (repeat, up to 10)"
                       + ("; replaces the whole list" if update else ""))
        p.add_argument("--priority", choices=("normal", "urgent"))
        p.add_argument("--ref", help="free reference, e.g. 'TG 5487'")
        if update:
            p.add_argument("--clear-options", action="store_true", help="remove all options")
            p.add_argument("--clear-links", action="store_true", help="remove all links")
        else:
            p.set_defaults(clear_options=False, clear_links=False)

    p = sub.add_parser("create", help="create an ask (agent token); --json FILE creates one object or a list")
    field_flags(p, update=False)
    p.add_argument("--json", dest="json_file", metavar="FILE", help="JSON file ('-' = stdin): one ask object or a list")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("update", parents=[raw], help="edit my open ask")
    p.add_argument("id", type=int)
    field_flags(p, update=True)
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("answer", parents=[raw], help="answer an ask (the human's token)")
    p.add_argument("id", type=int)
    p.add_argument("--text", required=True, help="the answer; '-' reads stdin")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("close", parents=[raw], help="close an ask (owner agent or the human)")
    p.add_argument("id", type=int)
    p.add_argument("--resolution", help="one line: what was done")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reopen", parents=[raw], help="reopen an answered or closed ask")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("attach", parents=[raw], help="attach a picture or video to my open ask (hosted bridge)")
    p.add_argument("id", type=int)
    p.add_argument("file", nargs="?", help="PNG, JPEG, WebP (up to 5 MB, 4096 px) or MP4 (up to 20 MB) to upload")
    p.add_argument("--url", dest="media_url", help="https URL the bridge fetches (or, with --link, shows as a link card)")
    p.add_argument("--link", action="store_true", help="with --url: a link card, nothing fetched")
    p.add_argument("--title", help="link card title (with --link)")
    p.add_argument("--caption", help="caption, up to 300 characters")
    p.add_argument("--source", help="where the original lives (https URL)")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("files", parents=[raw], help="save the files your user attached to an answer "
                                                     "(hosted bridge; ask-<id>-file-<n>[.ext], size checked)")
    p.add_argument("id", type=int)
    p.add_argument("--out", metavar="DIR", help="directory to save into (default: the current one)")
    p.add_argument("--secret", action="store_true",
                   help="also fetch secret files: each can be fetched once, then AskHooman deletes it")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("detach", parents=[raw], help="remove attachment N from my open ask")
    p.add_argument("id", type=int)
    p.add_argument("n", type=int, help="the attachment number")
    p.set_defaults(func=cmd_detach)

    # ---------- tasks ----------

    p = sub.add_parser("tasks", parents=[raw], help="list tasks (default: open = active + waiting)")
    p.add_argument("--status", choices=TASK_STATUSES, default="open")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--agent", help="only this agent's tasks")
    group.add_argument("--mine", action="store_true", help="only my tasks (agent token)")
    p.add_argument("--limit", type=int, help="max tasks (server default 200; 100 for done/dropped/finished)")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("task", help="one task: show, create, update, done, drop, reopen, ask, unask, files")
    tsub = p.add_subparsers(dest="task_command", metavar="ACTION", required=True)

    def task_flags(q, update: bool):
        q.add_argument("--title", help="what is being done (1-200 chars)")
        q.add_argument("--context", help="plain text, line breaks kept; '-' reads stdin")
        q.add_argument("--status", choices=("active", "waiting"), help="waiting = blocked on someone outside")
        q.add_argument("--waiting-on", help="who/what it waits for (≤200 chars)" + ("; '' clears" if update else ""))
        q.add_argument("--next-check", metavar="YYYY-MM-DD", help="when to look again" + ("; '' clears" if update else ""))
        q.add_argument("--link", action="append", metavar='"TITLE|URL"', help="link (repeat, up to 10)"
                       + ("; replaces the whole list" if update else ""))
        q.add_argument("--ref", help="free reference, e.g. 'TG 5591'")
        if update:
            q.add_argument("--clear-links", action="store_true", help="remove all links")
        else:
            q.set_defaults(clear_links=False)

    q = tsub.add_parser("show", parents=[raw], help="one task with its history")
    q.add_argument("id", type=int)
    q.set_defaults(func=cmd_task_show)

    q = tsub.add_parser("create", parents=[raw], help="record work you are doing (agent token)")
    task_flags(q, update=False)
    q.set_defaults(func=cmd_task_create)

    q = tsub.add_parser("update", parents=[raw], help="update my open task")
    q.add_argument("id", type=int)
    task_flags(q, update=True)
    q.set_defaults(func=cmd_task_update)

    for action, verb in (("done", "mark my task done"), ("drop", "drop my task (no longer needed)")):
        q = tsub.add_parser(action, parents=[raw], help=verb)
        q.add_argument("id", type=int)
        q.add_argument("--resolution", help="one line: the result, or why it was dropped")
        q.set_defaults(func=cmd_task_finish, action=action)

    q = tsub.add_parser("reopen", parents=[raw], help="reopen my done or dropped task")
    q.add_argument("id", type=int)
    q.set_defaults(func=cmd_task_reopen)

    q = tsub.add_parser("ask", parents=[raw], help="ask the human a question inside my task: it shows among "
                                                   "their questions until they reply")
    q.add_argument("id", type=int)
    q.add_argument("--question", required=True, help="one question they can answer in one tap (up to 500 chars)")
    q.add_argument("--option", action="append", help="a likely answer (repeat, up to 6)")
    q.set_defaults(func=cmd_task_ask)

    q = tsub.add_parser("unask", parents=[raw], help="take my open question on a task back")
    q.add_argument("id", type=int)
    q.add_argument("--note", help="where the answer came from, or why it no longer matters")
    q.set_defaults(func=cmd_task_unask)

    q = tsub.add_parser("files", parents=[raw], help="save the files your user attached to replies on my task "
                                                     "(hosted bridge; task-<id>-file-<n>[.ext], size checked; "
                                                     "a 429 is waited out)")
    q.add_argument("id", type=int)
    q.add_argument("--out", metavar="DIR", help="directory to save into (default: the current one)")
    q.add_argument("--secret", action="store_true",
                   help="also fetch secret files: each can be fetched once, then AskHooman deletes it")
    q.add_argument("--reply", type=int, metavar="N",
                   help="only the files of the reply with event id N (their reply_id); default: all replies'")
    q.set_defaults(func=cmd_task_files)

    p = sub.add_parser("listen", help="wait for the user's answers and replies (hosted bridge, agent key); "
                                      "one JSON line per event")
    p.add_argument("--exec", metavar="COMMAND", help="run COMMAND per event, with ASKHOOMAN_EVENT and "
                                                     "ASKHOOMAN_ASK_ID / ASKHOOMAN_TASK_ID set")
    p.add_argument("--timeout", type=float, default=900.0, metavar="SECONDS",
                   help="stop a command that runs longer (default 900)")
    p.add_argument("--once", action="store_true", help="stop after the first events")
    p.add_argument("--cursor-file", metavar="PATH", help="keep the feed cursor in this file and start after it, so "
                                                         "events between two runs are not lost (use with --once)")
    p.set_defaults(func=cmd_listen)

    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    try:
        client = Client(args.url, load_token())
        args.func(client, args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
