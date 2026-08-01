from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _load_env_file(path: Path = Path(".env")) -> None:
    """Load a small KEY=VALUE env file without overriding process variables."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
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


def _csv(name: str) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    )


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    api_host: str
    api_port: int
    api_token: str
    workers: int
    queue_size: int
    allowed_users: frozenset[str]
    allowed_groups: frozenset[str]
    group_require_mention: bool
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    llm_temperature: float
    llm_max_output_tokens: int
    system_prompt: str
    max_history_messages: int
    state_db: Path

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env_file()
        settings = cls(
            api_host=os.environ.get("BOT_API_HOST", "127.0.0.1"),
            api_port=int(os.environ.get("BOT_API_PORT", "8080")),
            api_token=_required("BOT_API_TOKEN"),
            workers=int(os.environ.get("BOT_WORKERS", "2")),
            queue_size=int(os.environ.get("BOT_QUEUE_SIZE", "100")),
            allowed_users=_csv("BOT_ALLOWED_USERS"),
            allowed_groups=_csv("BOT_ALLOWED_GROUPS"),
            group_require_mention=_bool("BOT_GROUP_REQUIRE_MENTION", True),
            llm_base_url=_required("LLM_BASE_URL").rstrip("/"),
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
            llm_model=_required("LLM_MODEL"),
            llm_timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "90")),
            llm_temperature=float(os.environ.get("LLM_TEMPERATURE", "0.3")),
            llm_max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "1024")),
            system_prompt=os.environ.get(
                "BOT_SYSTEM_PROMPT",
                "You are a concise and helpful assistant responding through WeChat.",
            ),
            max_history_messages=int(os.environ.get("BOT_MAX_HISTORY_MESSAGES", "20")),
            state_db=Path(
                os.environ.get("BOT_STATE_DB", "core-state.sqlite3")
            ).expanduser(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= self.api_port <= 65535:
            raise ValueError("BOT_API_PORT must be between 1 and 65535")
        if self.workers < 1:
            raise ValueError("BOT_WORKERS must be at least 1")
        if self.queue_size < 1:
            raise ValueError("BOT_QUEUE_SIZE must be at least 1")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be positive")
        if not 0 <= self.llm_temperature <= 2:
            raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
        if self.llm_max_output_tokens < 1:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be positive")
        if self.max_history_messages < 0:
            raise ValueError("BOT_MAX_HISTORY_MESSAGES cannot be negative")
