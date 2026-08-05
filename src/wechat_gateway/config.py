from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE env files without overriding process variables.

    With no argument, loads the legacy `.env.gateway` first and then the
    private `.private/env/gateway.env`, so the private copy wins during
    migration.
    """

    candidates = (
        [path]
        if path is not None
        else [Path(".env.gateway"), Path(".private/env/gateway.env")]
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
class GatewaySettings:
    account_id: str
    core_url: str
    api_token: str
    driver: str
    listen_chats: tuple[str, ...]
    bot_mention: str
    wechat_poll_interval_seconds: float
    poll_timeout_seconds: float
    action_lease_seconds: float
    http_timeout_seconds: float
    event_queue_size: int
    retry_min_seconds: float
    retry_max_seconds: float
    state_db: Path

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        _load_env_file()
        settings = cls(
            account_id=_required("GATEWAY_ACCOUNT_ID"),
            core_url=_required("BOT_CORE_URL").rstrip("/"),
            api_token=_required("BOT_API_TOKEN"),
            driver=os.environ.get("WECHAT_DRIVER", "mock").strip().lower(),
            listen_chats=_csv("WECHAT_LISTEN_CHATS"),
            bot_mention=os.environ.get("WECHAT_BOT_MENTION", "").strip(),
            wechat_poll_interval_seconds=float(
                os.environ.get("WECHAT_POLL_INTERVAL_SECONDS", "1")
            ),
            poll_timeout_seconds=float(
                os.environ.get("GATEWAY_POLL_TIMEOUT_SECONDS", "20")
            ),
            action_lease_seconds=float(
                os.environ.get("GATEWAY_ACTION_LEASE_SECONDS", "60")
            ),
            http_timeout_seconds=float(
                os.environ.get("GATEWAY_HTTP_TIMEOUT_SECONDS", "30")
            ),
            event_queue_size=int(os.environ.get("GATEWAY_EVENT_QUEUE_SIZE", "500")),
            retry_min_seconds=float(
                os.environ.get("GATEWAY_RETRY_MIN_SECONDS", "1")
            ),
            retry_max_seconds=float(
                os.environ.get("GATEWAY_RETRY_MAX_SECONDS", "30")
            ),
            state_db=Path(
                os.environ.get(
                    "GATEWAY_STATE_DB", ".private/state/gateway-state.sqlite3"
                )
            ).expanduser(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
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
        if self.poll_timeout_seconds < 0 or self.poll_timeout_seconds > 30:
            raise ValueError("GATEWAY_POLL_TIMEOUT_SECONDS must be between 0 and 30")
        if self.action_lease_seconds < 5 or self.action_lease_seconds > 300:
            raise ValueError("GATEWAY_ACTION_LEASE_SECONDS must be between 5 and 300")
        if self.http_timeout_seconds <= 0:
            raise ValueError("GATEWAY_HTTP_TIMEOUT_SECONDS must be positive")
        if self.event_queue_size < 1:
            raise ValueError("GATEWAY_EVENT_QUEUE_SIZE must be positive")
        if self.retry_min_seconds <= 0:
            raise ValueError("GATEWAY_RETRY_MIN_SECONDS must be positive")
        if self.retry_max_seconds < self.retry_min_seconds:
            raise ValueError(
                "GATEWAY_RETRY_MAX_SECONDS cannot be smaller than retry minimum"
            )
