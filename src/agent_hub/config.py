from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path


def _load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE env files without overriding process variables.

    With no argument, loads the legacy root `.env.hub` first and then the
    private `.private/env/hub.env`, so the private copy wins during migration.
    """

    candidates = (
        [path]
        if path is not None
        else [Path(".env.hub"), Path(".private/env/hub.env")]
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


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


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
    database_url: str | None
    object_store_url: str | None
    public_url: str | None
    allow_non_loopback_bind: bool
    allow_registration: bool
    enable_mcp: bool
    rate_limit_enabled: bool
    rate_limit_auth_per_minute: int
    rate_limit_register_per_hour: int
    web_secret: str | None
    web_cookie_secure: bool
    disable_bootstrap: bool
    oidc_issuer: str | None
    oidc_audience: str | None

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
                os.environ.get(
                    "AGENT_HUB_STATE_DB", ".private/state/hub-state.sqlite3"
                )
            ).expanduser(),
            database_url=os.environ.get("AGENT_HUB_DATABASE_URL", "").strip()
            or None,
            object_store_url=os.environ.get(
                "AGENT_HUB_OBJECT_STORE_URL", ""
            ).strip()
            or None,
            public_url=os.environ.get("AGENT_HUB_PUBLIC_URL", "").strip()
            or None,
            allow_non_loopback_bind=_bool(
                "AGENT_HUB_ALLOW_NON_LOOPBACK_BIND", False
            ),
            allow_registration=_bool("AGENT_HUB_ALLOW_REGISTRATION", True),
            enable_mcp=_bool("AGENT_HUB_ENABLE_MCP", True),
            rate_limit_enabled=_bool("AGENT_HUB_RATE_LIMIT", True),
            rate_limit_auth_per_minute=_positive_int(
                "AGENT_HUB_RATE_LIMIT_AUTH_PER_MINUTE", 20
            ),
            rate_limit_register_per_hour=_positive_int(
                "AGENT_HUB_RATE_LIMIT_REGISTER_PER_HOUR", 10
            ),
            web_secret=os.environ.get("AGENT_HUB_WEB_SECRET", "").strip() or None,
            web_cookie_secure=_bool("AGENT_HUB_WEB_COOKIE_SECURE", True),
            disable_bootstrap=_bool("AGENT_HUB_DISABLE_BOOTSTRAP", False),
            oidc_issuer=os.environ.get("AGENT_HUB_OIDC_ISSUER", "").strip() or None,
            oidc_audience=os.environ.get("AGENT_HUB_OIDC_AUDIENCE", "").strip()
            or None,
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
        if self.database_url is not None and not self.database_url.startswith(
            ("postgres://", "postgresql://")
        ):
            raise ValueError("AGENT_HUB_DATABASE_URL must be a PostgreSQL URL")
        if self.object_store_url is not None and not self.object_store_url.startswith(
            ("file://", "s3://")
        ):
            raise ValueError(
                "AGENT_HUB_OBJECT_STORE_URL must use file:// or s3://"
            )
        if self.public_url is not None and not self.public_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("AGENT_HUB_PUBLIC_URL must use HTTP or HTTPS")
        if self.web_secret is not None and len(self.web_secret) < 32:
            raise ValueError("AGENT_HUB_WEB_SECRET must contain at least 32 characters")
        if self.oidc_issuer is not None and not self.oidc_issuer.startswith("https://"):
            raise ValueError("AGENT_HUB_OIDC_ISSUER must use https://")
