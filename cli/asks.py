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

Waking up (the hosted bridge, ASKS_URL=https://askhooman.com with an agent key): wait for the user's answers and
replies and run a command per event, with ASKHOOMAN_EVENT and ASKHOOMAN_ASK_ID / ASKHOOMAN_TASK_ID set:
  asks.py listen --exec 'claude -p "check asks_mine and tasks_mine"'
"""

from __future__ import annotations  # keeps it runnable on macOS /usr/bin/python3 (3.9)

import argparse
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

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
    return kind


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
            print(f"  {local_time(event.get('at'))}  {event.get('actor', '?'):<9} {task_event_summary(event)}")


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
    run the command. Starts now (no replay); network errors, 429 and 5xx back off and retry from the same cursor;
    other HTTP errors (401: the key is wrong or revoked) stop with exit code 1."""
    cursor = None
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
        if args.once and events:
            return


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

    # ---------- tasks ----------

    p = sub.add_parser("tasks", parents=[raw], help="list tasks (default: open = active + waiting)")
    p.add_argument("--status", choices=TASK_STATUSES, default="open")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--agent", help="only this agent's tasks")
    group.add_argument("--mine", action="store_true", help="only my tasks (agent token)")
    p.add_argument("--limit", type=int, help="max tasks (server default 200; 100 for done/dropped/finished)")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("task", help="one task: show, create, update, done, drop, reopen")
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

    p = sub.add_parser("listen", help="wait for the user's answers and replies (hosted bridge, agent key); "
                                      "one JSON line per event")
    p.add_argument("--exec", metavar="COMMAND", help="run COMMAND per event, with ASKHOOMAN_EVENT and "
                                                     "ASKHOOMAN_ASK_ID / ASKHOOMAN_TASK_ID set")
    p.add_argument("--timeout", type=float, default=900.0, metavar="SECONDS",
                   help="stop a command that runs longer (default 900)")
    p.add_argument("--once", action="store_true", help="stop after the first events")
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
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
