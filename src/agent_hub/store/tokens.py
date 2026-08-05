from __future__ import annotations

import time
import uuid
import hashlib
import secrets
from typing import Any

from ..auth import AuthenticatedContext
from ..domain import (
    AuthTokenCreation,
)

class TokenStore:
    """Auth token issuance, revocation and authentication."""

    def create_auth_token(
        self, item: AuthTokenCreation
    ) -> tuple[str, dict[str, Any]]:
        now = time.time()
        if item.role == "node":
            if not item.actor_id or not item.node_id:
                raise ValueError("node tokens require actor_id and node_id")
        elif not item.principal_id:
            raise ValueError(
                "tenant_admin and tenant_user tokens require principal_id"
            )
        with self._condition, self._connection:
            self._tenant(item.tenant_id)
            if item.principal_id is not None:
                principal = self._principal(item.principal_id)
                if principal["tenant_id"] != item.tenant_id:
                    raise PermissionError("principal does not belong to tenant")
            if item.actor_id is not None:
                actor = self._actor(item.actor_id)
                if actor["tenant_id"] != item.tenant_id:
                    raise PermissionError("actor does not belong to tenant")
                if (
                    item.principal_id is not None
                    and actor["principal_id"] != item.principal_id
                ):
                    raise ValueError("actor does not belong to principal")
            if item.node_id is not None:
                node = self._node(item.node_id)
                if node["tenant_id"] != item.tenant_id:
                    raise PermissionError("node does not belong to tenant")
                if item.actor_id is not None and node["actor_id"] != item.actor_id:
                    raise ValueError("node does not belong to actor")
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            token_id = f"token_{uuid.uuid4().hex}"
            self._connection.execute(
                """
                INSERT INTO hub_auth_tokens(
                    token_id, token_hash, tenant_id, role, principal_id,
                    actor_id, node_id, label, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    token_hash,
                    item.tenant_id,
                    item.role,
                    item.principal_id,
                    item.actor_id,
                    item.node_id,
                    item.label,
                    now,
                    item.expires_at,
                ),
            )
            return raw_token, self._token_record(token_id)

    def list_auth_tokens(
        self, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if tenant_id is None:
                rows = self._connection.execute(
                    "SELECT token_id FROM hub_auth_tokens ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT token_id FROM hub_auth_tokens
                    WHERE tenant_id=? ORDER BY created_at DESC
                    """,
                    (tenant_id,),
                ).fetchall()
            return [self._token_record(str(row["token_id"])) for row in rows]

    def list_principal_tokens(self, principal_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT token_id FROM hub_auth_tokens
                WHERE principal_id=? ORDER BY created_at DESC
                """,
                (principal_id,),
            ).fetchall()
            return [self._token_record(str(row["token_id"])) for row in rows]

    def revoke_principal_token(
        self, token_id: str, principal_id: str
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            record = self._token_record(token_id)
            if record["principal_id"] != principal_id:
                raise LookupError("token not found")
            self._connection.execute(
                "UPDATE hub_auth_tokens SET revoked_at=? WHERE token_id=?",
                (now, token_id),
            )
            return self._token_record(token_id)

    def revoke_principal_tokens(self, principal_id: str) -> int:
        now = time.time()
        with self._condition, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE hub_auth_tokens SET revoked_at=?
                WHERE principal_id=? AND revoked_at IS NULL
                """,
                (now, principal_id),
            )
            return int(cursor.rowcount)

    def revoke_auth_token(
        self, token_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            record = self._token_record(token_id)
            if tenant_id is not None and record["tenant_id"] != tenant_id:
                raise LookupError("token not found")
            self._connection.execute(
                "UPDATE hub_auth_tokens SET revoked_at=? WHERE token_id=?",
                (now, token_id),
            )
            return self._token_record(token_id)

    def authenticate_token(self, raw_token: str) -> AuthenticatedContext | None:
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hub_auth_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None:
                return None
            if row["expires_at"] is not None and float(row["expires_at"]) < time.time():
                return None
            return AuthenticatedContext(
                role=str(row["role"]),
                tenant_id=str(row["tenant_id"]),
                principal_id=row["principal_id"],
                actor_id=row["actor_id"],
                node_id=row["node_id"],
            )

    def register_oidc_identity(
        self,
        *,
        provider: str,
        subject: str,
        tenant_id: str,
        principal_id: str,
        role: str,
    ) -> dict[str, Any]:
        if role not in {"tenant_admin", "tenant_user"}:
            raise ValueError("role must be tenant_admin or tenant_user")
        now = time.time()
        with self._condition, self._connection:
            self._tenant(tenant_id)
            principal = self._principal(principal_id)
            if principal["tenant_id"] != tenant_id:
                raise PermissionError("principal does not belong to tenant")
            self._connection.execute(
                """
                INSERT INTO hub_oidc_identities(
                    provider, subject, tenant_id, principal_id, role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, subject) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    principal_id=excluded.principal_id,
                    role=excluded.role
                """,
                (provider, subject, tenant_id, principal_id, role, now),
            )
            return {
                "provider": provider,
                "subject": subject,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "role": role,
                "created_at": now,
            }

    def authenticate_oidc(
        self, *, provider: str, subject: str
    ) -> AuthenticatedContext | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT tenant_id, principal_id, role FROM hub_oidc_identities
                WHERE provider=? AND subject=?
                """,
                (provider, subject),
            ).fetchone()
            if row is None:
                return None
            return AuthenticatedContext(
                role=str(row["role"]),
                tenant_id=str(row["tenant_id"]),
                principal_id=str(row["principal_id"]),
            )
