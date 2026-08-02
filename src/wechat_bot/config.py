from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _default_health_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/health", "", ""))


def _uses_loopback_host(url: str) -> bool:
    hostname = urlsplit(url).hostname
    if hostname is None:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class LlmEndpointSettings:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    temperature: float
    max_output_tokens: int
    max_concurrency: int
    top_p: float | None = None
    repeat_penalty: float | None = None
    health_url: str | None = None
    health_timeout_seconds: float = 2

    def validate(self, prefix: str) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{prefix}_BASE_URL must be an http(s) URL")
        if self.health_url is not None:
            health = urlsplit(self.health_url)
            if health.scheme not in {"http", "https"} or not health.netloc:
                raise ValueError(f"{prefix}_HEALTH_URL must be an http(s) URL")
        if self.timeout_seconds <= 0:
            raise ValueError(f"{prefix}_TIMEOUT_SECONDS must be positive")
        if not 0 <= self.temperature <= 2:
            raise ValueError(f"{prefix}_TEMPERATURE must be between 0 and 2")
        if self.max_output_tokens < 1:
            raise ValueError(f"{prefix}_MAX_OUTPUT_TOKENS must be positive")
        if self.max_concurrency < 1:
            raise ValueError(f"{prefix}_MAX_CONCURRENCY must be at least 1")
        if self.top_p is not None and not 0 < self.top_p <= 1:
            raise ValueError(f"{prefix}_TOP_P must be between 0 and 1")
        if self.repeat_penalty is not None and self.repeat_penalty <= 0:
            raise ValueError(f"{prefix}_REPEAT_PENALTY must be positive")
        if self.health_timeout_seconds <= 0:
            raise ValueError(f"{prefix}_HEALTH_TIMEOUT_SECONDS must be positive")


@dataclass(frozen=True, slots=True)
class Settings:
    api_host: str
    api_port: int
    api_token: str
    agent_hub_token: str
    agent_hub_allow_remote: bool
    workers: int
    queue_size: int
    allowed_users: frozenset[str]
    allowed_groups: frozenset[str]
    group_require_mention: bool
    llm_backend: str
    remote_llm: LlmEndpointSettings | None
    local_llm: LlmEndpointSettings | None
    system_prompt: str
    max_history_messages: int
    state_db: Path

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env_file()
        backend = os.environ.get("LLM_BACKEND", "remote").strip().lower()
        if backend not in {"remote", "local_rwkv", "auto"}:
            raise ValueError("LLM_BACKEND must be remote, local_rwkv, or auto")

        remote_llm = (
            cls._remote_llm_from_env() if backend in {"remote", "auto"} else None
        )
        local_llm = (
            cls._local_llm_from_env()
            if backend in {"local_rwkv", "auto"}
            else None
        )
        api_token = _required("BOT_API_TOKEN")
        settings = cls(
            api_host=os.environ.get("BOT_API_HOST", "127.0.0.1"),
            api_port=int(os.environ.get("BOT_API_PORT", "8080")),
            api_token=api_token,
            agent_hub_token=os.environ.get("AGENT_HUB_TOKEN", "").strip()
            or api_token,
            agent_hub_allow_remote=_bool("AGENT_HUB_ALLOW_REMOTE", False),
            workers=int(os.environ.get("BOT_WORKERS", "2")),
            queue_size=int(os.environ.get("BOT_QUEUE_SIZE", "100")),
            allowed_users=_csv("BOT_ALLOWED_USERS"),
            allowed_groups=_csv("BOT_ALLOWED_GROUPS"),
            group_require_mention=_bool("BOT_GROUP_REQUIRE_MENTION", True),
            llm_backend=backend,
            remote_llm=remote_llm,
            local_llm=local_llm,
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

    @staticmethod
    def _remote_llm_from_env() -> LlmEndpointSettings:
        return LlmEndpointSettings(
            base_url=_required("LLM_BASE_URL").rstrip("/"),
            api_key=os.environ.get("LLM_API_KEY", "").strip(),
            model=_required("LLM_MODEL"),
            timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "90")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.3")),
            max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "1024")),
            max_concurrency=int(os.environ.get("LLM_MAX_CONCURRENCY", "4")),
        )

    @staticmethod
    def _local_llm_from_env() -> LlmEndpointSettings:
        base_url = os.environ.get(
            "LOCAL_LLM_BASE_URL", "http://127.0.0.1:18080/v1"
        ).strip().rstrip("/")
        health_url = _optional("LOCAL_LLM_HEALTH_URL") or _default_health_url(base_url)
        return LlmEndpointSettings(
            base_url=base_url,
            api_key=os.environ.get("LOCAL_LLM_API_KEY", "").strip(),
            model=os.environ.get("LOCAL_LLM_MODEL", "rwkv-local").strip(),
            timeout_seconds=float(
                os.environ.get("LOCAL_LLM_TIMEOUT_SECONDS", "180")
            ),
            temperature=float(os.environ.get("LOCAL_LLM_TEMPERATURE", "1.0")),
            max_output_tokens=int(
                os.environ.get(
                    "LOCAL_LLM_MAX_OUTPUT_TOKENS",
                    "512",
                )
            ),
            max_concurrency=int(
                os.environ.get("LOCAL_LLM_MAX_CONCURRENCY", "1")
            ),
            top_p=float(os.environ.get("LOCAL_LLM_TOP_P", "0.5")),
            repeat_penalty=float(
                os.environ.get("LOCAL_LLM_REPEAT_PENALTY", "1.2")
            ),
            health_url=health_url,
            health_timeout_seconds=float(
                os.environ.get("LOCAL_LLM_HEALTH_TIMEOUT_SECONDS", "2")
            ),
        )

    def validate(self) -> None:
        if not 1 <= self.api_port <= 65535:
            raise ValueError("BOT_API_PORT must be between 1 and 65535")
        if self.workers < 1:
            raise ValueError("BOT_WORKERS must be at least 1")
        if self.queue_size < 1:
            raise ValueError("BOT_QUEUE_SIZE must be at least 1")
        if self.remote_llm is not None:
            self.remote_llm.validate("LLM")
        if self.local_llm is not None:
            if not self.local_llm.model:
                raise ValueError("LOCAL_LLM_MODEL cannot be empty")
            self.local_llm.validate("LOCAL_LLM")
            if not _uses_loopback_host(self.local_llm.base_url):
                raise ValueError("LOCAL_LLM_BASE_URL must use a loopback host")
            if self.local_llm.health_url is not None and not _uses_loopback_host(
                self.local_llm.health_url
            ):
                raise ValueError("LOCAL_LLM_HEALTH_URL must use a loopback host")
        if self.max_history_messages < 0:
            raise ValueError("BOT_MAX_HISTORY_MESSAGES cannot be negative")
