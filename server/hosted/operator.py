"""A ping to the operator's Telegram chat when a new external user signs up (SPEC "Operator stats"). Optional
(ASKS_OPERATOR_TELEGRAM_CHAT + ASKS_OPERATOR_TELEGRAM_TOKEN); it never holds up a sign-in: it is sent from a
background thread and a failure is only logged. No email or other personal data goes to Telegram: who signed up
is on the admin page."""

import logging
import threading
from collections.abc import Callable

import httpx

log = logging.getLogger("asks.operator")


def telegram_sender(token: str, http: httpx.Client) -> Callable[[str, str], None]:
    def send(chat: str, text: str) -> None:
        response = http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        if response.status_code != 200:
            raise RuntimeError(f"Telegram answered {response.status_code}")
    return send


def _in_background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="operator-ping", daemon=True).start()


class OperatorPing:
    def __init__(self, send: Callable[[str, str], None] | None, chat: str | None,
                 internal: Callable[[str | None], bool], app_url: str,
                 run: Callable[[Callable[[], None]], None] = _in_background):
        self.send, self.chat, self.internal, self.app_url, self.run = send, chat, internal, app_url, run

    def new_user(self, user: dict) -> None:
        email, send, chat = user.get("email"), self.send, self.chat
        if not send or not chat or self.internal(email):
            return
        text = f"🆕 AskHooman: a new user signed up\n{self.app_url}/#/admin"

        def deliver() -> None:
            try:
                send(chat, text)
            except Exception as exc:  # the token never reaches the log: only the exception's type
                log.warning("operator ping failed: %s", type(exc).__name__)
        self.run(deliver)
