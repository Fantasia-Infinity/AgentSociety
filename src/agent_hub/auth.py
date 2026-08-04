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


class OIDCIdentityProvider(Protocol):
    """Validates an OIDC ID token and maps it to an AgentSociety identity."""

    def validate_id_token(self, token: str) -> AuthenticatedContext | None:
        ...
