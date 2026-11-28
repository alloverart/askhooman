"""Hosted bridge: the hourly sweep of a task's reply files (SPEC 8.5): 7.6's rules with the names changed, in the same
run as an answer's files (one listing, one read cap), what differs (an open task may name files, so its files take a
read), the task written only while its item names the file, healing, the claims of finished tasks, and nothing of an
ask touched for a task or the reverse. Delete Account's purge of both kinds is in test_hosted_delete_account."""

import time
from datetime import datetime, timezone

import pytest

from server.hosted import sweep as sweep_module
from server.hosted.files import CLOCK_SLACK, FILE_ORPHAN, SECRET_ORPHAN, SECRET_TTL, task_claim_key
from server.hosted.sweep import LIST_FILES, NORMAL_TTL, TASK_FILE_NAME, reply_files
from server.task_rules import REPLY_FILES

from .fake_drive import app_action, app_task_reply_files, now_iso
from .hosted_support import mcp, tool_result
from .test_hosted_sweep import DAY, HOUR, Sweeper, epoch
from .test_hosted_task_files import rewrite_task, task_item, tasks_of


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskSweeper(Sweeper):
    """test_hosted_sweep.Sweeper (one user's agent and drive, asks answered with files) with tasks too: a reply carries
    files Drive made a minute before it, and `after(task, seconds)` moves the sweep's clock to a chosen distance after
    that reply."""

    def __init__(self, hosted, google, **kwargs):
        super().__init__(hosted, google, **kwargs)
        self.replies: dict[int, float] = {}  # task id -> epoch of its last reply with files

    def task(self, title="Chase the supplier") -> int:
        return tool_result(mcp(self.hosted, self.token, "task_create", {"title": title}))["id"]

    def put_task(self, task_id: int, n: int, uploaded: float | None = None, secret=False, created: float | None = None,
                 **props) -> str:
        """A file of a reply as the apps store it (tfile-<task>-<n>-<ms>, its marks); the time in its name and Drive's
        createdTime are `uploaded` unless `created` says Drive saw it at another time."""
        moment = self.now if uploaded is None else uploaded
        marks = {"task_id": str(task_id), "kind": "file", "file": str(n), "secret": "1" if secret else "0", **props}
        return self.drive.add(f"tfile-{task_id}-{n}-{int(moment * 1000)}", b"Q3 report " * 10, marks,
                              mime="application/octet-stream", created=moment if created is None else created)

    def replied(self, task_id: int, *specs) -> list[dict]:
        """The user's reply on task `task_id` with files; each spec is (n, secret) and gets a Drive file uploaded a
        minute before. The items' uploaded_at is the reply's time (min() is that instant here)."""
        items = [task_item(n, file_id=self.put_task(task_id, n, uploaded=time.time() - 60, secret=secret),
                           secret=secret, bytes=100) for n, secret in specs]
        doc = app_task_reply_files(self.drive, task_id, items)
        reply_id = doc["events"][-1]["id"]
        stored = [{**i, "uploaded_at": i["replied_at"]} if i["reply_id"] == reply_id else i for i in doc[REPLY_FILES]]
        rewrite_task(self.drive, task_id, reply_files=stored)
        self.replies[task_id] = epoch(doc["events"][-1]["at"])
        return [i for i in stored if i["reply_id"] == reply_id]

    def after(self, task_id: int, seconds: float) -> float:
        self.now = self.replies[task_id] + seconds
        return self.now

    def task_doc(self, task_id: int) -> dict:
        return self.drive.task_doc(task_id)

    def stored(self, task_id: int, n=1) -> dict:
        return next(i for i in self.task_doc(task_id)[REPLY_FILES] if i["id"] == n)

    def restamp(self, task_id: int, n: int, **changes) -> None:
        """Change item `n` of the task's reply_files, as any client of the user's Drive could."""
        rewrite_task(self.drive, task_id, reply_files=[{**i, **changes} if i["id"] == n else i
                                                       for i in self.task_doc(task_id)[REPLY_FILES]])

    def hold_task(self, task_id: int, file_id: str, state: str, until=None, expires_at=None) -> str:
        """A claim document of a task's secret file as files.FileClaims writes one (task_id, no ask_id): its key."""
        key = task_claim_key(self.sub, task_id, file_id)
        doc = {"user": self.sub, "task_id": task_id, "state": state}
        if state == "held":
            doc.update(until=until, owner="o1")
        if expires_at is not None:
            doc["expires_at"] = expires_at
        self.hosted.store.put("fileclaims", key, doc)
        return key

    def claim(self, key: str) -> dict | None:
        return self.hosted.store.get("fileclaims", key)

    def finish(self, task_id: int, tool="task_done") -> None:
        """The agent finishes the task while the Drive delete of every file it names fails: the items are stamped
        `finished` (plan_finish), the files stay for the sweep."""
        for i in self.task_doc(task_id)[REPLY_FILES]:
            if i["file_id"] is not None:
                self.drive.fail_next("DELETE", f"/drive/v3/files/{i['file_id']}", 500)
        tool_result(mcp(self.hosted, self.token, tool, {"id": task_id}))

    def record_reads(self) -> list[str]:
        """The names of the ask and task files Drive was asked for the content of, in order."""
        names = {i: f["name"] for i, f in self.drive.files.items() if f["name"].startswith(("ask-", "task-"))}
        return [names[path.rsplit("/", 1)[1]] for _, path, params in self.drive.calls("GET", "/drive/v3/files/")
                if params.get("alt") == "media" and path.rsplit("/", 1)[1] in names]


@pytest.fixture
def world(hosted, google):
    return TaskSweeper(hosted, google)


def last_event(world, task_id: int) -> tuple:
    event = world.task_doc(task_id)["events"][-1]
    return event["kind"], event["payload"], event["actor"]


def finish_by_hand(world, task_id: int, status="done") -> None:
    """The task file says finished (content and marks) while its items still name their files: not the bridge's
    finish, which stamps every item (a hand edit, or a client that knows nothing of reply_files)."""
    rewrite_task(world.drive, task_id, status=status, finished_at=now_iso())
    world.drive.file(f"task-{task_id}.json")["appProperties"]["status"] = status


def task_file(world, task_id: int) -> dict:
    return world.drive.file(f"task-{task_id}.json")


def break_task_file(world, task_id: int, how: str) -> None:
    file = task_file(world, task_id)
    world.hosted.app.state.pool._cache.clear()
    if how == "corrupt":
        file["content"], file["modifiedTime"] = b"not json at all", world.drive._tick()
    elif how == "oversize":
        file["content"], file["modifiedTime"] = b" " * (300 * 1024), world.drive._tick()


def created_at(world, file_id: str) -> float:
    return epoch(world.drive.files[file_id]["createdTime"])


# ---------- the listing: one for both kinds, split by the full name and the marks ----------

def test_the_names_and_the_listing():
    assert LIST_FILES == "(name contains 'afile-' or name contains 'tfile-') and trashed = false"
    assert TASK_FILE_NAME.fullmatch("tfile-3-1-1700000000000")
    assert not TASK_FILE_NAME.fullmatch("tfile-3-1-1700000000000x") and not TASK_FILE_NAME.fullmatch("afile-3-1-1")
    marks = {"task_id": "3", "kind": "file", "file": "1", "secret": "0"}
    ours = {"id": "f1", "name": "tfile-3-1-1700000000000", "appProperties": marks}
    assert reply_files([ours]) == [(ours, 3, 1)]
    for bad in ({**marks, "ask_id": "3"}, {**marks, "kind": "voice"}, {**marks, "task_id": "4"},
                {**marks, "file": "2"}, {k: v for k, v in marks.items() if k != "kind"}):
        assert reply_files([{**ours, "appProperties": bad}]) == [], bad
    assert reply_files([{**ours, "name": "afile-3-1-1700000000000"}, {**ours, "id": None}]) == []


def test_one_listing_per_user(world):
    ask, task = world.ask(), world.task()
    world.answered(ask, (1, False))
    world.replied(task, (1, False))
    before = len(world.drive.calls("GET", "/drive/v3/files"))
    assert world.sweep()[1] == {"users": 1}
    lists = [c[2]["q"] for c in world.drive.calls("GET", "/drive/v3/files")[before:] if c[1] == "/drive/v3/files"]
    assert [q for q in lists if "file-'" in q] == [LIST_FILES]
    assert len([q for q in lists if "contains 'ask-'" in q]) == 1  # the ask and task listing, read once and shared


# ---------- (a) secret files ----------

def test_secret_file_goes_24h_after_min_of_upload_and_reply_not_before(world):
    task = world.task()
    [one] = world.replied(task, (1, True))
    world.after(task, SECRET_TTL - 60)
    assert world.sweep()[1] == {"users": 1} and world.exists(one["file_id"])
    world.after(task, SECRET_TTL)
    assert world.sweep()[1]["deleted_expired"] == 1 and not world.exists(one["file_id"])
    gone = world.stored(task)
    assert (gone["file_id"], gone["deleted_reason"], gone["downloaded_at"]) == (None, "expired", None)
    assert epoch(gone["deleted_at"]) == int(world.now)
    assert last_event(world, task) == ("file_deleted", {"id": 1, "reason": "expired"}, "allen")
    # the earlier of the two: a file uploaded 23 h before the reply (Drive saw it then too) goes an hour after it
    [two] = world.replied(task, (2, True))
    early = world.replies[task] - 23 * HOUR
    world.restamp(task, 2, uploaded_at=iso(early))
    world.drive.files[two["file_id"]]["createdTime"] = iso(early)
    world.hosted.app.state.pool._cache.clear()
    world.after(task, HOUR - 60)
    assert world.sweep()[1] == {"users": 1} and world.exists(two["file_id"])
    world.after(task, HOUR)
    assert world.sweep()[1]["deleted_expired"] == 1 and not world.exists(two["file_id"])


def test_secret_file_with_a_held_unexpired_claim_is_kept(world):
    task = world.task()
    [one] = world.replied(task, (1, True))
    world.after(task, SECRET_TTL + 5 * HOUR)  # past its 24 h and past its ceiling: a send in flight still holds it
    world.hold_task(task, one["file_id"], "held", until=world.now + 100)
    assert "deleted_expired" not in world.sweep()[1] and world.exists(one["file_id"])
    world.hold_task(task, one["file_id"], "held", until=world.now - 1)  # the lease ran out: nobody is sending
    assert world.sweep()[1]["deleted_expired"] == 1 and not world.exists(one["file_id"])


def test_secret_file_with_a_done_claim_is_deleted_at_once_as_sent(world):
    """The send went out, the Drive delete after it failed: the claim is done, the file still there."""
    task = world.task()
    [one] = world.replied(task, (1, True))
    world.after(task, 120)  # nowhere near 24 h
    world.hold_task(task, one["file_id"], "done")
    _, stats = world.sweep()
    assert stats["deleted_sent"] == 1 and stats["recorded"] == 1 and not world.exists(one["file_id"])
    gone = world.stored(task)
    # no downloaded_at: that stamp goes with a file_downloaded event, which only the route writes
    assert (gone["file_id"], gone["deleted_reason"], gone["downloaded_at"]) == (None, "sent", None)
    assert last_event(world, task) == ("file_deleted", {"id": 1, "reason": "sent"}, "allen")


def test_a_secret_item_with_downloaded_at_and_a_live_file_goes_as_sent(world):
    task = world.task()
    secret, normal = world.replied(task, (1, True), (2, False))
    for n in (1, 2):
        world.restamp(task, n, downloaded_at="2026-11-19T09:00:00.000Z")
    world.after(task, 120)
    _, stats = world.sweep()
    assert stats["deleted_sent"] == 1 and not world.exists(secret["file_id"])
    gone = world.stored(task)
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "sent")
    assert gone["downloaded_at"] == "2026-11-19T09:00:00.000Z"
    assert world.exists(normal["file_id"])  # a normal file is downloaded many times: that proves nothing


# ---------- (b) the files of finished tasks ----------

@pytest.mark.parametrize("tool", ["task_done", "task_drop"])
def test_files_of_a_finished_task_go_at_any_age_by_confirmed_marks_without_a_read(world, tool):
    task = world.task()
    ones = world.replied(task, (1, False), (2, True))
    draft = world.put_task(task, 3, secret=True)  # named by no item
    world.finish(task, tool)
    files = [one["file_id"] for one in ones] + [draft]
    assert all(world.exists(f) for f in files)  # the bridge's deletes failed: the sweep's
    finished = world.task_doc(task)
    world.hosted.app.state.pool._cache.clear()
    world.now = time.time() + 3 * DAY  # finished days ago (past HEAL_WINDOW: healing does not read it either) ...
    for file_id in files:
        world.drive.files[file_id]["createdTime"] = iso(world.now - 60)  # ... and Drive says a minute old: any age
    before = len(world.record_reads())
    _, stats = world.sweep()
    assert stats == {"users": 1, "deleted_finished": 3}
    assert not any(world.exists(f) for f in files)
    assert world.record_reads()[before:] == []  # the task file was not downloaded in this run
    assert world.task_doc(task) == finished  # the finish stamped the items already: nothing to write


def test_a_finished_mark_the_file_does_not_confirm_is_decided_by_a_read(world, monkeypatch):
    task = world.task()
    [one] = world.replied(task, (1, False))
    file = task_file(world, task)
    file["appProperties"]["status"] = "done"  # the listing says done (an index that has not caught up) ...
    drive = world.hosted.app.state.pool.client(world.sub)
    real = drive.get_meta

    def meta(file_id):
        found = real(file_id)
        if file_id == file["id"] and found is not None:  # ... the file's own metadata says it is open
            found = {**found, "appProperties": {**found["appProperties"], "status": "active"}}
        return found
    monkeypatch.setattr(drive, "get_meta", meta)
    world.hosted.app.state.pool._cache.clear()
    before = len(world.record_reads())
    assert world.sweep(now=world.after(task, 60))[1] == {"users": 1} and world.exists(one["file_id"])
    assert world.record_reads()[before:] == [f"task-{task}.json"]  # the read decided: open, it names the file


# ---------- (c) orphans ----------

def test_a_normal_orphan_goes_after_24h(world):
    task = world.task()  # open: a read says it names nothing
    unnamed = world.put_task(task, 1, uploaded=world.now - FILE_ORPHAN + 60)
    absent = world.put_task(99, 1, uploaded=world.now - FILE_ORPHAN + 60)  # no task #99 at all
    assert world.sweep()[1] == {"users": 1} and world.exists(unnamed) and world.exists(absent)
    world.now += 60
    assert world.sweep()[1] == {"users": 1, "deleted_orphan": 2}
    assert not world.exists(unnamed) and not world.exists(absent)


def test_a_secret_orphan_of_an_absent_task_goes_after_1h(world):
    absent = world.put_task(99, 1, uploaded=world.now - SECRET_ORPHAN + 60, secret=True)  # no task #99 at all
    assert world.sweep()[1] == {"users": 1} and world.exists(absent)
    world.now += 60
    assert world.sweep()[1] == {"users": 1, "deleted_orphan": 1} and not world.exists(absent)


def test_a_secret_file_an_open_task_does_not_name_is_a_draft_until_its_24h_ceiling(world):
    """A draft: the app uploads on attach, before Send, so the open task does not name the file yet."""
    task = world.task()
    world.replied(task, (1, False))  # a task with a reply naming another file
    other = world.task()  # and one with none
    drafts = [world.put_task(task, 2, uploaded=world.now, secret=True),
              world.put_task(other, 1, uploaded=world.now, secret=True)]
    start = world.now
    assert "deleted_orphan" not in world.sweep(now=start + 2 * HOUR)[1]
    assert all(world.exists(d) for d in drafts)
    assert "deleted_expired" not in world.sweep(now=start + SECRET_TTL + CLOCK_SLACK - 1)[1]
    assert world.sweep(now=start + SECRET_TTL + CLOCK_SLACK)[1]["deleted_expired"] == 2
    assert not any(world.exists(d) for d in drafts)


# ---------- (d) normal files of an open task ----------

def test_a_normal_file_of_an_open_task_goes_30_days_after_its_reply(world):
    task = world.task()
    [one] = world.replied(task, (1, False))
    world.after(task, NORMAL_TTL - DAY)
    assert world.sweep()[1] == {"users": 1} and world.exists(one["file_id"])
    world.after(task, NORMAL_TTL)
    assert world.sweep()[1] == {"users": 1, "deleted_expired": 1, "recorded": 1}
    assert not world.exists(one["file_id"])
    gone = world.stored(task)
    assert (gone["file_id"], gone["deleted_reason"]) == (None, "expired")
    assert last_event(world, task) == ("file_deleted", {"id": 1, "reason": "expired"}, "allen")


# ---------- what differs from asks: an open task may name files ----------

def test_an_open_task_that_names_a_file_takes_one_read_and_keeps_it(world):
    task = world.task()
    [one] = world.replied(task, (1, False))
    world.hosted.app.state.pool._cache.clear()
    before = len(world.record_reads())
    # two days on: an open ask's file would be an orphan by now (its marks alone say it names nothing)
    assert world.sweep(now=world.after(task, 2 * DAY))[1] == {"users": 1} and world.exists(one["file_id"])
    assert world.record_reads()[before:] == [f"task-{task}.json"]


@pytest.mark.parametrize("how", ["corrupt", "oversize"])
def test_an_unreadable_task_keeps_its_files_until_the_ceiling(world, how):
    task = world.task()
    secret, normal = world.replied(task, (1, True), (2, False))
    created = {1: created_at(world, secret["file_id"]), 2: created_at(world, normal["file_id"])}
    break_task_file(world, task, how)

    def run(moment):
        world.hosted.app.state.pool._cache.clear()
        return world.sweep(now=moment)[1]
    assert not any(k.startswith("deleted_") for k in run(world.after(task, DAY + 120)))  # past every orphan age
    assert not any(k.startswith("deleted_") for k in run(created[1] + SECRET_TTL + CLOCK_SLACK - 1))
    assert world.exists(secret["file_id"]) and world.exists(normal["file_id"])
    assert run(created[1] + SECRET_TTL + CLOCK_SLACK)["deleted_expired"] == 1  # by Drive's secret mark
    assert not world.exists(secret["file_id"]) and world.exists(normal["file_id"])
    assert not any(k.startswith("deleted_") for k in run(created[2] + NORMAL_TTL + CLOCK_SLACK - 1))
    assert run(created[2] + NORMAL_TTL + CLOCK_SLACK)["deleted_expired"] == 1
    assert not world.exists(normal["file_id"])


def test_a_cut_listing_deletes_no_orphan_of_a_task(world):
    for n in (5, 6, 7):  # ask files fill the listing first: the task file is past the cut
        world.drive.add(f"ask-{n}.json", {"id": n}, {})
    task = world.task()
    ones = world.replied(task, (1, False), (2, True))
    stranger = world.put_task(99, 1, uploaded=world.now - 3 * DAY)
    world.after(task, 2 * HOUR)  # past the secret file's hour, were the task taken for absent
    client = world.hosted.app.state.pool.client(world.sub)
    client.max_files = 3
    _, stats = world.sweep()
    assert not any(key.startswith("deleted_") for key in stats), stats
    assert world.exists(stranger) and all(world.exists(one["file_id"]) for one in ones)
    client.max_files = 5000
    assert world.sweep()[1] == {"users": 1, "deleted_orphan": 1}  # complete again: the stranger is an orphan
    assert not world.exists(stranger) and all(world.exists(one["file_id"]) for one in ones)


def test_the_read_cap_is_shared_with_asks(world, monkeypatch):
    for _ in range(2):
        world.answered(world.ask(), (1, False))
    for _ in range(2):
        world.replied(world.task(), (1, False))
    world.hosted.app.state.pool._cache.clear()
    monkeypatch.setattr(sweep_module, "USER_READS", 2)
    before = len(world.record_reads())
    _, stats = world.sweep(now=world.now + HOUR)  # each of the four takes a read to be decided
    assert stats["user_cut"] == 1
    assert len(world.record_reads()[before:]) == 2  # two reads in all, not two of each kind


# ---------- the record: the item's guard, healing ----------

def test_the_task_is_written_only_if_the_item_still_names_the_file(world):
    task = world.task()
    [one] = world.replied(task, (1, False))
    pool, at = world.hosted.app.state.pool, "2026-11-28T12:00:00.000Z"
    stored = tasks_of(world.hosted).get(task)
    assert stored["reply_pending"] is True
    doc = world.task_doc(task)
    assert sweep_module._mark_task_deleted(pool, world.sub, stored, "other-file", "expired", at) is False
    assert world.task_doc(task) == doc  # another file: nothing written
    assert sweep_module._mark_task_deleted(pool, world.sub, stored, one["file_id"], "expired", at) is True
    gone = world.stored(task)
    assert (gone["file_id"], gone["deleted_at"], gone["deleted_reason"]) == (None, at, "expired")
    event = world.task_doc(task)["events"][-1]
    assert (event["kind"], event["payload"], event["actor"], event["at"]) == (
        "file_deleted", {"id": 1, "reason": "expired"}, "allen", at)
    assert tasks_of(world.hosted).get(task)["reply_pending"] is True  # not the owner's action
    doc = world.task_doc(task)
    assert sweep_module._mark_task_deleted(pool, world.sub, stored, one["file_id"], "sent",
                                           "2026-11-28T12:00:01.000Z") is False  # already deleted: once
    assert world.task_doc(task) == doc


def test_healing_names_the_reason_it_knows(world):
    finished, sent, plain = world.task(), world.task(), world.task()
    [f] = world.replied(finished, (1, False))
    [s] = world.replied(sent, (1, True))
    [p] = world.replied(plain, (1, False))
    world.hold_task(sent, s["file_id"], "done")
    finish_by_hand(world, finished)
    for one in (f, s, p):  # the Drive files are gone; the items still name them
        del world.drive.files[one["file_id"]]
    _, stats = world.sweep()
    assert stats["healed"] == 3
    for task, reason in ((finished, "finished"), (sent, "sent"), (plain, "expired")):
        gone = world.stored(task)
        assert (gone["file_id"], gone["deleted_reason"]) == (None, reason), task
        assert last_event(world, task) == ("file_deleted", {"id": 1, "reason": reason}, "allen")
    assert world.sweep()[1] == {"users": 1}  # nothing left to heal


def test_an_item_whose_file_is_there_under_another_name_is_not_healed(world):
    task = world.task()
    other = world.drive.add("something-else", b"x", {})
    app_task_reply_files(world.drive, task, [task_item(1, file_id=other, bytes=1)])
    world.put_task(77, 1)  # a young file of the user's: the task side is looked at (and the task, changed now, healed)
    assert world.sweep()[1] == {"users": 1}
    assert world.stored(task)["file_id"] == other and world.exists(other)


# ---------- claims ----------

def test_claims_of_finished_tasks_go_first_and_lapsed_done_claims_go(world, monkeypatch):
    live = [world.task() for _ in range(3)]
    for task in live:
        world.replied(task, (1, False))  # each takes a read
    done_task = world.task()
    [d] = world.replied(done_task, (1, True))
    world.finish(done_task)
    finished_claim = world.hold_task(done_task, d["file_id"], "done")  # left in the store after the finish
    held = world.hold_task(live[0], world.stored(live[0])["file_id"], "held", until=world.now + DAY)
    sent_task = world.task()
    a, b = world.replied(sent_task, (1, True), (2, True))
    for n, one in ((1, a), (2, b)):  # both sent and deleted, the record says so
        del world.drive.files[one["file_id"]]
        world.restamp(sent_task, n, file_id=None, deleted_at=now_iso(), deleted_reason="sent",
                      downloaded_at=now_iso())
    lapsed = world.hold_task(sent_task, a["file_id"], "done", expires_at=world.now - 60)
    fresh = world.hold_task(sent_task, b["file_id"], "done", expires_at=world.now + DAY)
    world.hosted.app.state.pool._cache.clear()
    monkeypatch.setattr(sweep_module, "USER_READS", 1)
    _, stats = world.sweep(now=world.now + 60)
    assert stats["user_cut"] == 1 and stats["claims_deleted"] == 1  # the finished task's, before any read
    assert world.claim(finished_claim) is None
    assert all(world.claim(key) is not None for key in (held, lapsed, fresh))
    monkeypatch.setattr(sweep_module, "USER_READS", 100)
    _, stats = world.sweep(now=world.now + 60)
    assert stats["claims_deleted"] == 1 and world.claim(lapsed) is None  # nothing live behind it
    assert world.claim(held) is not None and world.claim(fresh) is not None


def test_a_lapsed_done_claim_of_a_task_whose_item_still_names_a_live_file_stays(world):
    task = world.task()
    [one] = world.replied(task, (1, True))
    world.after(task, 120)
    key = world.hold_task(task, one["file_id"], "done", expires_at=time.time() - 60)
    world.drive.fail_next("DELETE", f"/drive/v3/files/{one['file_id']}", 500)  # the sweep can't delete it this hour
    _, stats = world.sweep()
    assert stats["failed"] == 1 and world.exists(one["file_id"])
    assert world.claim(key) is not None and "claims_deleted" not in stats  # the claim refuses a second send
    _, stats = world.sweep()  # the next hour: the file goes as sent, and then the claim has nothing behind it
    assert stats["deleted_sent"] == 1 and not world.exists(one["file_id"]) and world.claim(key) is None


# ---------- nothing of an ask is touched for a task, nor the reverse (Review Focus 5) ----------

def test_nothing_of_an_ask_is_touched_and_the_reverse(world):
    asks = [world.ask(), world.ask()]  # asks #1 and #2
    tasks = [world.task(), world.task()]  # tasks #1 and #2: two sequences, the same numbers
    assert asks == tasks == [1, 2]
    # ask #1 closed by the app with files and a claim, task #1 open with files and a held claim
    a1 = world.answered(1, (1, False), (2, True))
    a1_claim = world.hold(1, a1[1]["file_id"], "done")
    app_action(world.drive, 1, "closed")
    t1 = world.replied(1, (1, False), (2, True))
    t1_claim = world.hold_task(1, t1[1]["file_id"], "held", until=time.time() + DAY)
    # the mirror: ask #2 answered with files and a held claim, task #2 finished, its files and a claim left over
    a2 = world.answered(2, (1, False), (2, True))
    a2_claim = world.hold(2, a2[1]["file_id"], "held", until=time.time() + DAY)
    t2 = world.replied(2, (1, False), (2, True))
    world.finish(2)
    t2_claim = world.hold_task(2, t2[1]["file_id"], "done")
    # files that look like the other kind's and are not
    old = time.time() - 3 * DAY
    both_marks = [world.put_task(2, 9, uploaded=old, ask_id="2"),  # a tfile- of finished task #2 that names an ask
                  world.put(2, 9, uploaded=old, task_id="2")]  # an afile- of ask #2 that names a task: an orphan else
    app_task_reply_files(world.drive, 1, [task_item(5, file_id=a2[0]["file_id"], bytes=100)])  # task #1 names an afile-
    ask2, task1 = world.doc(2), world.task_doc(1)
    world.hosted.app.state.pool._cache.clear()
    world.now = time.time() + 60
    _, stats = world.sweep()
    assert stats["deleted_closed"] == 2 and stats["deleted_finished"] == 2 and stats["claims_deleted"] == 2
    assert not any(world.exists(one["file_id"]) for one in (*a1, *t2))
    assert all(world.exists(one["file_id"]) for one in (*t1, *a2)) and all(world.exists(f) for f in both_marks)
    assert world.claim(a1_claim) is None and world.claim(t2_claim) is None
    assert world.claim(t1_claim) is not None and world.claim(a2_claim) is not None  # task #1's survives ask #1's close
    assert world.task_doc(1) == task1 and world.doc(2) == ask2  # nothing written into the other kind's live record
    assert {(i["file_id"], i["deleted_reason"]) for i in world.doc(1)["answer_files"]} == {(None, "closed")}


def test_an_afile_with_a_tasks_mark_is_nobodys():
    marks = {"ask_id": "3", "kind": "file", "file": "1", "secret": "0"}
    ours = {"id": "f1", "name": "afile-3-1-1700000000000", "appProperties": marks}
    assert sweep_module.answer_files([ours]) == [(ours, 3, 1)]
    assert sweep_module.answer_files([{**ours, "appProperties": {**marks, "task_id": "3"}}]) == []
