from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any


class TaskStatus(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)


def required_text(
    payload: dict[str, Any], name: str, *, maximum: int = 4096
) -> str:
    value = str(payload.get(name, "")).strip()
    if not value:
        raise ValueError(f"{name} is required")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def optional_text(
    payload: dict[str, Any], name: str, *, maximum: int = 4096
) -> str | None:
    raw = payload.get(name)
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def object_value(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def string_list(payload: dict[str, Any], name: str) -> tuple[str, ...]:
    value = payload.get(name, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    result = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if len(result) > 100:
        raise ValueError(f"{name} cannot contain more than 100 entries")
    if any(len(item) > 200 for item in result):
        raise ValueError(f"{name} entries cannot exceed 200 characters")
    return result


@dataclass(frozen=True, slots=True)
class PrincipalRegistration:
    principal_id: str
    kind: str
    display_name: str
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PrincipalRegistration":
        kind = required_text(payload, "kind", maximum=40)
        if kind not in {"human", "agent", "service", "organization"}:
            raise ValueError("kind must be human, agent, service, or organization")
        return cls(
            principal_id=required_text(payload, "principal_id", maximum=200),
            kind=kind,
            display_name=required_text(payload, "display_name", maximum=200),
            metadata=object_value(payload, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class ActorRegistration:
    actor_id: str
    principal_id: str
    kind: str
    display_name: str
    capabilities: tuple[str, ...]
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActorRegistration":
        kind = required_text(payload, "kind", maximum=40)
        if kind not in {"human", "agent", "service"}:
            raise ValueError("kind must be human, agent, or service")
        return cls(
            actor_id=required_text(payload, "actor_id", maximum=200),
            principal_id=required_text(payload, "principal_id", maximum=200),
            kind=kind,
            display_name=required_text(payload, "display_name", maximum=200),
            capabilities=string_list(payload, "capabilities"),
            metadata=object_value(payload, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class NodeRegistration:
    node_id: str
    actor_id: str
    display_name: str
    capabilities: tuple[str, ...]
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NodeRegistration":
        return cls(
            node_id=required_text(payload, "node_id", maximum=200),
            actor_id=required_text(payload, "actor_id", maximum=200),
            display_name=required_text(payload, "display_name", maximum=200),
            capabilities=string_list(payload, "capabilities"),
            metadata=object_value(payload, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class NodeWebRegistration:
    """Optional DSH Web capability advertised by one node.

    Metadata only: the Hub never dials the endpoint. A future outbound node
    tunnel owns the data path with an explicit path allowlist, so this
    registration cannot turn the Hub into an arbitrary SSRF proxy.
    """

    enabled: bool
    protocol_version: str | None
    dsh_version: str | None
    profile: str | None
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "protocol_version": self.protocol_version,
            "dsh_version": self.dsh_version,
            "profile": self.profile,
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NodeWebRegistration":
        raw_enabled = payload.get("enabled", False)
        if not isinstance(raw_enabled, bool):
            raise ValueError("web.enabled must be a boolean")
        return cls(
            enabled=raw_enabled,
            protocol_version=optional_text(payload, "protocol_version", maximum=80),
            dsh_version=optional_text(payload, "dsh_version", maximum=120),
            profile=optional_text(payload, "profile", maximum=200),
            capabilities=string_list(payload, "capabilities"),
        )


@dataclass(frozen=True, slots=True)
class TaskSubmission:
    principal_id: str
    delegator_actor_id: str
    objective: str
    assignee_actor_id: str | None
    context_id: str | None
    idempotency_key: str | None
    required_capabilities: tuple[str, ...]
    input: dict[str, Any]
    metadata: dict[str, Any]
    origin: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskSubmission":
        return cls(
            principal_id=required_text(payload, "principal_id", maximum=200),
            delegator_actor_id=required_text(
                payload, "delegator_actor_id", maximum=200
            ),
            objective=required_text(payload, "objective", maximum=50_000),
            assignee_actor_id=optional_text(
                payload, "assignee_actor_id", maximum=200
            ),
            context_id=optional_text(payload, "context_id", maximum=200),
            idempotency_key=optional_text(
                payload, "idempotency_key", maximum=200
            ),
            required_capabilities=string_list(payload, "required_capabilities"),
            input=object_value(payload, "input"),
            metadata=object_value(payload, "metadata"),
            origin=optional_text(payload, "origin", maximum=80) or "hub",
        )


@dataclass(frozen=True, slots=True)
class TaskUpdate:
    run_id: str
    lease_token: str
    status: TaskStatus
    message: str | None
    result: dict[str, Any]
    partial_result: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskUpdate":
        raw_status = required_text(payload, "status", maximum=40)
        try:
            status = TaskStatus(raw_status)
        except ValueError as exc:
            raise ValueError("invalid task status") from exc
        if status == TaskStatus.SUBMITTED:
            raise ValueError("a claimed task cannot return to submitted")
        return cls(
            run_id=required_text(payload, "run_id", maximum=200),
            lease_token=required_text(payload, "lease_token", maximum=200),
            status=status,
            message=optional_text(payload, "message", maximum=10_000),
            result=object_value(payload, "result"),
            partial_result=object_value(payload, "partial_result")
            if payload.get("partial_result") is not None
            else None,
        )


@dataclass(frozen=True, slots=True)
class RunSubmission:
    principal_id: str
    actor_id: str
    node_id: str
    origin: str
    objective: str | None
    task_id: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunSubmission":
        return cls(
            principal_id=required_text(payload, "principal_id", maximum=200),
            actor_id=required_text(payload, "actor_id", maximum=200),
            node_id=required_text(payload, "node_id", maximum=200),
            origin=required_text(payload, "origin", maximum=80),
            objective=optional_text(payload, "objective", maximum=50_000),
            task_id=optional_text(payload, "task_id", maximum=200),
            metadata=object_value(payload, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactSubmission:
    name: str
    media_type: str
    uri: str
    task_id: str | None
    run_id: str | None
    created_by_actor_id: str
    sha256: str | None
    size_bytes: int | None
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ArtifactSubmission":
        task_id = optional_text(payload, "task_id", maximum=200)
        run_id = optional_text(payload, "run_id", maximum=200)
        if task_id is None and run_id is None:
            raise ValueError("task_id or run_id is required")
        raw_size = payload.get("size_bytes")
        size_bytes = None if raw_size is None else int(raw_size)
        if size_bytes is not None and size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        sha256 = optional_text(payload, "sha256", maximum=64)
        if sha256 is not None and (
            len(sha256) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in sha256)
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return cls(
            name=required_text(payload, "name", maximum=500),
            media_type=required_text(payload, "media_type", maximum=200),
            uri=required_text(payload, "uri", maximum=4000),
            task_id=task_id,
            run_id=run_id,
            created_by_actor_id=required_text(
                payload, "created_by_actor_id", maximum=200
            ),
            sha256=sha256.lower() if sha256 is not None else None,
            size_bytes=size_bytes,
            metadata=object_value(payload, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class SharedEventAppend:
    """One entry in a principal's shared memory (consensus / directory / qa).

    The shared event log is append-only and idempotent (`event_id` derived
    from content by the writer). Entries expire when `ttl_hours` is set.
    """

    scope: str
    kind: str
    payload: dict[str, Any]
    principal_id: str
    session_id: str | None = None
    actor_id: str | None = None
    node_id: str | None = None
    ttl_hours: int | None = None
    event_id: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SharedEventAppend":
        scope = required_text(payload, "scope", maximum=40)
        if scope not in {"consensus", "directory", "qa"}:
            raise ValueError("scope must be consensus, directory, or qa")
        kind = required_text(payload, "kind", maximum=80)
        body = object_value(payload, "payload")
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
        if len(encoded) > 8192:
            raise ValueError("shared event payload exceeds 8192 characters")
        ttl = payload.get("ttl_hours")
        ttl_hours = None if ttl is None else int(ttl)
        if ttl_hours is not None and ttl_hours <= 0:
            raise ValueError("ttl_hours must be positive")
        return cls(
            scope=scope,
            kind=kind,
            payload=body,
            principal_id=required_text(payload, "principal_id", maximum=200),
            session_id=optional_text(payload, "session_id", maximum=200),
            actor_id=optional_text(payload, "actor_id", maximum=200),
            node_id=optional_text(payload, "node_id", maximum=200),
            ttl_hours=ttl_hours,
            event_id=optional_text(payload, "event_id", maximum=200),
        )


@dataclass(frozen=True, slots=True)
class TenantRegistration:
    tenant_id: str
    display_name: str
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TenantRegistration":
        return cls(
            tenant_id=required_text(payload, "tenant_id", maximum=200),
            display_name=required_text(payload, "display_name", maximum=200),
            metadata=object_value(payload, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class AuthTokenCreation:
    tenant_id: str
    role: str
    principal_id: str | None
    actor_id: str | None
    node_id: str | None
    label: str
    expires_at: float | None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AuthTokenCreation":
        role = required_text(payload, "role", maximum=40)
        if role not in {"tenant_admin", "tenant_user", "node"}:
            raise ValueError("role must be tenant_admin, tenant_user, or node")
        raw_expires = payload.get("expires_at")
        expires_at = None if raw_expires is None else float(raw_expires)
        return cls(
            tenant_id=required_text(payload, "tenant_id", maximum=200),
            role=role,
            principal_id=optional_text(payload, "principal_id", maximum=200),
            actor_id=optional_text(payload, "actor_id", maximum=200),
            node_id=optional_text(payload, "node_id", maximum=200),
            label=required_text(payload, "label", maximum=200),
            expires_at=expires_at,
        )
