import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Settings
from server.core import AskService
from server.db import Database
from server.tokens import Identity

from .conftest import ALLEN, ASKS_DIR, DENIS, HAMILTON, auth

ASK_KEYS = {
    "id", "agent", "title", "context", "options", "links", "priority", "ref", "status", "answer",
    "answered_at", "resolution", "closed_by", "closed_at", "created_at", "updated_at",
}


def create(client, token=ALLEN, **fields):
    fields.setdefault("title", "Делаем?")
    response = client.post("/api/asks", json=fields, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


def answer(client, ask_id, text="Да", token=DENIS):
    return client.post(f"/api/asks/{ask_id}/answer", json={"answer": text}, headers=auth(token))


def close(client, ask_id, token=ALLEN, resolution=None):
    body = {} if resolution is None else {"resolution": resolution}
    return client.post(f"/api/asks/{ask_id}/close", json=body, headers=auth(token))


def reopen(client, ask_id, token=ALLEN):
    return client.post(f"/api/asks/{ask_id}/reopen", headers=auth(token))


# ---------- auth ----------

def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer nope-not-a-real-token-at-all"},
    {"Authorization": f"Basic {ALLEN}"},
])
def test_missing_or_unknown_token_is_401(client, headers):
    for method, path in [("get", "/api/me"), ("get", "/api/asks"), ("post", "/api/asks"), ("get", "/api/asks/1")]:
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 401
        assert isinstance(response.json()["detail"], str)
        assert response.headers["www-authenticate"] == "Bearer"


def test_me(client):
    assert client.get("/api/me", headers=auth(DENIS)).json() == {"role": "denis", "agent": None}
    assert client.get("/api/me", headers=auth(ALLEN)).json() == {"role": "agent", "agent": "allen"}
    assert client.get("/api/me", headers={"Authorization": f"bearer {HAMILTON}"}).json()["agent"] == "hamilton"


def test_role_and_owner_checks_are_403(client):
    ask = create(client)
    assert client.post("/api/asks", json={"title": "x?"}, headers=auth(DENIS)).status_code == 403
    assert answer(client, ask["id"], token=ALLEN).status_code == 403
    assert client.patch(f"/api/asks/{ask['id']}", json={"title": "y?"}, headers=auth(HAMILTON)).status_code == 403
    assert client.patch(f"/api/asks/{ask['id']}", json={"title": "y?"}, headers=auth(DENIS)).status_code == 403
    assert close(client, ask["id"], token=HAMILTON).status_code == 403
    assert answer(client, ask["id"]).status_code == 200
    response = reopen(client, ask["id"], token=HAMILTON)
    assert response.status_code == 403
    assert "allen" in response.json()["detail"]
    # nothing changed by the rejected calls
    assert client.get(f"/api/asks/{ask['id']}", headers=auth(ALLEN)).json()["status"] == "answered"


def test_agent_cannot_act_as_another_agent(client):
    response = client.post("/api/asks", json={"title": "Чей?", "agent": "hamilton"}, headers=auth(ALLEN))
    assert response.status_code == 422
    ask = create(client, token=HAMILTON, title="Чей?")
    assert ask["agent"] == "hamilton"
    assert client.get("/api/asks?agent=allen", headers=auth(ALLEN)).json()["asks"] == []


# ---------- create / get / list ----------

def test_create_returns_full_ask(client):
    ask = create(
        client,
        title="  ePost: делать тестовый ярлык?  ",
        context="Строка 1\r\nСтрока 2 https://example.com/x",
        options=["Да, делай", "Подождём"],
        links=[{"title": "Doc", "url": "https://docs.google.com/document/d/1"}, {"url": "http://a.b/c"}],
        priority="urgent",
        ref="TG 5487",
    )
    assert set(ask) == ASK_KEYS
    assert ask["agent"] == "allen"
    assert ask["title"] == "ePost: делать тестовый ярлык?"
    assert ask["context"] == "Строка 1\nСтрока 2 https://example.com/x"
    assert ask["options"] == ["Да, делай", "Подождём"]
    assert ask["links"] == [
        {"title": "Doc", "url": "https://docs.google.com/document/d/1"},
        {"title": "http://a.b/c", "url": "http://a.b/c"},
    ]
    assert (ask["priority"], ask["ref"], ask["status"]) == ("urgent", "TG 5487", "open")
    assert ask["answer"] is None and ask["closed_at"] is None
    assert ask["created_at"].endswith("Z") and ask["created_at"] == ask["updated_at"]


def test_create_defaults_use_empty_arrays(client):
    ask = create(client, title="Минимальный?")
    assert ask["options"] == [] and ask["links"] == []
    assert ask["priority"] == "normal"
    assert ask["context"] is None and ask["ref"] is None


def test_get_includes_events_and_404(client):
    ask = create(client, options=["a"])
    data = client.get(f"/api/asks/{ask['id']}", headers=auth(HAMILTON)).json()
    assert data["events"][0]["kind"] == "created"
    assert data["events"][0]["actor"] == "allen"
    assert data["events"][0]["payload"]["options"] == ["a"]
    response = client.get("/api/asks/999", headers=auth(ALLEN))
    assert response.status_code == 404
    assert response.json() == {"detail": "no ask #999"}
    assert answer(client, 999).status_code == 404


def test_list_ordering_and_counts(client):
    a1 = create(client, title="open normal 1")
    a2 = create(client, title="open urgent", priority="urgent")
    a3 = create(client, title="open normal 2", token=HAMILTON)
    a4 = create(client, title="to answer first")
    a5 = create(client, title="to answer second", token=HAMILTON)
    a6 = create(client, title="to close first")
    a7 = create(client, title="to close second")
    a8 = create(client, title="urgent but later", priority="urgent")

    assert answer(client, a5["id"]).status_code == 200
    time.sleep(0.01)
    assert answer(client, a4["id"]).status_code == 200
    assert close(client, a7["id"]).status_code == 200
    time.sleep(0.01)
    assert close(client, a6["id"]).status_code == 200

    def ids(query="", token=ALLEN):
        data = client.get(f"/api/asks{query}", headers=auth(token)).json()
        return [a["id"] for a in data["asks"]], data["counts"]

    active, counts = ids()
    assert active == [a2["id"], a8["id"], a1["id"], a3["id"], a4["id"], a5["id"]]
    assert counts == {"open": 4, "answered": 2, "closed": 2}

    everything, _ = ids("?status=all", token=DENIS)
    assert everything == active + [a6["id"], a7["id"]]
    assert ids("?status=open")[0] == [a2["id"], a8["id"], a1["id"], a3["id"]]
    assert ids("?status=answered")[0] == [a4["id"], a5["id"]]
    assert ids("?status=closed")[0] == [a6["id"], a7["id"]]
    assert ids("?status=closed&limit=1")[0] == [a6["id"]]
    assert ids("?status=all&limit=3")[0] == [a2["id"], a8["id"], a1["id"]]

    hamilton_ids, hamilton_counts = ids("?agent=hamilton&status=all")
    assert hamilton_ids == [a3["id"], a5["id"]]
    assert hamilton_counts == {"open": 1, "answered": 1, "closed": 0}
    # counts ignore status and limit
    assert ids("?status=closed&limit=1")[1] == {"open": 4, "answered": 2, "closed": 2}
    assert ids("?agent=nobody")[1] == {"open": 0, "answered": 0, "closed": 0}


def test_seed_file_is_valid_input(client):
    # seed/ — живые вопросы от 31.10.2026, в репозиторий не кладутся (01.11.2026):
    # тест идёт только там, где файл есть
    path = ASKS_DIR / "seed" / "seed_3110.json"
    if not path.exists():
        pytest.skip("seed/ is local data, not in the repository")
    seed = json.loads(path.read_text())
    for item in seed:
        create(client, **item)
    data = client.get("/api/asks", headers=auth(DENIS)).json()
    assert data["counts"]["open"] == len(seed) == 11


# ---------- transitions ----------

def test_edit_open_ask_records_changes(client):
    ask = create(client, title="Старый?", options=["a", "b"], ref="TG 1")
    response = client.patch(
        f"/api/asks/{ask['id']}",
        json={"title": "Новый?", "options": ["a", "b"], "context": "контекст", "ref": "", "priority": "urgent"},
        headers=auth(ALLEN),
    )
    assert response.status_code == 200
    edited = response.json()
    assert (edited["title"], edited["context"], edited["ref"], edited["priority"]) == ("Новый?", "контекст", None, "urgent")
    assert edited["updated_at"] >= ask["updated_at"]
    event = client.get(f"/api/asks/{ask['id']}", headers=auth(ALLEN)).json()["events"][-1]
    assert event["kind"] == "edited"
    assert event["payload"]["changes"] == {
        "title": {"from": "Старый?", "to": "Новый?"},
        "context": {"from": None, "to": "контекст"},
        "ref": {"from": "TG 1", "to": None},
        "priority": {"from": "normal", "to": "urgent"},
    }
    # an empty patch changes nothing and records nothing
    assert client.patch(f"/api/asks/{ask['id']}", json={}, headers=auth(ALLEN)).json()["title"] == "Новый?"
    assert len(client.get(f"/api/asks/{ask['id']}", headers=auth(ALLEN)).json()["events"]) == 2


def test_patch_null_means_unchanged(client):
    """Same rule as MCP ask_update: null leaves a field alone; "" / [] clear it."""
    ask = create(client, title="t?", context="c", options=["a", "b"], links=[{"url": "https://x.y"}],
                 priority="urgent", ref="TG 1")
    nulls = {"title": "t2?", "context": None, "options": None, "links": None, "priority": None, "ref": None}
    edited = client.patch(f"/api/asks/{ask['id']}", json=nulls, headers=auth(ALLEN)).json()
    assert edited["title"] == "t2?"
    assert (edited["context"], edited["options"], edited["priority"], edited["ref"]) == ("c", ["a", "b"], "urgent", "TG 1")
    assert edited["links"] == ask["links"]
    assert client.patch(f"/api/asks/{ask['id']}", json={"title": None}, headers=auth(ALLEN)).json()["title"] == "t2?"
    cleared = client.patch(f"/api/asks/{ask['id']}", json={"context": "", "options": [], "links": [], "ref": ""},
                           headers=auth(ALLEN)).json()
    assert (cleared["context"], cleared["options"], cleared["links"], cleared["ref"]) == (None, [], [], None)


def test_answer_and_reanswer(client, sender, notifier):
    ask = create(client, options=["Да", "Нет"])
    first = answer(client, ask["id"], "Да").json()
    assert first["status"] == "answered" and first["answer"] == "Да"
    assert first["answered_at"].endswith("Z")
    time.sleep(0.01)
    second = answer(client, ask["id"], "  Нет, подожди  ").json()
    assert second["answer"] == "Нет, подожди"
    assert second["answered_at"] > first["answered_at"]
    events = client.get(f"/api/asks/{ask['id']}", headers=auth(ALLEN)).json()["events"]
    assert [e["kind"] for e in events] == ["created", "answered", "answered"]
    assert events[2]["payload"] == {"answer": "Нет, подожди", "previous_answer": "Да"}
    assert events[2]["actor"] == "denis"
    assert notifier.flush()
    assert len(sender.sent) == 2

    # the same answer again (e.g. the highlighted option tapped in «Изменить ответ») is a no-op
    again = answer(client, ask["id"], "Нет, подожди").json()
    assert again["answered_at"] == second["answered_at"]
    assert len(client.get(f"/api/asks/{ask['id']}", headers=auth(ALLEN)).json()["events"]) == 3
    assert notifier.flush()
    assert len(sender.sent) == 2


def test_close_by_agent_and_by_denis(client):
    a = create(client)
    answer(client, a["id"])
    closed = close(client, a["id"], resolution="Сделала ярлык").json()
    assert (closed["status"], closed["closed_by"], closed["resolution"]) == ("closed", "allen", "Сделала ярлык")
    assert closed["answer"] == "Да"  # the answer stays visible on a closed ask

    b = create(client)
    dismissed = close(client, b["id"], token=DENIS).json()
    assert (dismissed["closed_by"], dismissed["resolution"]) == ("denis", "Not relevant (Denis)")

    c = create(client)
    assert close(client, c["id"], token=DENIS, resolution="сам сделал").json()["resolution"] == "сам сделал"
    d = create(client)
    no_body = client.post(f"/api/asks/{d['id']}/close", headers=auth(ALLEN))
    assert no_body.status_code == 200 and no_body.json()["resolution"] is None


def test_reopen_clears_answer_and_keeps_history(client):
    ask = create(client)
    answer(client, ask["id"], "Да")
    close(client, ask["id"], resolution="готово")
    reopened = reopen(client, ask["id"], token=DENIS).json()
    assert reopened["status"] == "open"
    for field in ("answer", "answered_at", "resolution", "closed_by", "closed_at"):
        assert reopened[field] is None
    events = client.get(f"/api/asks/{ask['id']}", headers=auth(ALLEN)).json()["events"]
    assert [e["kind"] for e in events] == ["created", "answered", "closed", "reopened"]
    assert events[-1]["payload"]["answer"] == "Да"
    assert events[-1]["payload"]["resolution"] == "готово"
    assert events[-1]["payload"]["previous_status"] == "closed"
    # answered -> open by the owner agent
    answer(client, ask["id"], "Нет")
    assert reopen(client, ask["id"]).json()["status"] == "open"


@pytest.mark.parametrize("action", ["answer", "close", "reopen", "edit"])
def test_invalid_transitions_are_409(client, action):
    ask = create(client)
    if action == "reopen":
        response = reopen(client, ask["id"])  # already open
    else:
        if action == "edit":
            answer(client, ask["id"])  # edit is allowed only while open
        else:
            close(client, ask["id"])
        response = {
            "answer": lambda: answer(client, ask["id"]),
            "close": lambda: close(client, ask["id"]),
            "edit": lambda: client.patch(f"/api/asks/{ask['id']}", json={"title": "x?"}, headers=auth(ALLEN)),
        }[action]()
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)


# ---------- validation ----------

@pytest.mark.parametrize("body", [
    {},
    {"title": ""},
    {"title": "   "},
    {"title": "x" * 201},
    {"title": "ok?", "context": "x" * 4001},
    {"title": "ok?", "options": ["1", "2", "3", "4", "5", "6", "7"]},
    {"title": "ok?", "options": ["x" * 81]},
    {"title": "ok?", "options": [""]},
    {"title": "ok?", "options": "Да"},
    {"title": "ok?", "links": [{"title": "x", "url": "ftp://example.com/file"}]},
    {"title": "ok?", "links": [{"title": "x", "url": "javascript:alert(1)"}]},
    {"title": "ok?", "links": [{"title": "x", "url": "https://"}]},
    {"title": "ok?", "links": [{"title": "x"}]},
    {"title": "ok?", "links": [{"url": f"https://e.com/{i}"} for i in range(11)]},
    {"title": "ok?", "priority": "high"},
    {"title": "ok?", "ref": "x" * 101},
    {"title": "ok?", "colour": "red"},
])
def test_create_validation_is_422(client, body):
    response = client.post("/api/asks", json=body, headers=auth(ALLEN))
    assert response.status_code == 422, body
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_limits_are_inclusive(client):
    ask = create(
        client,
        title="x" * 200,
        context="y" * 4000,
        options=[str(i) * 80 for i in range(6)],
        links=[{"title": "t", "url": f"https://e.com/{i}"} for i in range(10)],
        ref="r" * 100,
    )
    assert len(ask["title"]) == 200 and len(ask["options"]) == 6 and len(ask["links"]) == 10


def test_other_validation_errors_are_422(client):
    ask = create(client)
    checks = [
        client.patch(f"/api/asks/{ask['id']}", json={"title": ""}, headers=auth(ALLEN)),
        client.patch(f"/api/asks/{ask['id']}", json={"options": ["x" * 81]}, headers=auth(ALLEN)),
        answer(client, ask["id"], ""),
        answer(client, ask["id"], "x" * 4001),
        client.post(f"/api/asks/{ask['id']}/answer", json={}, headers=auth(DENIS)),
        client.post(f"/api/asks/{ask['id']}/close", json={"resolution": "x" * 1001}, headers=auth(ALLEN)),
        client.get("/api/asks?status=pending", headers=auth(ALLEN)),
        client.get("/api/asks?limit=0", headers=auth(ALLEN)),
        client.get("/api/asks?limit=abc", headers=auth(ALLEN)),
    ]
    for response in checks:
        assert response.status_code == 422, response.text
        assert isinstance(response.json()["detail"], str)
    assert answer(client, ask["id"], "x" * 4000).status_code == 200

    broken = client.post(f"/api/asks/{ask['id']}/answer", content=b"{oops",
                         headers={**auth(DENIS), "Content-Type": "application/json"})
    assert broken.status_code == 422
    assert broken.json() == {"detail": "request body is not valid JSON"}


def test_control_characters_are_dropped(client):
    """Agent text ends up in terminals (CLI): escape sequences must not survive. \n and \t do."""
    ask = create(client, title="t\u001b]0;pwned\u0007?", context="a\tb\r\nc\rd\u009b2J\x00",
                 options=["\u001b[31mДа"], links=[{"title": "x\u0008y", "url": "https://e.com/\u001b"}],
                 ref="TG\u007f 1")
    assert ask["title"] == "t]0;pwned?"
    assert ask["context"] == "a\tb\nc\nd2J"
    assert ask["options"] == ["[31mДа"]
    assert ask["links"] == [{"title": "xy", "url": "https://e.com/"}]
    assert ask["ref"] == "TG 1"
    assert answer(client, ask["id"], "ок\u001b[2K").json()["answer"] == "ок[2K"
    response = client.post("/api/asks", json={"title": "\u001b\u0007"}, headers=auth(ALLEN))
    assert response.status_code == 422  # nothing left of the title


def test_unpaired_surrogate_is_422_not_500(client):
    body = b'{"title": "\\ud800?"}'
    response = client.post("/api/asks", content=body, headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 422
    assert "Unicode" in response.json()["detail"]


@pytest.mark.parametrize("ask_id", ["0", "-5", "9223372036854775807", "9223372036854775808", "99999999999999999999"])
def test_out_of_range_ids_are_404(client, ask_id):
    for method, path, body in [("get", "", None), ("post", "/answer", {"answer": "x"}), ("post", "/reopen", None)]:
        kwargs = {"headers": auth(DENIS)}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(f"/api/asks/{ask_id}{path}", **kwargs)
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": f"no ask #{int(ask_id)}"}


def test_unexpected_errors_keep_the_json_contract(app, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(app.state.service, "list", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/asks", headers=auth(ALLEN))
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error (RuntimeError)"}


# ---------- request guard ----------

def test_api_token_is_checked_before_the_body_is_read(client):
    response = client.post("/api/asks", content=b"{oops", headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}
    response = client.post("/api/asks", content=b"{oops", headers={**auth("nope-0123456789abcdef"),
                                                                   "Content-Type": "application/json"})
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


def test_large_bodies_are_413(client):
    from server.app import MAX_BODY

    big = b'{"title": "' + b"x" * MAX_BODY + b'"}'
    response = client.post("/api/asks", content=big, headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 413 and "too large" in response.json()["detail"]

    def chunks():  # no Content-Length: sent chunked
        yield b'{"title": "'
        for _ in range(MAX_BODY // 4096 + 1):
            yield b"x" * 4096
        yield b'"}'

    response = client.post("/api/asks", content=chunks(), headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 413
    # the same path still works for a normal-sized chunked body
    small = iter([b'{"title": ', b'"chunked?"}'])
    response = client.post("/api/asks", content=small, headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 201 and response.json()["title"] == "chunked?"
    # the largest valid ask, every character \u-escaped by json.dumps, still fits
    full = {
        "title": "я" * 200, "context": "я" * 4000, "options": ["я" * 80] * 6, "ref": "я" * 100,
        "links": [{"title": "я" * 200, "url": "https://e.com/" + "a" * 1980} for _ in range(10)],
    }
    body = json.dumps(full).encode()
    assert len(body) < MAX_BODY
    response = client.post("/api/asks", content=body, headers={**auth(ALLEN), "Content-Type": "application/json"})
    assert response.status_code == 201, response.text


def test_no_api_docs(client):
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json"):
        assert client.get(path).status_code == 404, path


# ---------- static web ----------

def test_web_app_served_at_root(tmp_dir, settings, notifier):
    web = tmp_dir / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Ждёт решения</title>", encoding="utf-8")
    (web / "manifest.webmanifest").write_text("{}")
    with TestClient(create_app(settings, notifier=notifier)) as c:
        root = c.get("/")
        assert root.status_code == 200 and "Ждёт решения" in root.text
        assert c.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
        assert c.get("/api/me", headers=auth(ALLEN)).status_code == 200
        # browsers must revalidate, so a redeploy is picked up at once; the ETag keeps that a cheap 304
        assert root.headers["cache-control"] == "no-cache"
        again = c.get("/", headers={"If-None-Match": root.headers["etag"]})
        assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"


def test_api_works_without_web_dir(client):
    assert client.get("/").status_code == 404
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/asks", headers=auth(ALLEN)).status_code == 200


def test_web_dir_from_env(tmp_dir, tokens_file, monkeypatch):
    web = tmp_dir / "elsewhere"
    web.mkdir()
    (web / "index.html").write_text("custom web")
    monkeypatch.setenv("ASKS_WEB_DIR", str(web))
    monkeypatch.setenv("ASKS_DB", str(tmp_dir / "env.db"))
    monkeypatch.setenv("ASKS_TOKENS", str(tokens_file))
    monkeypatch.delenv("ASKS_NOTIFY", raising=False)
    env_app = create_app()
    assert env_app.state.notifier.enabled is False
    with TestClient(env_app) as c:
        assert c.get("/").text == "custom web"
        assert c.get("/api/me", headers=auth(DENIS)).json()["role"] == "denis"
    assert (tmp_dir / "env.db").exists()


def test_settings_from_env_defaults():
    s = Settings.from_env({})
    assert s.tokens_path is None and s.notify is False and s.tg_chat == ""
    assert s.web_dir == ASKS_DIR / "web"
    s = Settings.from_env({"ASKS_NOTIFY": "1", "ASKS_TG_CHAT": "-42"})
    assert s.notify is True and s.tg_chat == "-42"


# ---------- concurrency ----------

def test_concurrent_writes_from_threads(tmp_dir):
    db = Database(tmp_dir / "c.db")
    db.init()
    service = AskService(db)
    allen, denis = Identity("agent", "allen"), Identity("denis")
    errors = []

    def worker(n):
        try:
            for i in range(10):
                ask = service.create(allen, {"title": f"t{n}-{i}?"})
                service.answer(denis, ask["id"], "ok")
                service.close(allen, ask["id"], "done")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert service.list("all", limit=1000)["counts"] == {"open": 0, "answered": 0, "closed": 80}
