import json
import logging
import os
import time

from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Settings
from server.tokens import Identity, TokenStore, bearer_token

from .conftest import ALLEN, DENIS, auth

NEW_AGENT = "ogilvy-token-0123456789abcdef"


def write(path, data):
    """Rewrite the file and push its mtime forward so the change is visible even on coarse clocks."""
    path.write_text(json.dumps(data))
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_tokens_file_reload_without_restart(client, tokens_file):
    assert client.get("/api/me", headers=auth(NEW_AGENT)).status_code == 401

    data = json.loads(tokens_file.read_text())
    data[NEW_AGENT] = {"role": "agent", "agent": "ogilvy"}
    write(tokens_file, data)
    assert client.get("/api/me", headers=auth(NEW_AGENT)).json() == {"role": "agent", "agent": "ogilvy"}

    del data[ALLEN]
    write(tokens_file, data)
    assert client.get("/api/me", headers=auth(ALLEN)).status_code == 401
    assert client.get("/api/me", headers=auth(DENIS)).status_code == 200


def test_broken_file_keeps_previous_tokens_briefly_then_fails_closed(tmp_dir, caplog):
    path = tmp_dir / "t.json"
    write(path, {DENIS: {"role": "denis"}, ALLEN: {"role": "agent", "agent": "allen"}})
    store = TokenStore(path, broken_grace=0.3)
    assert store.identify(DENIS) == Identity("denis")

    # e.g. a leaked token deleted by hand, leaving a trailing comma
    path.write_text(json.dumps({DENIS: {"role": "denis"}})[:-1] + ', }')
    os.utime(path, ns=(0, os.stat(path).st_mtime_ns + 4_000_000_000))
    with caplog.at_level(logging.WARNING, logger="asks.tokens"):
        assert store.identify(ALLEN) == Identity("agent", "allen")  # grace: maybe still being written
        time.sleep(0.4)
        assert store.identify(ALLEN) is None  # still broken: nothing is accepted
        assert store.identify(DENIS) is None
    assert "no tokens accepted until it is fixed" in caplog.text
    assert DENIS not in caplog.text and ALLEN not in caplog.text

    write(path, {DENIS: {"role": "denis"}})  # fixed: picked up at once
    assert store.identify(DENIS) == Identity("denis")
    assert store.identify(ALLEN) is None

    path.unlink()
    assert store.identify(DENIS) is None


def test_bad_entries_are_skipped_and_never_logged(tmp_dir, caplog):
    path = tmp_dir / "t.json"
    short = "tiny-tok"
    write(path, {
        short: {"role": "denis"},
        "agent-without-slug-0123456789": {"role": "agent"},
        "agent-with-bad-slug-0123456789": {"role": "agent", "agent": "Allen Kim"},
        "unknown-role-token-0123456789": {"role": "admin"},
        ALLEN: {"role": "agent", "agent": "allen"},
    })
    with caplog.at_level(logging.INFO, logger="asks.tokens"):
        store = TokenStore(path)
        assert store.identify(ALLEN) == Identity("agent", "allen")
        assert store.identify(short) is None
        assert store.identify("agent-with-bad-slug-0123456789") is None
    assert "1 entries" in caplog.text
    for token in (short, ALLEN, "agent-with-bad-slug-0123456789"):
        assert token not in caplog.text


def test_unset_tokens_path_rejects_everything(tmp_dir, notifier):
    settings = Settings(db_path=tmp_dir / "a.db", tokens_path=None, web_dir=tmp_dir / "web")
    with TestClient(create_app(settings, notifier=notifier)) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/me", headers=auth(DENIS)).status_code == 401


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer   abc  ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer") is None
    assert bearer_token("") is None
    assert bearer_token(None) is None


def test_missing_file_is_reported_once(tmp_dir, caplog):
    store = TokenStore(tmp_dir / "absent.json")
    with caplog.at_level(logging.WARNING, logger="asks.tokens"):
        assert store.identify(DENIS) is None
        assert store.identify(DENIS) is None
    assert caplog.text.count("not found") == 1
