"""Runtime configuration, read from environment variables.

ASKS_MODE           "hosted" = the multi-tenant bridge (SPEC v4; its settings: server/hosted/config.py);
                    anything else (default) = the single-tenant bridge configured below
ASKS_DB             SQLite file (default: <asks>/data/asks.db)
ASKS_TOKENS         tokens JSON file (no default: without it every request is 401)
ASKS_WEB_DIR        static web app directory (default: <asks>/web)
ASKS_NOTIFY         "1" enables Telegram notifications (default off)
ASKS_TG_TOKEN_FILE  file with the Telegram bot token that posts the notifications
ASKS_TG_CHAT        Telegram chat id to post to (no default: notifications need it)
ASKS_TG_HANDLES     JSON {"<agent slug>": "@bot_handle"}: whom to @mention per agent (default none)
ASKS_TG_AGENT_CHATS JSON {"<agent slug>": "<chat id>"}: per-agent chat overrides (default none)
ASKS_DRIVE          "1" mirrors every ask into the linked Google Drive appDataFolder (default off)
ASKS_DRIVE_CLIENT   JSON file with the Desktop OAuth client's client_id and client_secret
ASKS_DRIVE_TOKEN    JSON file with the refresh token ({"refresh_token": ...}; SPEC.md, "Drive storage")
ASKS_DRIVE_POLL     seconds between full Drive syncs (default 15)
"""

import os
from dataclasses import dataclass
from pathlib import Path

ASKS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TG_CHAT = ""  # no default chat: set ASKS_TG_CHAT
DEFAULT_DRIVE_POLL = 15.0


def _path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _seconds(value: str | None, default: float) -> float:
    try:
        seconds = float(value) if value and value.strip() else default
    except ValueError:
        return default
    return seconds if seconds > 0 else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    tokens_path: Path | None
    web_dir: Path
    notify: bool = False
    tg_token_file: Path | None = None
    tg_chat: str = DEFAULT_TG_CHAT
    drive: bool = False
    drive_client_file: Path | None = None
    drive_token_file: Path | None = None
    drive_poll: float = DEFAULT_DRIVE_POLL
    mode: str = "single"  # "single" (single-tenant, default) or "hosted"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            db_path=_path(env.get("ASKS_DB")) or ASKS_DIR / "data" / "asks.db",
            tokens_path=_path(env.get("ASKS_TOKENS")),
            web_dir=_path(env.get("ASKS_WEB_DIR")) or ASKS_DIR / "web",
            notify=_flag(env.get("ASKS_NOTIFY")),
            tg_token_file=_path(env.get("ASKS_TG_TOKEN_FILE")),
            tg_chat=env.get("ASKS_TG_CHAT", "").strip() or DEFAULT_TG_CHAT,
            drive=_flag(env.get("ASKS_DRIVE")),
            drive_client_file=_path(env.get("ASKS_DRIVE_CLIENT")),
            drive_token_file=_path(env.get("ASKS_DRIVE_TOKEN")),
            drive_poll=_seconds(env.get("ASKS_DRIVE_POLL"), DEFAULT_DRIVE_POLL),
            mode="hosted" if env.get("ASKS_MODE", "").strip().lower() == "hosted" else "single",
        )
