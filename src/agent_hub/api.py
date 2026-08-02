from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs

from .domain import (
    ActorRegistration,
    ArtifactSubmission,
    NodeRegistration,
    PrincipalRegistration,
    RunStatus,
    RunSubmission,
    TaskSubmission,
    TaskUpdate,
    object_value,
    optional_text,
    required_text,
)
from .store import AgentHubStore


class AgentHubApi:
    """HTTP-independent router for the coordination API."""

    prefix = "/v1/hub"

    def __init__(self, store: AgentHubStore) -> None:
        self.store = store

    @classmethod
    def matches(cls, path: str) -> bool:
        return path == cls.prefix or path.startswith(f"{cls.prefix}/")

    def get(
        self, path: str, query_string: str
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        if path == self.prefix:
            return HTTPStatus.OK, {"status": "ok", **self.store.stats()}
        if path == f"{self.prefix}/principals":
            return HTTPStatus.OK, {"principals": self.store.list_principals()}
        if path == f"{self.prefix}/actors":
            return HTTPStatus.OK, {"actors": self.store.list_actors()}
        if path == f"{self.prefix}/nodes":
            return HTTPStatus.OK, {"nodes": self.store.list_nodes()}
        if path == f"{self.prefix}/tasks":
            query = parse_qs(query_string)
            status = (query.get("status") or [None])[0]
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError as exc:
                raise ValueError("limit must be an integer") from exc
            return HTTPStatus.OK, {
                "tasks": self.store.list_tasks(status=status, limit=limit)
            }

        parts = self._parts(path)
        if len(parts) == 2 and parts[0] == "tasks":
            return HTTPStatus.OK, {"task": self.store.get_task(parts[1])}
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
            return HTTPStatus.OK, {
                "events": self.store.list_task_events(parts[1])
            }
        if len(parts) == 2 and parts[0] == "runs":
            return HTTPStatus.OK, {"run": self.store.get_run(parts[1])}
        return None

    def post(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        if path == f"{self.prefix}/principals":
            item = self.store.register_principal(
                PrincipalRegistration.from_dict(payload)
            )
            return HTTPStatus.OK, {"principal": item}
        if path == f"{self.prefix}/actors":
            item = self.store.register_actor(ActorRegistration.from_dict(payload))
            return HTTPStatus.OK, {"actor": item}
        if path == f"{self.prefix}/nodes":
            item = self.store.register_node(NodeRegistration.from_dict(payload))
            return HTTPStatus.OK, {"node": item}
        if path == f"{self.prefix}/nodes/heartbeat":
            node_id = required_text(payload, "node_id", maximum=200)
            return HTTPStatus.OK, {"node": self.store.heartbeat_node(node_id)}
        if path == f"{self.prefix}/tasks":
            task, created = self.store.create_task(TaskSubmission.from_dict(payload))
            return (
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {"task": task, "created": created},
            )
        if path == f"{self.prefix}/tasks/claim":
            actor_id = required_text(payload, "actor_id", maximum=200)
            node_id = required_text(payload, "node_id", maximum=200)
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
            )
            return HTTPStatus.OK, {"claim": claim}
        if path == f"{self.prefix}/runs":
            run = self.store.start_run(RunSubmission.from_dict(payload))
            return HTTPStatus.CREATED, {"run": run}
        if path == f"{self.prefix}/artifacts":
            artifact = self.store.add_artifact(ArtifactSubmission.from_dict(payload))
            return HTTPStatus.CREATED, {"artifact": artifact}

        parts = self._parts(path)
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "updates":
            task = self.store.update_task(parts[1], TaskUpdate.from_dict(payload))
            return HTTPStatus.OK, {"task": task}
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
            task = self.store.cancel_task(
                parts[1],
                actor_id=required_text(payload, "actor_id", maximum=200),
                reason=optional_text(payload, "reason", maximum=10_000),
            )
            return HTTPStatus.OK, {"task": task}
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
            )
            return HTTPStatus.OK, {"run": run}
        return None

    def _parts(self, path: str) -> list[str]:
        prefix = f"{self.prefix}/"
        if not path.startswith(prefix):
            return []
        return [part for part in path[len(prefix) :].split("/") if part]
