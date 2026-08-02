from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path


def _load_env_file(path: Path = Path(".env.hub")) -> None:
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


def _loopback_host(host: str) -> bool:
    normalized = host.strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class HubSettings:
    api_host: str
    api_port: int
    api_token: str
    state_db: Path
    allow_non_loopback_bind: bool

    @classmethod
    def from_env(cls) -> "HubSettings":
        _load_env_file(
            Path(os.environ.get("AGENT_HUB_ENV_FILE", ".env.hub")).expanduser()
        )
        settings = cls(
            api_host=os.environ.get("AGENT_HUB_HOST", "127.0.0.1").strip(),
            api_port=int(os.environ.get("AGENT_HUB_PORT", "8090")),
            api_token=_required("AGENT_HUB_TOKEN"),
            state_db=Path(
                os.environ.get("AGENT_HUB_STATE_DB", "hub-state.sqlite3")
            ).expanduser(),
            allow_non_loopback_bind=_bool(
                "AGENT_HUB_ALLOW_NON_LOOPBACK_BIND", False
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.api_host:
            raise ValueError("AGENT_HUB_HOST cannot be empty")
        if not 1 <= self.api_port <= 65535:
            raise ValueError("AGENT_HUB_PORT must be between 1 and 65535")
        if len(self.api_token) < 24:
            raise ValueError("AGENT_HUB_TOKEN must contain at least 24 characters")
        if not self.allow_non_loopback_bind and not _loopback_host(self.api_host):
            raise ValueError(
                "AGENT_HUB_HOST must be loopback unless "
                "AGENT_HUB_ALLOW_NON_LOOPBACK_BIND=true"
            )
