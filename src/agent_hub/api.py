from __future__ import annotations

from http import HTTPStatus
from typing import Any
import base64
import binascii
from urllib.parse import parse_qs

from .auth import AuthenticatedContext
from .domain import (
    ActorRegistration,
    ArtifactSubmission,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
    RunStatus,
    RunSubmission,
    TenantRegistration,
    TaskSubmission,
    TaskUpdate,
    object_value,
    optional_text,
    required_text,
)
from .errors import ApiError, map_error
from .store import AgentHubStore
from .object_store import ObjectStore


class AgentHubApi:
    """HTTP-independent router for the coordination API."""

    prefix = "/v1/hub"
    auth_prefix = "/v1/auth"
    public_auth_posts = frozenset(
        {
            "/v1/auth/register",
            "/v1/auth/login",
            "/v1/auth/agent-login",
        }
    )

    def __init__(
        self,
        store: AgentHubStore,
        object_store: ObjectStore | None = None,
        *,
        allow_registration: bool = True,
    ) -> None:
        self.store = store
        self.object_store = object_store
        self.allow_registration = allow_registration

    @classmethod
    def is_public_auth_post(cls, path: str) -> bool:
        return path in cls.public_auth_posts

    @classmethod
    def matches(cls, path: str) -> bool:
        return path == cls.prefix or path.startswith(f"{cls.prefix}/")

    def get(
        self,
        path: str,
        query_string: str,
        context: AuthenticatedContext | None = None,
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        try:
            return self._get(path, query_string, context)
        except ApiError:
            raise
        except (LookupError, PermissionError, ValueError) as exc:
            raise map_error(exc) from exc

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        context: AuthenticatedContext | None = None,
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        try:
            return self._post(path, payload, context)
        except ApiError:
            raise
        except (LookupError, PermissionError, ValueError) as exc:
            raise map_error(exc) from exc

    def authenticate(self, raw_token: str) -> AuthenticatedContext | None:
        """Resolve a bearer token into a context (the only token entry point)."""

        return self.store.authenticate_token(raw_token)

    def register_gateway_identity(
        self,
        principal: PrincipalRegistration,
        actor: ActorRegistration,
        *,
        tenant_id: str = "default",
    ) -> None:
        """Idempotently register a protocol gateway identity (MCP/A2A).

        Gateway identities are trusted service accounts created by the protocol
        adapters, so they bypass the tenant-manager role requirement.
        """

        self.store.register_principal(principal, tenant_id=tenant_id)
        self.store.register_actor(actor, tenant_id=tenant_id)

    def _get(
        self,
        path: str,
        query_string: str,
        context: AuthenticatedContext | None = None,
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        tenant_id = self._tenant_scope(context)
        principal_scope = self._principal_scope(context)
        if context is not None and context.is_admin:
            requested = (parse_qs(query_string).get("tenant_id") or [None])[0]
            if requested:
                tenant_id = requested
        if path == self.prefix:
            return HTTPStatus.OK, {
                "status": "ok",
                **self.store.stats(
                    tenant_id=tenant_id, principal_id=principal_scope
                ),
            }
        if path == f"{self.prefix}/tenants":
            if context is not None and not context.is_admin:
                return HTTPStatus.OK, {
                    "tenants": [self.store.get_tenant(context.tenant_id or "default")]
                }
            return HTTPStatus.OK, {"tenants": self.store.list_tenants()}
        if path == f"{self.prefix}/tokens":
            return HTTPStatus.OK, {
                "tokens": self.store.list_auth_tokens(tenant_id=tenant_id)
            }
        if path == f"{self.prefix}/principals":
            return HTTPStatus.OK, {
                "principals": self.store.list_principals(
                    tenant_id=tenant_id, principal_id=principal_scope
                )
            }
        if path == f"{self.prefix}/actors":
            return HTTPStatus.OK, {
                "actors": self.store.list_actors(
                    tenant_id=tenant_id, principal_id=principal_scope
                )
            }
        if path == f"{self.prefix}/nodes":
            return HTTPStatus.OK, {
                "nodes": self.store.list_nodes(
                    tenant_id=tenant_id, principal_id=principal_scope
                )
            }
        if path == f"{self.prefix}/tasks":
            query = parse_qs(query_string)
            status = (query.get("status") or [None])[0]
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError as exc:
                raise ValueError("limit must be an integer") from exc
            return HTTPStatus.OK, {
                "tasks": self.store.list_tasks(
                    status=status,
                    limit=limit,
                    tenant_id=tenant_id,
                    principal_id=principal_scope,
                )
            }
        if path == f"{self.prefix}/runs":
            query = parse_qs(query_string)
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError as exc:
                raise ValueError("limit must be an integer") from exc
            return HTTPStatus.OK, {
                "runs": self.store.list_runs(
                    limit=limit,
                    tenant_id=tenant_id,
                    principal_id=principal_scope,
                )
            }
        if path == f"{self.prefix}/artifacts":
            query = parse_qs(query_string)
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError as exc:
                raise ValueError("limit must be an integer") from exc
            return HTTPStatus.OK, {
                "artifacts": self.store.list_artifacts(
                    limit=limit,
                    tenant_id=tenant_id,
                    principal_id=principal_scope,
                )
            }

        parts = self._parts(path)
        if len(parts) == 2 and parts[0] == "tenants":
            if context is not None and not context.is_admin:
                if parts[1] != (context.tenant_id or "default"):
                    raise PermissionError("cannot access another tenant")
            return HTTPStatus.OK, {"tenant": self.store.get_tenant(parts[1])}
        if len(parts) == 3 and parts[0] == "tenants" and parts[2] == "tokens":
            if context is not None and not context.is_admin:
                if context.role != "tenant_admin":
                    raise PermissionError("tenant_admin role required")
                if parts[1] != (context.tenant_id or "default"):
                    raise PermissionError("cannot access another tenant")
            return HTTPStatus.OK, {
                "tokens": self.store.list_auth_tokens(tenant_id=parts[1])
            }
        if len(parts) == 2 and parts[0] == "tasks":
            task = self.store.get_task(parts[1], tenant_id=tenant_id)
            self._assert_principal_owner(context, task)
            return HTTPStatus.OK, {"task": task}
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
            query = parse_qs(query_string)
            try:
                after_seq = int((query.get("after_seq") or ["0"])[0])
                limit = int((query.get("limit") or ["500"])[0])
            except ValueError as exc:
                raise ValueError("after_seq and limit must be integers") from exc
            return HTTPStatus.OK, {
                "events": self.store.list_task_events(
                    parts[1],
                    after_seq=after_seq,
                    limit=limit,
                    tenant_id=tenant_id,
                )
            }
        if len(parts) == 2 and parts[0] == "runs":
            run = self.store.get_run(parts[1], tenant_id=tenant_id)
            self._assert_principal_owner(context, run)
            return HTTPStatus.OK, {"run": run}
        if path == f"{self.auth_prefix}/me":
            if context is None or not context.principal_id:
                raise ApiError("authentication required", HTTPStatus.UNAUTHORIZED)
            principal = self.store._principal(context.principal_id)
            account = self.store.account_for_principal(context.principal_id)
            sessions = self.store.list_sessions(
                principal_id=context.principal_id
            )
            tokens = self.store.list_principal_tokens(context.principal_id)
            return HTTPStatus.OK, {
                "me": {
                    "principal": principal,
                    "account": account,
                    "tenant_id": context.tenant_id,
                    "role": context.role,
                },
                "sessions": sessions,
                "tokens": tokens,
            }
        return None

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        context: AuthenticatedContext | None = None,
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        tenant_id = self._resolve_tenant(payload, context)
        if path == f"{self.auth_prefix}/register":
            if not self.allow_registration:
                raise ApiError("registration is disabled", HTTPStatus.FORBIDDEN)
            account = self.store.register_user(
                username=str(payload.get("username", "")),
                password=str(payload.get("password", "")),
                display_name=str(payload.get("display_name", "")).strip(),
                tenant_id="default",
            )
            return HTTPStatus.CREATED, {"user": account}
        if path == f"{self.auth_prefix}/login":
            account = self.store.authenticate_password(
                username=str(payload.get("username", "")),
                password=str(payload.get("password", "")),
            )
            if account is None:
                raise ApiError("invalid username or password", HTTPStatus.UNAUTHORIZED)
            raw, record = self.store.create_session(
                account,
                label=str(payload.get("label", "api")),
            )
            return HTTPStatus.OK, {
                "session_token": raw,
                "session": record,
                "user": {
                    "username": account["username"],
                    "display_name": account["display_name"],
                    "principal_id": account["principal_id"],
                    "tenant_id": account["tenant_id"],
                    "role": account["role"],
                },
            }
        if path == f"{self.auth_prefix}/agent-login":
            try:
                item = self.store.agent_login(
                    username=str(payload.get("username", "")),
                    password=str(payload.get("password", "")),
                    node_id=str(payload.get("node_id", "")),
                    actor_id=str(payload.get("actor_id", "")).strip() or None,
                    display_name=str(payload.get("display_name", "")).strip() or None,
                    capabilities=tuple(
                        str(c).strip()
                        for c in (payload.get("capabilities") or [])
                        if str(c).strip()
                    ),
                    metadata=object_value(payload, "metadata"),
                )
            except PermissionError as exc:
                raise ApiError(
                    "invalid username or password", HTTPStatus.UNAUTHORIZED
                ) from exc
            return HTTPStatus.OK, item
        if path == f"{self.auth_prefix}/logout":
            if context is None or not context.session_id:
                raise ApiError("session authentication required", HTTPStatus.UNAUTHORIZED)
            return HTTPStatus.OK, {
                "session": self.store.revoke_session(context.session_id)
            }
        if path == f"{self.auth_prefix}/change-password":
            if context is None or not context.principal_id:
                raise ApiError("authentication required", HTTPStatus.UNAUTHORIZED)
            account = self.store.account_for_principal(context.principal_id)
            if account is None:
                raise ApiError(
                    "no password account for this principal", HTTPStatus.CONFLICT
                )
            ok = self.store.change_password(
                username=str(account["username"]),
                old_password=str(payload.get("old_password", "")),
                new_password=str(payload.get("new_password", "")),
            )
            if not ok:
                raise ApiError("invalid current password", HTTPStatus.UNAUTHORIZED)
            self.store.revoke_principal_sessions(
                principal_id=context.principal_id, except_hash=context.session_id
            )
            self.store.revoke_principal_tokens(context.principal_id)
            return HTTPStatus.OK, {"changed": True}
        if path == f"{self.auth_prefix}/sessions/revoke":
            if context is None or not context.principal_id:
                raise ApiError("authentication required", HTTPStatus.UNAUTHORIZED)
            session_hash = str(payload.get("session_token_id", "")).strip()
            if not session_hash:
                raise ValueError("session_token_id is required")
            record = self.store.revoke_session(session_hash)
            if record["principal_id"] != context.principal_id:
                raise ApiError("session not found", HTTPStatus.NOT_FOUND)
            return HTTPStatus.OK, {"session": record}
        if path == f"{self.auth_prefix}/tokens/revoke":
            if context is None or not context.principal_id:
                raise ApiError("authentication required", HTTPStatus.UNAUTHORIZED)
            token_id = str(payload.get("token_id", "")).strip()
            if not token_id:
                raise ValueError("token_id is required")
            record = self.store.revoke_principal_token(
                token_id, context.principal_id
            )
            return HTTPStatus.OK, {"token": record}
        if path == f"{self.prefix}/tenants":
            self._require_admin(context)
            item = self.store.create_tenant(TenantRegistration.from_dict(payload))
            return HTTPStatus.CREATED, {"tenant": item}
        if path == f"{self.prefix}/tokens":
            self._require_admin(context)
            item = AuthTokenCreation.from_dict(payload)
            raw, record = self.store.create_auth_token(item)
            return HTTPStatus.CREATED, {"token": record, "raw_token": raw}
        if path == f"{self.prefix}/principals":
            self._require_tenant_manager(context)
            item = self.store.register_principal(
                PrincipalRegistration.from_dict(payload), tenant_id=tenant_id
            )
            return HTTPStatus.OK, {"principal": item}
        if path == f"{self.prefix}/actors":
            self._require_tenant_manager(context)
            item = self.store.register_actor(
                ActorRegistration.from_dict(payload), tenant_id=tenant_id
            )
            return HTTPStatus.OK, {"actor": item}
        if path == f"{self.prefix}/nodes":
            self._require_tenant_manager(context)
            item = self.store.register_node(
                NodeRegistration.from_dict(payload), tenant_id=tenant_id
            )
            return HTTPStatus.OK, {"node": item}
        if path == f"{self.prefix}/nodes/heartbeat":
            node_id = required_text(payload, "node_id", maximum=200)
            if context is not None and not context.is_admin:
                if context.node_id != node_id:
                    raise PermissionError("node token cannot heartbeat another node")
            return HTTPStatus.OK, {"node": self.store.heartbeat_node(node_id)}
        if path == f"{self.prefix}/tasks":
            scoped = dict(payload)
            scope_principal = self._principal_scope(context)
            if scope_principal is not None:
                scoped["principal_id"] = scope_principal
                delegator = str(scoped.get("delegator_actor_id", "")).strip()
                if not delegator:
                    raise ValueError("delegator_actor_id is required")
                actor = self.store._actor(delegator)
                if actor["principal_id"] != scope_principal:
                    raise PermissionError(
                        "delegator actor must belong to your account"
                    )
                if context.actor_id is not None and context.actor_id != delegator:
                    raise PermissionError(
                        "node token can only delegate as its own actor"
                    )
            task, created = self.store.create_task(
                TaskSubmission.from_dict(scoped), tenant_id=tenant_id
            )
            return (
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {"task": task, "created": created},
            )
        if path == f"{self.prefix}/tasks/claim":
            actor_id = required_text(payload, "actor_id", maximum=200)
            node_id = required_text(payload, "node_id", maximum=200)
            if context is not None and not context.is_admin:
                if context.role != "node":
                    raise PermissionError("only node tokens can claim tasks")
                if context.actor_id != actor_id or context.node_id != node_id:
                    raise PermissionError("node token cannot claim for another node")
            try:
                wait_seconds = float(payload.get("wait_seconds", 0))
                lease_seconds = float(payload.get("lease_seconds", 120))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "wait_seconds and lease_seconds must be numbers"
                ) from exc
            claim = self.store.claim_task(
                actor_id=actor_id,
                node_id=node_id,
                wait_seconds=wait_seconds,
                lease_seconds=lease_seconds,
                tenant_id=tenant_id,
            )
            return HTTPStatus.OK, {"claim": claim}
        if path == f"{self.prefix}/runs":
            if context is not None and not context.is_admin:
                if context.role != "node":
                    raise PermissionError("only node tokens can start runs")
                if context.actor_id != required_text(payload, "actor_id", maximum=200):
                    raise PermissionError("node token cannot start a run for another actor")
                if context.node_id != required_text(payload, "node_id", maximum=200):
                    raise PermissionError("node token cannot start a run for another node")
                scoped_run = dict(payload)
                scoped_run["principal_id"] = context.principal_id
                run_payload = RunSubmission.from_dict(scoped_run)
            else:
                run_payload = RunSubmission.from_dict(payload)
            run = self.store.start_run(
                run_payload, tenant_id=tenant_id
            )
            return HTTPStatus.CREATED, {"run": run}
        if path == f"{self.prefix}/artifacts":
            artifact_payload = dict(payload)
            encoded = artifact_payload.pop("content_base64", None)
            if encoded is not None:
                if self.object_store is None:
                    raise ValueError("Hub object storage is not configured")
                if not isinstance(encoded, str):
                    raise ValueError("content_base64 must be a string")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("content_base64 is invalid") from exc
                stored = self.object_store.put(
                    content,
                    name=required_text(artifact_payload, "name", maximum=500),
                    media_type=required_text(
                        artifact_payload, "media_type", maximum=200
                    ),
                )
                artifact_payload["uri"] = stored.uri
                artifact_payload["sha256"] = stored.sha256
                artifact_payload["size_bytes"] = stored.size_bytes
            artifact = self.store.add_artifact(
                ArtifactSubmission.from_dict(artifact_payload), tenant_id=tenant_id
            )
            return HTTPStatus.CREATED, {"artifact": artifact}

        parts = self._parts(path)
        if len(parts) == 3 and parts[0] == "tenants" and parts[2] == "tokens":
            if context is not None and not context.is_admin:
                if context.role != "tenant_admin":
                    raise PermissionError("tenant_admin role required")
                if parts[1] != context.tenant_id:
                    raise PermissionError("cannot manage another tenant")
            item = AuthTokenCreation.from_dict(
                {**payload, "tenant_id": parts[1]}
            )
            raw, record = self.store.create_auth_token(item)
            return HTTPStatus.CREATED, {"token": record, "raw_token": raw}
        if len(parts) == 3 and parts[0] == "tokens" and parts[2] == "revoke":
            if context is not None and not context.is_admin:
                if context.role != "tenant_admin":
                    raise PermissionError("tenant_admin role required")
            record = self.store.revoke_auth_token(
                parts[1], tenant_id=tenant_id if not context.is_admin else None
            )
            return HTTPStatus.OK, {"token": record}
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "updates":
            task = self.store.update_task(
                parts[1], TaskUpdate.from_dict(payload), tenant_id=tenant_id
            )
            return HTTPStatus.OK, {"task": task}
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
            task = self.store.get_task(parts[1], tenant_id=tenant_id)
            self._assert_principal_owner(context, task)
            task = self.store.cancel_task(
                parts[1],
                actor_id=required_text(payload, "actor_id", maximum=200),
                reason=optional_text(payload, "reason", maximum=10_000),
                tenant_id=tenant_id,
            )
            return HTTPStatus.OK, {"task": task}
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "controls":
            task = self.store.get_task(parts[1], tenant_id=tenant_id)
            self._assert_principal_owner(context, task)
            control = self.store.create_task_control(
                parts[1],
                actor_id=required_text(payload, "actor_id", maximum=200),
                kind=required_text(payload, "kind", maximum=40),
                message=required_text(payload, "message", maximum=50_000),
                tenant_id=tenant_id,
            )
            return HTTPStatus.CREATED, {"control": control}
        if (
            len(parts) == 4
            and parts[0] == "tasks"
            and parts[2] == "controls"
            and parts[3] == "claim"
        ):
            controls = self.store.claim_task_controls(
                parts[1],
                run_id=required_text(payload, "run_id", maximum=200),
                lease_token=required_text(payload, "lease_token", maximum=200),
                limit=int(payload.get("limit", 20)),
                tenant_id=tenant_id,
            )
            return HTTPStatus.OK, {"controls": controls}
        if (
            len(parts) == 5
            and parts[0] == "tasks"
            and parts[2] == "controls"
            and parts[4] == "ack"
        ):
            control = self.store.acknowledge_task_control(
                parts[1],
                parts[3],
                run_id=required_text(payload, "run_id", maximum=200),
                lease_token=required_text(payload, "lease_token", maximum=200),
                tenant_id=tenant_id,
            )
            return HTTPStatus.OK, {"control": control}
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "updates":
            raw_status = required_text(payload, "status", maximum=40)
            try:
                status = RunStatus(raw_status)
            except ValueError as exc:
                raise ValueError("invalid run status") from exc
            run = self.store.update_run(
                parts[1],
                status=status,
                result=object_value(payload, "result"),
                error=optional_text(payload, "error", maximum=10_000),
                tenant_id=tenant_id,
            )
            return HTTPStatus.OK, {"run": run}
        return None

    def _tenant_scope(
        self, context: AuthenticatedContext | None
    ) -> str | None:
        if context is not None and not context.is_admin:
            return context.tenant_id or "default"
        return None

    def _principal_scope(
        self, context: AuthenticatedContext | None
    ) -> str | None:
        if context is None or context.is_admin:
            return None
        if context.role == "tenant_admin":
            return None
        return context.principal_id

    def _assert_principal_owner(
        self,
        context: AuthenticatedContext | None,
        record: dict[str, Any],
    ) -> None:
        scope = self._principal_scope(context)
        if scope is None:
            return
        if record.get("principal_id") != scope:
            raise LookupError("record not found")

    def _resolve_tenant(
        self, payload: dict[str, Any], context: AuthenticatedContext | None
    ) -> str:
        if context is not None and not context.is_admin:
            return context.tenant_id or "default"
        requested = payload.get("tenant_id")
        return str(requested).strip() if requested else "default"

    def _require_admin(self, context: AuthenticatedContext | None) -> None:
        if context is None or not context.is_admin:
            raise PermissionError("admin role required")

    def _require_tenant_manager(self, context: AuthenticatedContext | None) -> None:
        if context is None:
            return
        if not context.is_admin and context.role != "tenant_admin":
            raise PermissionError("tenant manager role required")

    def _parts(self, path: str) -> list[str]:
        prefix = f"{self.prefix}/"
        if not path.startswith(prefix):
            return []
        return [part for part in path[len(prefix) :].split("/") if part]
