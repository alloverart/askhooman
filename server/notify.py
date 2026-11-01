"""Telegram notifications (single-tenant bridge) when the human answers, closes or reopens an agent's ask.

The message goes to ASKS_TG_CHAT (or the agent's chat from ASKS_TG_AGENT_CHATS) and starts with the agent's
bot handle from ASKS_TG_HANDLES, so a bot that only sees messages mentioning it wakes up.

Messages go out from a background thread: a failure is retried a few times, then logged; it never
reaches the API caller. Plain text only (no parse_mode). The bot token is never logged.
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("asks.notify")

TG_LIMIT = 4096
RETRY_DELAYS = (5.0, 30.0, 120.0)  # seconds between attempts after a failed send
RETRY_AFTER_MAX = 600.0  # cap for Telegram's 429 retry_after


def handles() -> dict:
    """Agent slug -> bot handle from env ASKS_TG_HANDLES (JSON object {"slug": "@bot"}; none by default), so a
    new agent needs only a config change, not a code change. Values without a leading @ are skipped; a
    malformed value is logged and ignored."""
    raw = os.environ.get("ASKS_TG_HANDLES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return {str(k): str(v) for k, v in data.items() if str(v).startswith("@")}
    except ValueError as exc:
        log.error("ASKS_TG_HANDLES ignored: %s", exc)
        return {}


def agent_chats() -> dict:
    """Per-agent chat overrides from env ASKS_TG_AGENT_CHATS (JSON {"slug": "<chat id>"}): an agent that lives
    in another group gets its pings there. Malformed value: logged, ignored."""
    raw = os.environ.get("ASKS_TG_AGENT_CHATS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return {str(k): str(v).strip() for k, v in data.items() if str(v).strip()}
    except ValueError as exc:
        log.error("ASKS_TG_AGENT_CHATS ignored: %s", exc)
        return {}


def _mention(agent: str) -> str:
    handle = handles().get(agent)
    return f"{handle} " if handle else ""


def _disarm(text: str) -> str:
    """Agent-written text must not @mention (and so wake) another bot under the notifying bot's name.
    A word joiner after @ breaks Telegram's mention parsing and stays invisible."""
    return text.replace("@", "@\u2060")


def _head(ask: dict, verb: str) -> str:
    return f"{_mention(ask['agent'])}Your ask #{ask['id']} «{_disarm(ask['title'])}» was {verb}"


def answer_text(ask: dict) -> str:
    head = _head(ask, "answered") + ":\n"
    tail = f'\n\nAct on it, then close it: ask_close({ask["id"]}, "what was done")'
    answer = ask["answer"] or ""
    room = TG_LIMIT - len(head) - len(tail)
    if len(answer) > room:
        answer = answer[: room - 1] + "…"
    return head + answer + tail


def close_text(ask: dict) -> str:
    text = _head(ask, "closed")
    if ask.get("resolution"):
        text += f": {ask['resolution']}"
    return text[:TG_LIMIT]


def reopen_text(ask: dict, previous_status: str) -> str:
    if previous_status == "answered":
        return _head(ask, "reopened") + ": the answer was withdrawn, it waits for a decision again"
    return _head(ask, "reopened") + ": it is open again and waits for a decision"


class SendError(RuntimeError):
    """A failed send. permanent: retrying won't help; retry_after: seconds Telegram asked to wait."""

    def __init__(self, message: str, permanent: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


class TelegramSender:
    """Posts with the bot token read from a file at send time (so rotating the file needs no restart)."""

    def __init__(self, token_file: Path | None, timeout: float = 10.0):
        self.token_file = token_file
        self.timeout = timeout

    def _token(self) -> str:
        if self.token_file is None:
            raise SendError("ASKS_TG_TOKEN_FILE is not set", permanent=True)
        try:
            token = Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SendError(f"cannot read bot token file {self.token_file}: {exc.strerror}", permanent=True) from None
        if not token:
            raise SendError(f"bot token file {self.token_file} is empty", permanent=True)
        return token

    def problem(self) -> str | None:
        """Configuration problem that would make every send fail (checked at startup), or None."""
        try:
            self._token()
        except SendError as exc:
            return str(exc)
        return None

    def __call__(self, chat_id: str, text: str) -> None:
        token = self._token()
        body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            retry_after = None
            try:
                retry_after = float(json.loads(raw)["parameters"]["retry_after"])
            except (ValueError, KeyError, TypeError):
                pass
            # 429 (flood control) and 5xx are worth retrying; other 4xx (bad chat, revoked token) are not.
            permanent = exc.code != 429 and exc.code < 500
            message = f"Telegram HTTP {exc.code}: {raw[:300]}".replace(token, "***")
            raise SendError(message, permanent=permanent, retry_after=retry_after) from None
        except Exception as exc:
            raise SendError(f"Telegram request failed: {type(exc).__name__}: {exc}".replace(token, "***")) from None
        if not result.get("ok"):
            message = f"Telegram refused: {str(result.get('description'))[:300]}".replace(token, "***")
            raise SendError(message, permanent=True)


class Notifier:
    def __init__(self, enabled: bool, chat_id: str, sender=None, token_file: Path | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS):
        self.enabled = enabled
        self.chat_id = chat_id
        self.sender = sender or TelegramSender(token_file)
        self.retry_delays = tuple(retry_delays)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def ask_answered(self, ask: dict) -> None:
        self._submit(answer_text(ask), ask["id"], self.chat_for(ask["agent"]))

    def ask_closed_by_denis(self, ask: dict) -> None:
        self._submit(close_text(ask), ask["id"], self.chat_for(ask["agent"]))

    def ask_reopened_by_denis(self, ask: dict, previous_status: str) -> None:
        self._submit(reopen_text(ask, previous_status), ask["id"], self.chat_for(ask["agent"]))

    def chat_for(self, agent: str) -> str:
        return agent_chats().get(agent, self.chat_id)

    def config_problem(self) -> str | None:
        """Why every notification would fail (e.g. missing bot token file), for a startup warning."""
        if not self.enabled:
            return None
        if not self.chat_id and not agent_chats():
            return "ASKS_TG_CHAT is not set"
        check = getattr(self.sender, "problem", None)
        return check() if check else None

    def _submit(self, text: str, ask_id: int, chat_id: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="asks-notify", daemon=True)
                self._thread.start()
        self._queue.put((text, ask_id, chat_id or self.chat_id))

    def _worker(self) -> None:
        while True:
            text, ask_id, chat_id = self._queue.get()
            try:
                self._send(text, ask_id, chat_id)
            finally:
                self._queue.task_done()

    def _send(self, text: str, ask_id: int, chat_id: str | None = None) -> None:
        """One message, retried after network errors, Telegram 5xx and 429 (honouring retry_after).
        Messages stay in order: the next one waits until this one is sent or given up."""
        chat_id = chat_id or self.chat_id
        if not chat_id:
            log.error("notify for ask #%s skipped: no Telegram chat (set ASKS_TG_CHAT)", ask_id)
            return
        attempts = len(self.retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                self.sender(chat_id, text)
            except Exception as exc:
                # Sender errors are already scrubbed of the token; no traceback (it could hold the URL).
                if getattr(exc, "permanent", False) or attempt == attempts:
                    log.error("notify for ask #%s failed (attempt %d of %d, giving up): %s",
                              ask_id, attempt, attempts, exc)
                    return
                delay = self.retry_delays[attempt - 1]
                retry_after = getattr(exc, "retry_after", None)
                if retry_after:
                    delay = max(delay, min(float(retry_after), RETRY_AFTER_MAX))
                log.warning("notify for ask #%s failed (attempt %d of %d), retrying in %.0f s: %s",
                            ask_id, attempt, attempts, delay, exc)
                time.sleep(delay)
            else:
                log.info("notified chat %s about ask #%s", chat_id, ask_id)
                return

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued messages are processed (used by tests). True if the queue drained."""
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        return done.wait(timeout)
