from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AuthenticatedContext:
    role: str
    tenant_id: str | None = None
    principal_id: str | None = None
    actor_id: str | None = None
    node_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "actor_id": self.actor_id,
            "node_id": self.node_id,
        }

    @classmethod
    def from_dict(cls, claims: dict[str, Any]) -> "AuthenticatedContext":
        return cls(
            role=str(claims.get("role") or "admin"),
            tenant_id=claims.get("tenant_id"),
            principal_id=claims.get("principal_id"),
            actor_id=claims.get("actor_id"),
            node_id=claims.get("node_id"),
        )


def trusted_local_context() -> AuthenticatedContext:
    """Admin context for trusted local processes (stdio MCP).

    This is intentionally not a bearer-token flow: the process is launched by
    the local operator and already controls the machine.
    """

    return AuthenticatedContext(role="admin")


class OIDCIdentityProvider(Protocol):
    """Validates an OIDC ID token and maps it to an AgentSociety identity."""

    def validate_id_token(self, token: str) -> AuthenticatedContext | None:
        ...
