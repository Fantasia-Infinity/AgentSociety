from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE env files without overriding process variables.

    With no argument, loads the legacy `.env.wechatd` first and then the
    private `.private/env/wechatd.env`, so the private copy wins.
    """

    candidates = (
        [path]
        if path is not None
        else [Path(".env.wechatd"), Path(".private/env/wechatd.env")]
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value if value else default


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    )


@dataclass(frozen=True, slots=True)
class WechatdSettings:
    account_id: str
    http_host: str
    http_port: int
    http_token: str
    driver: str
    listen_chats: tuple[str, ...]
    bot_mention: str
    wechat_poll_interval_seconds: float
    send_min_interval_seconds: float
    max_request_bytes: int
    state_db: Path

    @classmethod
    def from_env(cls) -> "WechatdSettings":
        _load_env_file()
        settings = cls(
            account_id=_required("WECHATD_ACCOUNT_ID"),
            http_host=_optional("WECHATD_HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("WECHATD_HTTP_PORT", "8742")),
            http_token=os.environ.get("WECHATD_HTTP_TOKEN", "").strip(),
            driver=os.environ.get("WECHAT_DRIVER", "mock").strip().lower(),
            listen_chats=_csv("WECHAT_LISTEN_CHATS"),
            bot_mention=os.environ.get("WECHAT_BOT_MENTION", "").strip(),
            wechat_poll_interval_seconds=float(
                os.environ.get("WECHAT_POLL_INTERVAL_SECONDS", "1")
            ),
            send_min_interval_seconds=float(
                os.environ.get("WECHATD_SEND_MIN_INTERVAL_SECONDS", "1")
            ),
            max_request_bytes=int(
                os.environ.get("WECHATD_MAX_REQUEST_BYTES", "65536")
            ),
            state_db=Path(
                os.environ.get(
                    "WECHATD_STATE_DB", ".private/state/wechatd-state.sqlite3"
                )
            ).expanduser(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.http_port < 1 or self.http_port > 65535:
            raise ValueError("WECHATD_HTTP_PORT must be between 1 and 65535")
        if self.driver not in {"mock", "wxauto4", "wxautox4"}:
            raise ValueError("WECHAT_DRIVER must be mock, wxauto4, or wxautox4")
        if self.driver != "mock" and not self.listen_chats:
            raise ValueError("WECHAT_LISTEN_CHATS is required for a wxauto driver")
        if (
            self.wechat_poll_interval_seconds < 1
            or self.wechat_poll_interval_seconds > 60
        ):
            raise ValueError(
                "WECHAT_POLL_INTERVAL_SECONDS must be between 1 and 60"
            )
        if self.send_min_interval_seconds < 0:
            raise ValueError("WECHATD_SEND_MIN_INTERVAL_SECONDS cannot be negative")
        if self.max_request_bytes < 1024:
            raise ValueError("WECHATD_MAX_REQUEST_BYTES must be at least 1024")
