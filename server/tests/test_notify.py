import io
import json
import logging
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.notify import (
    TG_LIMIT,
    Notifier,
    SendError,
    TelegramSender,
    answer_text,
    close_text,
    handles,
    reopen_text,
)

from .conftest import ALLEN, CHAT, DENIS, HAMILTON, STRANGER, auth

BOT_TOKEN = "123456:SECRET-bot-token-value"
BOTS = {"allen": "@allen_test_bot", "hamilton": "@hamilton_test_bot"}


@pytest.fixture(autouse=True)
def bot_handles(monkeypatch):
    """Bot handles come only from env ASKS_TG_HANDLES (none are built in)."""
    monkeypatch.setenv("ASKS_TG_HANDLES", json.dumps(BOTS))


def make_ask(**overrides):
    ask = {"id": 12, "agent": "allen", "title": "Делаем ярлык?", "answer": "Да, делай", "resolution": None}
    ask.update(overrides)
    return ask


def test_answer_text_exact():
    assert answer_text(make_ask()) == (
        "@allen_test_bot Your ask #12 «Делаем ярлык?» was answered:\n"
        "Да, делай\n\n"
        'Act on it, then close it: ask_close(12, "what was done")'
    )


def test_close_text_exact():
    assert close_text(make_ask(resolution="Not relevant (Denis)")) == (
        "@allen_test_bot Your ask #12 «Делаем ярлык?» was closed: Not relevant (Denis)"
    )
    assert close_text(make_ask(resolution=None)) == "@allen_test_bot Your ask #12 «Делаем ярлык?» was closed"


@pytest.mark.parametrize("agent, handle", sorted(BOTS.items()))
def test_handle_mapping(agent, handle):
    assert handles()[agent] == handle
    assert answer_text(make_ask(agent=agent)).startswith(f"{handle} Your ask #12 «Делаем ярлык?» was answered")
    assert close_text(make_ask(agent=agent)).startswith(f"{handle} Your ask #12 «Делаем ярлык?» was closed")


def test_no_handles_are_built_in(monkeypatch):
    monkeypatch.delenv("ASKS_TG_HANDLES")
    assert handles() == {}
    assert answer_text(make_ask()).startswith("Your ask #12")


def test_reopen_text_exact():
    assert reopen_text(make_ask(), "closed") == (
        "@allen_test_bot Your ask #12 «Делаем ярлык?» was reopened: it is open again and waits for a decision"
    )
    assert reopen_text(make_ask(), "answered") == (
        "@allen_test_bot Your ask #12 «Делаем ярлык?» was reopened: the answer was withdrawn, it waits for a decision again"
    )


def test_agent_text_cannot_mention_other_bots():
    """Only the leading mention may wake a bot; @handles inside the agent's title are disarmed."""
    ask = make_ask(title="Попросить @hamilton_test_bot выкатить фикс?", answer="Да, и скажи @hamilton_test_bot")
    for text in (answer_text(ask), close_text(ask), reopen_text(ask, "closed")):
        assert text.startswith("@allen_test_bot Your ask #12 ")
        assert "«Попросить @\u2060hamilton_test_bot выкатить фикс?»" in text
        assert text.count("@hamilton_test_bot") == (1 if " was answered:" in text else 0)
    assert answer_text(ask).count("@allen_test_bot") == 1
    # the human's own words stay as they are
    assert "Да, и скажи @hamilton_test_bot" in answer_text(ask)


def test_unknown_agent_gets_no_mention():
    assert answer_text(make_ask(agent="zed")).startswith("Your ask #12 «Делаем ярлык?» was answered")
    assert close_text(make_ask(agent="zed")).startswith("Your ask #12 «Делаем ярлык?» was closed")


def test_long_answer_is_truncated_but_keeps_instruction():
    text = answer_text(make_ask(answer="я" * 4000, title="т" * 200))
    assert len(text) <= TG_LIMIT
    assert text.endswith('ask_close(12, "what was done")')


def test_api_sends_notifications(client, sender, notifier):
    a = client.post("/api/asks", json={"title": "Шить M?"}, headers=auth(ALLEN)).json()
    h = client.post("/api/asks", json={"title": "ТЗ отдать?"}, headers=auth(HAMILTON)).json()
    z = client.post("/api/asks", json={"title": "Кто я?"}, headers=auth(STRANGER)).json()

    client.post(f"/api/asks/{a['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS))
    client.post(f"/api/asks/{a['id']}/answer", json={"answer": "Нет"}, headers=auth(DENIS))  # re-answer notifies again
    client.post(f"/api/asks/{a['id']}/close", json={"resolution": "сделано"}, headers=auth(ALLEN))  # agent: silent
    client.post(f"/api/asks/{a['id']}/reopen", headers=auth(ALLEN))  # silent
    client.post(f"/api/asks/{h['id']}/close", headers=auth(DENIS))
    client.post(f"/api/asks/{h['id']}/reopen", headers=auth(DENIS))  # «Вернуть» after «Неактуально»
    client.post(f"/api/asks/{z['id']}/answer", json={"answer": "Никто"}, headers=auth(DENIS))
    client.post(f"/api/asks/{z['id']}/reopen", headers=auth(DENIS))  # the human takes the answer back
    assert notifier.flush()

    assert [chat for chat, _ in sender.sent] == [CHAT] * 6
    assert sender.texts == [
        f'@allen_test_bot Your ask #{a["id"]} «Шить M?» was answered:\nДа\n\n'
        f'Act on it, then close it: ask_close({a["id"]}, "what was done")',
        f'@allen_test_bot Your ask #{a["id"]} «Шить M?» was answered:\nНет\n\n'
        f'Act on it, then close it: ask_close({a["id"]}, "what was done")',
        f"@hamilton_test_bot Your ask #{h['id']} «ТЗ отдать?» was closed: Not relevant (Denis)",
        f"@hamilton_test_bot Your ask #{h['id']} «ТЗ отдать?» was reopened: it is open again and waits for a decision",
        f'Your ask #{z["id"]} «Кто я?» was answered:\nНикто\n\nAct on it, then close it: ask_close({z["id"]}, "what was done")',
        f"Your ask #{z['id']} «Кто я?» was reopened: the answer was withdrawn, it waits for a decision again",
    ]


def test_sender_failure_never_fails_the_request(client, sender, notifier, caplog):
    sender.fail = True
    ask = client.post("/api/asks", json={"title": "Упадёт?"}, headers=auth(ALLEN)).json()
    with caplog.at_level(logging.ERROR, logger="asks.notify"):
        response = client.post(f"/api/asks/{ask['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS))
        assert response.status_code == 200
        assert notifier.flush()
    assert "simulated Telegram outage" in caplog.text
    assert "giving up" in caplog.text
    assert response.json()["status"] == "answered"


class FlakySender:
    """Fails with the given errors first, then succeeds."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0
        self.sent = []

    def __call__(self, chat_id, text):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(text)


def test_failed_sends_are_retried(monkeypatch, caplog):
    sleeps = []
    monkeypatch.setattr("server.notify.time.sleep", sleeps.append)
    sender = FlakySender(SendError("timeout"), SendError("HTTP 429", retry_after=35), SendError("HTTP 502"))
    notifier = Notifier(True, CHAT, sender=sender)  # default schedule: 5 s, 30 s, 2 min
    with caplog.at_level(logging.WARNING, logger="asks.notify"):
        notifier.ask_answered(make_ask())
        assert notifier.flush()
    assert sender.calls == 4 and len(sender.sent) == 1
    assert sleeps == [5.0, 35.0, 120.0]  # 429 waits for Telegram's retry_after
    assert caplog.text.count("retrying in") == 3 and "giving up" not in caplog.text


def test_permanent_errors_are_not_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr("server.notify.time.sleep", sleeps.append)
    sender = FlakySender(SendError("HTTP 400: chat not found", permanent=True))
    notifier = Notifier(True, CHAT, sender=sender)
    notifier.ask_answered(make_ask())
    assert notifier.flush()
    assert sender.calls == 1 and sleeps == []


def test_telegram_http_errors_are_classified(tmp_dir, monkeypatch):
    token_file = tmp_dir / "bot-token.txt"
    token_file.write_text(BOT_TOKEN)

    def failing(code, body):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "err", {}, io.BytesIO(body.encode()))
        return urlopen

    cases = [
        (429, '{"ok":false,"error_code":429,"parameters":{"retry_after":17}}', False, 17.0),
        (502, "Bad Gateway", False, None),
        (400, '{"ok":false,"description":"Bad Request: chat not found"}', True, None),
        (401, '{"ok":false,"description":"Unauthorized"}', True, None),
    ]
    for code, body, permanent, retry_after in cases:
        monkeypatch.setattr(urllib.request, "urlopen", failing(code, body))
        with pytest.raises(SendError) as raised:
            TelegramSender(token_file)("-100", "x")
        assert (raised.value.permanent, raised.value.retry_after) == (permanent, retry_after), code
        assert BOT_TOKEN not in str(raised.value)


def test_startup_warns_when_the_bot_token_is_missing(tmp_dir, settings, caplog):
    for token_file in (None, tmp_dir / "absent.txt", tmp_dir / "empty.txt"):
        if token_file is not None and token_file.name == "empty.txt":
            token_file.write_text("\n")
        notifier = Notifier(True, CHAT, token_file=token_file)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="asks"), TestClient(create_app(settings, notifier=notifier)):
            pass
        assert "every message will fail" in caplog.text, token_file
    good = tmp_dir / "good.txt"
    good.write_text(BOT_TOKEN)
    assert Notifier(True, CHAT, token_file=good).config_problem() is None
    assert Notifier(False, CHAT, token_file=None).config_problem() is None


def test_notify_disabled_sends_nothing(settings, sender):
    notifier = Notifier(False, CHAT, sender=sender)
    with TestClient(create_app(settings, notifier=notifier)) as c:
        ask = c.post("/api/asks", json={"title": "Тихо?"}, headers=auth(ALLEN)).json()
        assert c.post(f"/api/asks/{ask['id']}/answer", json={"answer": "Да"}, headers=auth(DENIS)).status_code == 200
    assert notifier.flush() and sender.sent == []


def test_telegram_sender_posts_plain_text(tmp_dir, monkeypatch):
    token_file = tmp_dir / "bot-token.txt"
    token_file.write_text(BOT_TOKEN + "\n")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true, "result": {}}'

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, body=request.data, timeout=timeout)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    TelegramSender(token_file)("-100", "привет")
    assert captured["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = captured["body"].decode()
    assert '"chat_id": "-100"' in body and "parse_mode" not in body


def test_telegram_errors_never_contain_the_token(tmp_dir, monkeypatch, caplog):
    token_file = tmp_dir / "bot-token.txt"
    token_file.write_text(BOT_TOKEN)

    def failing_urlopen(request, timeout):
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    notifier = Notifier(True, "-100", token_file=token_file, retry_delays=(0.01,))
    with caplog.at_level(logging.DEBUG):
        notifier.ask_answered(make_ask())
        assert notifier.flush()
    assert "notify for ask #12 failed" in caplog.text
    assert BOT_TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


def test_missing_token_file_is_logged(caplog):
    notifier = Notifier(True, "-100", token_file=None)
    with caplog.at_level(logging.ERROR, logger="asks.notify"):
        notifier.ask_closed_by_denis(make_ask())
        assert notifier.flush()
    assert "ASKS_TG_TOKEN_FILE is not set" in caplog.text


def test_handles_from_env(monkeypatch):
    monkeypatch.setenv("ASKS_TG_HANDLES", '{"seo": "@seo_test_bot", "bad": "no-at"}')
    assert answer_text(make_ask(agent="seo")).startswith("@seo_test_bot Your ask #12")
    assert not answer_text(make_ask(agent="bad")).startswith("no-at")
    assert answer_text(make_ask()).startswith("Your ask #12")  # only the agents the env names


def test_malformed_handles_ignored(monkeypatch):
    monkeypatch.setenv("ASKS_TG_HANDLES", "not json")
    assert answer_text(make_ask(agent="seo")).startswith("Your ask #12")
    assert answer_text(make_ask()).startswith("Your ask #12")


def test_agent_chat_override(monkeypatch):
    monkeypatch.setenv("ASKS_TG_AGENT_CHATS", '{"aide": "-100other"}')
    sent = []
    n = Notifier(True, CHAT, sender=lambda chat, text: sent.append((chat, text)), retry_delays=())
    n.ask_answered(make_ask(agent="aide"))
    n.ask_answered(make_ask(agent="allen"))
    assert n.flush(5)
    assert [c for c, _ in sent] == ["-100other", CHAT]


def test_agent_chat_override_malformed(monkeypatch):
    monkeypatch.setenv("ASKS_TG_AGENT_CHATS", "[1,2]")
    n = Notifier(True, CHAT, sender=lambda chat, text: None, retry_delays=())
    assert n.chat_for("aide") == CHAT


def test_no_chat_configured(tmp_dir, monkeypatch, caplog):
    """There is no default chat: without ASKS_TG_CHAT startup warns and sends are skipped (logged)."""
    token_file = tmp_dir / "bot-token.txt"
    token_file.write_text(BOT_TOKEN)
    assert Notifier(True, "", token_file=token_file).config_problem() == "ASKS_TG_CHAT is not set"
    sent = []
    n = Notifier(True, "", sender=lambda chat, text: sent.append((chat, text)), retry_delays=())
    with caplog.at_level(logging.ERROR, logger="asks.notify"):
        n.ask_answered(make_ask())
        assert n.flush(5)
    assert sent == [] and "no Telegram chat" in caplog.text
    monkeypatch.setenv("ASKS_TG_AGENT_CHATS", '{"allen": "-100other"}')
    assert Notifier(True, "", token_file=token_file).config_problem() is None
    n.ask_answered(make_ask())
    assert n.flush(5) and sent and sent[0][0] == "-100other"
