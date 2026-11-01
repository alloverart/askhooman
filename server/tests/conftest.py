import json
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Settings
from server.notify import Notifier

from .hosted_support import google, hosted  # noqa: F401  (fixtures of the hosted-bridge tests)

ASKS_DIR = Path(__file__).resolve().parents[2]
TMP_ROOT = ASKS_DIR / "_tmp"

DENIS = "denis-token-0123456789abcdef"
ALLEN = "allen-token-0123456789abcdef"
HAMILTON = "hamilton-token-0123456789abc"
STRANGER = "stranger-token-0123456789abc"  # agent slug without a Telegram handle

TOKENS = {
    DENIS: {"role": "denis"},
    ALLEN: {"role": "agent", "agent": "allen"},
    HAMILTON: {"role": "agent", "agent": "hamilton"},
    STRANGER: {"role": "agent", "agent": "zed"},
}
CHAT = "-100test"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def __call__(self, chat_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("simulated Telegram outage")
        self.sent.append((chat_id, text))

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


@pytest.fixture
def tmp_dir():
    path = TMP_ROOT / f"t-{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tokens_file(tmp_dir) -> Path:
    path = tmp_dir / "tokens.json"
    path.write_text(json.dumps(TOKENS))
    return path


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def notifier(sender) -> Notifier:
    return Notifier(True, CHAT, sender=sender, retry_delays=(0.01, 0.01))


@pytest.fixture
def settings(tmp_dir, tokens_file) -> Settings:
    return Settings(db_path=tmp_dir / "data" / "asks.db", tokens_path=tokens_file, web_dir=tmp_dir / "web")


@pytest.fixture
def app(settings, notifier):
    return create_app(settings, notifier=notifier)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(app):
    """The real app under uvicorn on a free localhost port, in a background thread."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
