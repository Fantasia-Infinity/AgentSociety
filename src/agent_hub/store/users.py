from __future__ import annotations

import secrets
import time
import hashlib
from typing import Any

from ..auth import AuthenticatedContext
from ..domain import (
    ActorRegistration,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
    TenantRegistration,
)
from ..passwords import (
    hash_password,
    needs_rehash,
    public_account,
    validate_password,
    validate_username,
    verify_password,
)


MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60
DEFAULT_NODE_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60


class UserStore:
    """Password accounts, sessions, and password-based agent logins."""

    def register_user_personal(
        self,
        *,
        username: str,
        password: str,
        display_name: str,
    ) -> dict[str, Any]:
        """Register a user in a private tenant of their own.

        Public self-registration must never land in an existing shared tenant:
        a stranger there could submit tasks that the tenant's workers would
        execute. Each account gets a dedicated ``user-<username>`` tenant and
        becomes its tenant_admin.
        """

        username = validate_username(username)
        tenant_id = f"user-{username}"
        self.create_tenant(
            TenantRegistration(
                tenant_id=tenant_id,
                display_name=f"{display_name.strip() or username} workspace",
                metadata={"kind": "personal", "owner_username": username},
            )
        )
        return self.register_user(
            username=username,
            password=password,
            display_name=display_name,
            tenant_id=tenant_id,
        )

    def register_user(
        self,
        *,
        username: str,
        password: str,
        display_name: str,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        username = validate_username(username)
        validate_password(password)
        now = time.time()
        with self._condition, self._connection:
            self._tenant(tenant_id)
            existing = self._connection.execute(
                "SELECT username FROM hub_user_accounts WHERE username=?",
                (username,),
            ).fetchone()
            if existing is not None:
                raise ValueError("username is already registered")
            count_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM hub_user_accounts WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
            role = "tenant_admin" if int(count_row["n"]) == 0 else "tenant_user"
            principal_id = f"human-{username}"
            self.register_principal(
                PrincipalRegistration(
                    principal_id=principal_id,
                    kind="human",
                    display_name=display_name.strip() or username,
                    metadata={"origin": "self-registration"},
                ),
                tenant_id=tenant_id,
            )
            self._connection.execute(
                """
                INSERT INTO hub_user_accounts(
                    username, password_hash, principal_id, tenant_id, role,
                    display_name, failed_attempts, locked_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (
                    username,
                    hash_password(password),
                    principal_id,
                    tenant_id,
                    role,
                    display_name.strip() or username,
                    now,
                    now,
                ),
            )
            return public_account(self._account(username))

    def authenticate_password(
        self, *, username: str, password: str
    ) -> dict[str, Any] | None:
        username = validate_username(username)
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hub_user_accounts WHERE username=?",
                (username,),
            ).fetchone()
            if row is None:
                return None
            account = dict(row)
            if account.get("locked_until") is not None and float(
                account["locked_until"]
            ) > now:
                return None
            if not verify_password(str(account["password_hash"]), password):
                failed = int(account["failed_attempts"]) + 1
                locked_until = now + LOCK_SECONDS if failed >= MAX_FAILED_ATTEMPTS else None
                self._connection.execute(
                    """
                    UPDATE hub_user_accounts
                    SET failed_attempts=?, locked_until=?, updated_at=?
                    WHERE username=?
                    """,
                    (failed, locked_until, now, username),
                )
                return None
            self._connection.execute(
                """
                UPDATE hub_user_accounts
                SET failed_attempts=0, locked_until=NULL, updated_at=?
                WHERE username=?
                """,
                (now, username),
            )
            if needs_rehash(str(account["password_hash"])):
                self._connection.execute(
                    "UPDATE hub_user_accounts SET password_hash=?, updated_at=? WHERE username=?",
                    (hash_password(password), now, username),
                )
                account["password_hash"] = "rehashed"
            account["failed_attempts"] = 0
            account["locked_until"] = None
            return account

    def create_session(
        self,
        account: dict[str, Any],
        *,
        label: str = "web",
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> tuple[str, dict[str, Any]]:
        now = time.time()
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self._condition, self._connection:
            self._connection.execute(
                """
                INSERT INTO hub_auth_sessions(
                    session_token_hash, principal_id, tenant_id, role, label,
                    created_at, expires_at, revoked_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    token_hash,
                    account["principal_id"],
                    account["tenant_id"],
                    account["role"],
                    label,
                    now,
                    now + ttl_seconds,
                ),
            )
            return raw, self._session_record(token_hash)

    def authenticate_session(self, raw_token: str) -> AuthenticatedContext | None:
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hub_auth_sessions WHERE session_token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None:
                return None
            if float(row["expires_at"]) < now:
                return None
            self._connection.execute(
                "UPDATE hub_auth_sessions SET last_seen_at=? WHERE session_token_hash=?",
                (now, token_hash),
            )
            return AuthenticatedContext(
                role=str(row["role"]),
                tenant_id=str(row["tenant_id"]),
                principal_id=str(row["principal_id"]),
                session_id=str(row["session_token_hash"]),
            )

    def revoke_session(self, session_token_hash: str) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._connection.execute(
                "UPDATE hub_auth_sessions SET revoked_at=? WHERE session_token_hash=?",
                (now, session_token_hash),
            )
            return self._session_record(session_token_hash)

    def list_sessions(
        self, *, principal_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if principal_id is None:
                rows = self._connection.execute(
                    "SELECT session_token_hash FROM hub_auth_sessions ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT session_token_hash FROM hub_auth_sessions
                    WHERE principal_id=? ORDER BY created_at DESC
                    """,
                    (principal_id,),
                ).fetchall()
            return [self._session_record(str(row["session_token_hash"])) for row in rows]

    def revoke_principal_sessions(
        self, *, principal_id: str, except_hash: str | None = None
    ) -> int:
        now = time.time()
        with self._condition, self._connection:
            if except_hash is None:
                cursor = self._connection.execute(
                    """
                    UPDATE hub_auth_sessions SET revoked_at=?
                    WHERE principal_id=? AND revoked_at IS NULL
                    """,
                    (now, principal_id),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE hub_auth_sessions SET revoked_at=?
                    WHERE principal_id=? AND session_token_hash<>? AND revoked_at IS NULL
                    """,
                    (now, principal_id, except_hash),
                )
            return int(cursor.rowcount)

    def change_password(
        self, *, username: str, old_password: str, new_password: str
    ) -> bool:
        account = self.authenticate_password(username=username, password=old_password)
        if account is None:
            return False
        validate_password(new_password)
        now = time.time()
        with self._condition, self._connection:
            self._connection.execute(
                "UPDATE hub_user_accounts SET password_hash=?, updated_at=? WHERE username=?",
                (hash_password(new_password), now, username),
            )
        return True

    def agent_login(
        self,
        *,
        username: str,
        password: str,
        node_id: str,
        actor_id: str | None,
        display_name: str | None,
        capabilities: tuple[str, ...],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        account = self.authenticate_password(username=username, password=password)
        if account is None:
            raise PermissionError("invalid username or password")
        principal_id = str(account["principal_id"])
        tenant_id = str(account["tenant_id"])
        node_id = str(node_id).strip()
        if not node_id:
            raise ValueError("node_id is required")
        resolved_actor_id = (actor_id or f"pi-{node_id}").strip()
        now = time.time()
        with self._condition, self._connection:
            self.register_actor(
                ActorRegistration(
                    actor_id=resolved_actor_id,
                    principal_id=principal_id,
                    kind="agent",
                    display_name=(display_name or f"Pi on {node_id}").strip() or resolved_actor_id,
                    capabilities=capabilities or ("pi", "hub-task"),
                    metadata=metadata,
                ),
                tenant_id=tenant_id,
            )
            node_item = self.register_node(
                NodeRegistration(
                    node_id=node_id,
                    actor_id=resolved_actor_id,
                    display_name=(display_name or node_id).strip() or node_id,
                    capabilities=capabilities or ("pi", "hub-task"),
                    metadata=metadata,
                ),
                tenant_id=tenant_id,
            )
            raw, record = self.create_auth_token(
                AuthTokenCreation(
                    tenant_id=tenant_id,
                    role="node",
                    principal_id=principal_id,
                    actor_id=resolved_actor_id,
                    node_id=node_id,
                    label=f"agent-login {node_id}",
                    expires_at=now + DEFAULT_NODE_TOKEN_TTL_SECONDS,
                )
            )
        return {
            "node_token": raw,
            "token": record,
            "node": node_item,
            "actor": self._actor(resolved_actor_id),
            "principal": self._principal(principal_id),
            "tenant_id": tenant_id,
            "user": public_account(account),
        }

    def _account(self, username: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_user_accounts WHERE username=?",
            (username,),
        ).fetchone()
        if row is None:
            raise LookupError("account not found")
        return dict(row)

    def account_for_principal(self, principal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hub_user_accounts WHERE principal_id=?",
                (principal_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def _session_record(self, token_hash: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_auth_sessions WHERE session_token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise LookupError("session not found")
        return {
            "session_token_id": str(row["session_token_hash"]),
            "principal_id": str(row["principal_id"]),
            "tenant_id": str(row["tenant_id"]),
            "role": str(row["role"]),
            "label": str(row["label"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "revoked_at": row["revoked_at"],
            "last_seen_at": row["last_seen_at"],
        }
