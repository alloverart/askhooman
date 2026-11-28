"""Hosted bridge: GET /media/v1/<token> for a task reply's normal file (SPEC 8.3 «The routes», which is 7.4 «The route»,
«Parts», «Budgets», «Drive refusals» and «Normal file: streaming» with the names changed): the check order and the
task's 404, the 410s of a finished task, a deleted item and a pruned id, Drive's word on the file, parts, the budgets
an answer's files share, the stamps that never clear reply_pending, a finish while the file streams, and the links of
answers and recordings on the same path."""

import json
import logging
from datetime import datetime

import httpx
import pytest

from server.hosted import files_api
from server.hosted.crypto import Keyring
from server.hosted.files import PART_BYTES, download_name
from server.hosted.files_api import Target
from server.hosted.ratelimit import ByteBudget
from server.hosted.records import HostedTasks
from server.task_rules import REPLY_FILES
from server.tokens import Identity

from .fake_drive import app_task_reply_files, now_iso
from .hosted_support import DENIS_SUB, KEY, OLGA_EMAIL, OLGA_SUB, PUBLIC, id_token, mcp, tool_result
from .test_hosted_files_api import (
    BLOCKED,
    BODY,
    DELETED,
    LARGE,
    MIB,
    PART_TOO_BIG,
    World,
    asgi_get,
    charged,
    patch_media,
)
from .test_hosted_task_files import put_task_file, rewrite_task, sent_item, task_item, tasks_of
from .test_hosted_voice import agent, agent_id, answered_by_voice

NO_SUCH = {"detail": "no such file, or the link has expired: read the task again for a new link"}
NO_RECORDING = {"detail": "no such recording, or the link has expired: read the ask again for a new link"}
SENT = "the file was deleted after it was sent"
TOO_BIG = (f"this file is {len(LARGE)} bytes, more than one response can carry: fetch it in parts with Range: "
           "bytes=START-END, at most 32505856 bytes each (askhooman task files does this)")
LATER = "2026-11-28T15:00:00.000Z"


class TaskWorld:
    """An app with one agent (Allen, or the agent of `token`), its task #1 with one reply carrying one file (BODY,
    normal unless `secret`), and the owner's link to it as task_get gives it."""

    def __init__(self, hosted, google, content=BODY, name="Q3 report.PDF", token=None, **over):
        self.hosted, self.google = hosted, google
        self.token = token or agent(hosted, google)
        self.drive = google.drive(DENIS_SUB)
        tool_result(mcp(hosted, self.token, "task_create", {"title": "Chase the supplier"}))
        self.file_id = put_task_file(self.drive, content=content, secret=over.get("secret", False))
        self.items = app_task_reply_files(self.drive, 1, [task_item(
            1, name=name, mime="application/pdf", bytes=len(content), file_id=self.file_id, **over)])[REPLY_FILES]
        self.path = self.link()

    def link(self, n=0) -> str:
        files = tool_result(mcp(self.hosted, self.token, "task_get", {"id": 1}))[REPLY_FILES]
        if "url" in files[n]:
            return files[n]["url"].removeprefix(PUBLIC)
        stored = self.task()[REPLY_FILES][n]  # a secret file has no link: one made as the bridge makes them
        url, _ = self.hosted.app.state.task_file_links.url_for(
            DENIS_SUB, agent_id(self.hosted, self.google), 1, stored["reply_id"], stored["file_id"], stored["id"])
        return url.removeprefix(PUBLIC)

    def claims(self, path=None) -> dict:
        return self.hosted.app.state.task_file_links.open((path or self.path).rsplit("/", 1)[1])

    def sealed(self, context="taskfile", **changes) -> str:
        """A token sealed here with the link's claims `changes`d."""
        return f"/media/v1/{Keyring(KEY).encrypt(json.dumps({**self.claims(), **changes}), context)}"

    def get(self, path=None, **kwargs):
        return self.hosted.get(path or self.path, **kwargs)

    def task(self) -> dict:
        return self.drive.task_doc(1)

    def stored(self, n=0) -> dict:
        return self.task()[REPLY_FILES][n]

    def file_calls(self, file_id=None) -> list:
        """Every request about the reply's Drive file so far (its metadata and its bytes)."""
        return self.drive.calls("GET", f"/drive/v3/files/{file_id or self.file_id}")

    def content_reads(self, file_id=None) -> list:
        return [c for c in self.file_calls(file_id) if c[2].get("alt") == "media"]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.task()["events"]]


@pytest.fixture
def world(hosted, google):
    return TaskWorld(hosted, google)


def refusal(response) -> tuple[int, str]:
    """(status, detail) of a refusal _check_task returned."""
    return response.status_code, json.loads(response.body)["detail"]


# ---------- headers ----------

def test_download_headers_exactly(world):
    response = world.get()
    assert response.status_code == 200 and response.content == BODY
    assert dict(response.headers) == {
        "content-type": "application/octet-stream",
        "content-length": str(len(BODY)),
        "content-disposition": "attachment; filename=\"task-1-file-1.pdf\"; filename*=UTF-8''Q3%20report.PDF",
        "cache-control": "private, no-store",
        "x-content-type-options": "nosniff",
        "content-security-policy": "sandbox",
    }
    assert not any(h.startswith("access-control-") for h in response.headers)
    assert world.hosted.head(world.path).status_code == 405


def test_download_name_names_the_holder():
    assert download_name(12, 3, "Q3 report.PDF", "task") == ("task-12-file-3.pdf", "UTF-8''Q3%20report.PDF")
    assert download_name(12, 3, "noext", "task")[0] == "task-12-file-3"
    assert download_name(12, 3, "Q3 report.PDF") == ("ask-12-file-3.pdf", "UTF-8''Q3%20report.PDF")  # an ask's


# ---------- the check order ----------

def test_the_check_order(hosted, google):
    world = TaskWorld(hosted, google)
    assert files_api.TASK_NO_SUCH == NO_SUCH["detail"]
    token = world.path.rsplit("/", 1)[1]
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    # a token that opens under no context: nothing says what it was, so the recording route's words
    assert world.get(f"/media/v1/{flipped}").json() == NO_RECORDING
    assert world.get("/media/v1/v1.k1.garbage").json() == NO_RECORDING
    assert world.get(world.sealed(exp=1)).json() == NO_RECORDING  # expired: it doesn't open
    # the agent: another user's, an agent of the user who does not own the task
    agent(hosted, google, "Olga's bot", sub=OLGA_SUB, email=OLGA_EMAIL)
    olga_id = agent_id(hosted, google, "Olga's bot", sub=OLGA_SUB, email=OLGA_EMAIL)
    assert world.get(world.sealed(agent=olga_id)).json() == NO_SUCH
    assert world.get(world.sealed(user=OLGA_SUB)).json() == NO_SUCH
    agent(hosted, google, "Feynman")
    assert world.get(world.sealed(agent=agent_id(hosted, google, "Feynman"))).json() == NO_SUCH  # task #1 is Allen's
    # the item: another reply, another Drive file, a secret item, an id that never was, a task that never was
    assert world.get(world.sealed(reply_id=world.items[0]["reply_id"] + 1)).json() == NO_SUCH
    assert world.get(world.sealed(file_id="other")).json() == NO_SUCH
    secret = app_task_reply_files(world.drive, 1, [sent_item(world.drive, 2, secret=True)])[REPLY_FILES][1]
    assert world.get(world.sealed(n=2, reply_id=secret["reply_id"], file_id=secret["file_id"])).json() == NO_SUCH
    assert world.content_reads(secret["file_id"]) == []
    never = world.get(world.sealed(n=9))
    assert never.status_code == 404 and never.json() == NO_SUCH and never.headers["cache-control"] == "no-store"
    assert world.get(world.sealed(task_id=9)).json() == NO_SUCH
    assert world.file_calls() == []  # none of these asked Drive about the file
    assert world.get().status_code == 200  # the link itself is good
    # a revoked agent
    hosted.delete(f"/api/agents/{agent_id(hosted, google)}", headers=id_token(google)).raise_for_status()
    assert world.get().json() == NO_SUCH


def test_finished_task_410(world):
    rewrite_task(world.drive, 1, status="done")  # finished, by hand, with the item still naming its file
    response = world.get()
    assert (response.status_code, response.json()) == (410, DELETED)
    assert response.headers["cache-control"] == "no-store"
    assert world.file_calls() == []
    rewrite_task(world.drive, 1, status="active")
    assert world.get().status_code == 200
    assert tool_result(mcp(world.hosted, world.token, "task_done", {"id": 1}))["status"] == "done"
    assert (world.get().status_code, world.get().json()) == (410, DELETED)


def test_deleted_item_410(world):
    gone = {**world.stored(), "file_id": None, "deleted_at": now_iso(), "deleted_reason": "expired"}
    rewrite_task(world.drive, 1, reply_files=[gone])
    response = world.get()
    assert (response.status_code, response.json()) == (410, DELETED)
    assert world.file_calls() == []


def test_reopened_task_410(world):
    old = world.link()
    tool_result(mcp(world.hosted, world.token, "task_done", {"id": 1}))
    reopened = tool_result(mcp(world.hosted, world.token, "task_reopen", {"id": 1}))
    assert reopened["status"] == "active" and "url" not in reopened[REPLY_FILES][0]
    assert "url" not in tool_result(mcp(world.hosted, world.token, "task_get", {"id": 1}))[REPLY_FILES][0]
    for path in (old, world.path):
        assert (world.get(path).status_code, world.get(path).json()) == (410, DELETED)


def test_pruned_id_410(world):
    claims = world.claims()
    gone = {**world.stored(), "file_id": None, "deleted_at": now_iso(), "deleted_reason": "expired"}
    rewrite_task(world.drive, 1, reply_files=[gone])
    later = app_task_reply_files(world.drive, 1, [], text="Any news?")  # the user's next reply prunes the item
    assert later[REPLY_FILES] == [] and later["events"][-1]["kind"] == "replied"
    response = world.get()  # a file that is gone, not an id that never was
    assert (response.status_code, response.json()) == (410, DELETED)
    assert world.get(world.sealed(file_id="other")).json() == NO_SUCH  # not the snapshot's file
    assert world.get(world.sealed(reply_id=later["events"][-1]["id"])).json() == NO_SUCH  # another reply's
    assert world.get(world.sealed(n=2)).json() == NO_SUCH
    assert claims["n"] == 1 and world.file_calls() == []


# ---------- Drive's word on the file ----------

MARKS = {  # appProperties of the Drive file the item names, none of them this item's
    "an ask's file": {"ask_id": "1", "kind": "file", "file": "1", "secret": "0"},  # afile-1-1-…: ask #1's marks
    "ask_id besides": {"task_id": "1", "kind": "file", "file": "1", "secret": "0", "ask_id": "1"},
    "another task's": {"task_id": "2", "kind": "file", "file": "1", "secret": "0"},
    "another item's": {"task_id": "1", "kind": "file", "file": "2", "secret": "0"},
    "another kind": {"task_id": "1", "kind": "voice", "file": "1", "secret": "0"},
    "no kind": {"task_id": "1", "file": "1", "secret": "0"},
}


@pytest.mark.parametrize("marks", list(MARKS))
def test_an_afile_named_by_a_task_item_404(hosted, google, caplog, marks):
    world = TaskWorld(hosted, google)
    world.drive.files[world.file_id]["appProperties"] = dict(MARKS[marks])
    with caplog.at_level(logging.WARNING):
        assert world.get().json() == NO_SUCH
    assert world.content_reads() == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["hosted: the Drive file named by task #1 item 1 is not its reply file: not sent"]  # ids only


def test_drive_size_not_bytes_404(world):
    world.drive.files[world.file_id]["content"] = BODY + b"x"
    assert world.get().json() == NO_SUCH
    assert world.content_reads() == []


def test_size_over_the_cap_404(world, monkeypatch):
    monkeypatch.setattr(files_api, "MAX_FILE_BYTES", len(BODY) - 1)
    assert world.get().json() == NO_SUCH


def test_file_gone_from_drive_410(world):
    del world.drive.files[world.file_id]
    assert (world.get().status_code, world.get().json()) == (410, DELETED)


def test_blocked_item_410_without_a_drive_call(world):
    rewrite_task(world.drive, 1, reply_files=[{**world.stored(), "blocked_at": now_iso()}])
    budget = ByteBudget()
    world.hosted.app.state.file_bytes = budget
    slots = world.hosted.app.state.stream_slots
    assert slots.take(4)  # no slot is needed
    response = world.get()
    slots.give(4)
    assert (response.status_code, response.json()) == (410, BLOCKED)
    assert world.file_calls() == [] and budget._charges == {}


# ---------- parts ----------

def test_parts(hosted, google):
    world = TaskWorld(hosted, google, content=LARGE)
    slots = hosted.app.state.stream_slots
    assert slots.take(4)  # every slot busy: the 409 and the 416 need none
    whole = world.get()
    over = world.get(headers={"Range": f"bytes=0-{PART_BYTES}"})
    slots.give(4)
    assert (whole.status_code, whole.json()) == (409, {"detail": TOO_BIG})
    assert whole.json()["detail"].endswith("(askhooman task files does this)")
    assert (over.status_code, over.json()) == (416, {"detail": PART_TOO_BIG})
    assert over.headers["content-range"] == f"bytes */{len(LARGE)}"
    assert world.file_calls() == []  # decided from the item's bytes: not even its metadata is read
    start, end = 4096 + 7, 3 * 4096 + 2  # a middle part
    part = world.get(headers={"Range": f"bytes={start}-{end}"})
    assert part.status_code == 206 and part.content == LARGE[start:end + 1]
    assert part.headers["content-range"] == f"bytes {start}-{end}/{len(LARGE)}"
    assert part.headers["content-length"] == str(end - start + 1)
    assert part.headers["content-disposition"].startswith('attachment; filename="task-1-file-1.pdf"')
    assert world.stored()["downloaded_at"] is None  # not the end of the file
    last = len(LARGE) - 1
    assert world.get(headers={"Range": f"bytes={last - 4095}-{last}"}).status_code == 206  # the last part
    assert world.stored()["downloaded_at"] is not None and world.kinds().count("file_downloaded") == 1


# ---------- budgets ----------

def test_the_budgets_are_shared_with_answers_files(hosted, google):
    answers = World(hosted, google)  # Allen's ask #1, answered with a file
    world = TaskWorld(hosted, google, token=answers.token)  # and Allen's task #1, replied with one
    for _ in range(20):
        assert answers.get().status_code == 200
    refused = world.get()  # one `files` bucket for both kinds, not one each
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    assert refused.json() == {"detail": "too many requests for files: at most 20 a minute per agent"}
    limiter = hosted.app.state.limiter
    clock = limiter.clock
    limiter.clock = lambda: clock() + 61  # a minute on: the bucket is free again
    budget = ByteBudget(limit=2 * len(BODY))
    hosted.app.state.file_bytes = budget
    assert answers.get().status_code == 200 and world.get().status_code == 200
    assert charged(world, budget) == 2 * len(BODY)  # one byte budget, charged by both
    for which in (world, answers):
        response = which.get()
        assert response.status_code == 429 and response.headers["retry-after"] == "3600"
        assert response.json() == {"detail": "too many bytes downloaded: at most 1 GiB an hour per agent"}
    assert charged(world, budget) == 2 * len(BODY) and hosted.app.state.stream_slots.in_use == 0


# ---------- downloaded_at and blocked_at ----------

def test_fetching_a_file_does_not_clear_reply_pending(world):
    before = world.task()
    assert tasks_of(world.hosted).get(1)["reply_pending"] is True
    assert world.get().status_code == 200
    task = world.task()
    stored = task[REPLY_FILES][0]
    assert stored["downloaded_at"] is not None
    assert stored == {**before[REPLY_FILES][0], "downloaded_at": stored["downloaded_at"]}
    event = task["events"][-1]
    assert (event["kind"], event["payload"], event["actor"], event["at"]) == (
        "file_downloaded", {"id": 1}, "allen", stored["downloaded_at"])
    assert task["updated_at"] == before["updated_at"] and task["status"] == "active"
    assert tasks_of(world.hosted).get(1)["reply_pending"] is True
    got = tool_result(mcp(world.hosted, world.token, "task_get", {"id": 1}))
    assert got["reply_pending"] is True and got["reply"]["files"] == [1]
    assert got[REPLY_FILES][0]["downloaded_at"] == stored["downloaded_at"] and "url" in got[REPLY_FILES][0]
    # later downloads write nothing
    assert world.get().status_code == 200 and world.get(headers={"Range": "bytes=10-"}).status_code == 206
    assert world.task() == task


def test_mark_task_downloaded_guards(world):
    pool = world.hosted.app.state.pool
    args = (pool, DENIS_SUB, 1, 1, world.file_id)
    stamp = "2026-11-28T14:00:00.000Z"
    doc = world.task()
    for mark in (files_api.mark_task_downloaded, files_api.mark_task_blocked):
        assert mark(*args[:4], "other", stamp) is False  # another file_id
        assert mark(*args[:3], 2, world.file_id, stamp) is False  # another item
        assert mark(pool, DENIS_SUB, 9, 1, world.file_id, stamp) is False  # no such task
    assert world.task() == doc
    rewrite_task(world.drive, 1, status="done")  # finished, the item still naming its file
    doc = world.task()
    for mark in (files_api.mark_task_downloaded, files_api.mark_task_blocked):
        assert mark(*args, stamp) is False
    assert world.task() == doc
    rewrite_task(world.drive, 1, status="active")
    assert files_api.mark_task_downloaded(*args, stamp) is True
    assert world.stored()["downloaded_at"] == stamp and world.kinds()[-1] == "file_downloaded"
    doc = world.task()
    assert files_api.mark_task_downloaded(*args, LATER) is False  # a stamp already there
    assert world.task() == doc
    # Drive refuses the file for good: 410, and blocked_at with its event, written once
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason="cannotDownloadAbusiveFile")
    response = world.get()
    assert (response.status_code, response.json()) == (410, BLOCKED)
    task = world.task()
    stored, event = task[REPLY_FILES][0], task["events"][-1]
    assert stored["blocked_at"] is not None and stored["file_id"] == world.file_id  # blocked, not deleted
    assert (event["kind"], event["payload"], event["actor"]) == ("file_blocked", {"id": 1}, "allen")
    assert task["updated_at"] == doc["updated_at"] and tasks_of(world.hosted).get(1)["reply_pending"] is True
    assert files_api.mark_task_blocked(*args, LATER) is False and world.task() == task
    calls = len(world.file_calls())
    assert (world.get().status_code, world.get().json()) == (410, BLOCKED)
    assert len(world.file_calls()) == calls  # sticky: Drive is not asked again
    # the real finish: nothing more is written
    tool_result(mcp(world.hosted, world.token, "task_done", {"id": 1}))
    doc = world.task()
    for mark in (files_api.mark_task_downloaded, files_api.mark_task_blocked):
        assert mark(*args, LATER) is False
    assert world.task() == doc


def test_a_rate_limit_403_is_503_and_stamps_nothing(world):
    world.drive.fail_next("GET", f"/drive/v3/files/{world.file_id}", 403, after=1, reason="userRateLimitExceeded")
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert world.stored()["blocked_at"] is None and "file_blocked" not in world.kinds()


# ---------- Review Focus 2: the agent finishes the task while its download streams ----------

@pytest.mark.parametrize("cut", [True, False])
def test_a_finish_while_the_file_streams(hosted, google, monkeypatch, caplog, cut):
    """The finish deletes the Drive file mid-body. Drive cuts the stream (cut): the response breaks off, never a
    short 200. Or the bytes still all went out: the finished task gets no downloaded_at. Either way the next request is
    410."""
    big = bytes(range(256)) * (3 * MIB // 256)
    world = TaskWorld(hosted, google, content=big)
    tasks = HostedTasks(hosted.app.state.pool.records(DENIS_SUB))

    def body_of(drive, file):
        yield file["content"][:MIB + 5]
        tasks.done(Identity("agent", "allen"), 1)  # task_done: the write, then the bridge deletes the file
        assert file["id"] not in drive.files
        if cut:
            raise httpx.ReadError("the file is gone")
        yield file["content"][MIB + 5:]

    patch_media(monkeypatch, body_of)
    with caplog.at_level(logging.WARNING):
        sent = asgi_get(hosted.app, world.path)
    body = b"".join(m.get("body", b"") for m in sent["messages"])
    assert sent["messages"][0]["status"] == 200
    if cut:
        assert sent["error"] is not None and len(body) < len(big)
        assert not (sent["messages"][-1].get("more_body") is False)  # the response never completed
        assert "hosted: download of task #1 item 1 broke off after" in caplog.text  # by ids
        assert "Q3 report" not in caplog.text and world.path.rsplit("/", 1)[1] not in caplog.text
    else:
        assert sent["error"] is None and body == big
    assert hosted.app.state.stream_slots.in_use == 0
    stored = world.stored()
    assert (stored["file_id"], stored["deleted_reason"], stored["downloaded_at"]) == (None, "finished", None)
    assert "file_downloaded" not in world.kinds() and world.task()["status"] == "done"
    again = world.get()
    assert (again.status_code, again.json()) == (410, DELETED)


# ---------- Review Focus 3: a task the bridge can't read ----------

def test_an_unreadable_task_file_is_404_and_drive_down_is_503(world):
    task_file = world.drive.file("task-1.json")
    world.drive.down = True
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert response.json() == {"detail": "Google Drive is not available right now, try again"}
    world.drive.down = False
    rewrite_task(world.drive, 1, title="Chase the supplier again")  # a changed file: read again, not cached
    world.drive.fail_next("GET", f"/drive/v3/files/{task_file['id']}", 503)  # the read of the task file
    response = world.get()
    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert world.get().status_code == 200
    rewrite_task(world.drive, 1, context="x" * (300 * 1024))  # over the 256 KiB a task file may be
    assert world.get().json() == NO_SUCH
    rewrite_task(world.drive, 1, context=None, status="archived")  # a status no task has
    assert world.get().json() == NO_SUCH
    with world.drive.lock:
        task_file["content"], task_file["modifiedTime"] = b"not json {", world.drive._tick()
    response = world.get()
    assert (response.status_code, response.json()) == (404, NO_SUCH)


# ---------- the other links on /media/v1/ ----------

def test_answer_file_and_voice_links_still_work_on_the_same_path(hosted, google):
    answers = World(hosted, google)  # ask #1 answered with a file
    world = TaskWorld(hosted, google, token=answers.token)
    tool_result(mcp(hosted, answers.token, "ask_create", {"title": "Record it?"}))
    answered_by_voice(answers.drive, 2)
    voice = tool_result(mcp(hosted, answers.token, "ask_get", {"id": 2}))["answer_voice"]["url"]
    recording = hosted.get(voice.removeprefix(PUBLIC))
    assert recording.status_code == 200 and recording.headers["content-type"] == "audio/mp4"
    answer = answers.get()
    assert answer.status_code == 200 and answer.content == BODY
    assert answer.headers["content-disposition"].startswith('attachment; filename="ask-1-file-1.pdf"')
    reply = world.get()
    assert reply.status_code == 200 and reply.content == BODY
    assert reply.headers["content-disposition"].startswith('attachment; filename="task-1-file-1.pdf"')
    # each kind of token opens only its own: a task link's claims sealed as an answer's file or a recording, and an
    # answer's sealed as a task's, open nothing
    for context in ("file", "media"):
        assert hosted.get(world.sealed(context)).json() == NO_RECORDING, context
    answer_claims = json.dumps(answers.claims())
    assert hosted.get(f"/media/v1/{Keyring(KEY).encrypt(answer_claims, 'taskfile')}").json() == NO_RECORDING
    assert hosted.get("/media/v1/v1.k1.garbage").json() == NO_RECORDING


# ---------- _check_task in full: the secret branches the bearer route and the MCP fetch use (Task 5) ----------

def test_check_task_secret_branches(hosted, google, monkeypatch):
    world = TaskWorld(hosted, google, secret=True)
    pool = hosted.app.state.pool

    def check(n=1, **kwargs):
        return files_api._check_task(pool, DENIS_SUB, 1, n, "allen", None, True, **kwargs)

    assert world.get().json() == NO_SUCH  # through a link a secret item is the 404 of every link
    target = check(streams=True, range_header="bytes=0-9")
    assert isinstance(target, Target) and target.kind == "task" and target.secret
    assert (target.holder["id"], target.n, target.ids, target.span) == (1, 1, "task #1 item 1", None)
    assert target.item == world.stored() and isinstance(target.holder["events"], list)
    assert isinstance(check(sent_detail="already sent"), Target)  # nothing sent yet
    # past 24 h (from min(uploaded_at, replied_at), bounded by Drive's createdTime): 410 expired
    created = datetime.fromisoformat(world.drive.files[world.file_id]["createdTime"]).timestamp()
    monkeypatch.setattr(files_api, "_now", lambda: created + 24 * 3600 - 1)
    assert isinstance(check(), Target)
    monkeypatch.setattr(files_api, "_now", lambda: created + 24 * 3600 + 1)
    assert refusal(check()) == (410, "expired")
    monkeypatch.undo()
    # sent: downloaded_at, then the file deleted; the caller's words for «sent», else the route's
    assert files_api.mark_task_downloaded(pool, DENIS_SUB, 1, 1, world.file_id, now_iso()) is True
    assert refusal(check()) == (410, SENT)
    assert refusal(check(sent_detail="already sent")) == (410, "already sent")
    rewrite_task(world.drive, 1, reply_files=[{**world.stored(), "file_id": None, "deleted_at": now_iso(),
                                               "deleted_reason": "sent"}])
    assert refusal(check()) == (410, DELETED["detail"])
    assert refusal(check(sent_detail="already sent")) == (410, "already sent")
    # pruned by the next reply: the snapshot says secret and the history holds its file_downloaded
    app_task_reply_files(world.drive, 1, [sent_item(world.drive, 2, secret=True)], text="And this one")
    assert [f["id"] for f in world.task()[REPLY_FILES]] == [2]
    assert refusal(check()) == (410, SENT)
    assert refusal(check(sent_detail="already sent")) == (410, "already sent")
    # a pruned secret never sent (it expired): the file was deleted
    rewrite_task(world.drive, 1, reply_files=[{**world.stored(), "file_id": None, "deleted_at": now_iso(),
                                               "deleted_reason": "expired"}])
    app_task_reply_files(world.drive, 1, [], text="Never mind")
    assert world.task()[REPLY_FILES] == []
    assert refusal(check(n=2)) == (410, DELETED["detail"])
    assert refusal(check(n=2, sent_detail="already sent")) == (410, DELETED["detail"])
    assert refusal(check(n=3)) == (404, NO_SUCH["detail"])  # never was
    assert refusal(files_api._check_task(pool, DENIS_SUB, 1, 1, "feynman", None, True)) == (404, NO_SUCH["detail"])
