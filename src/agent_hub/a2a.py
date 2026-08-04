from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
import json
import uuid
from typing import Any
from urllib.parse import quote

from .api import AgentHubApi
from .domain import (
    TaskStatus,
)
from .errors import ApiError


A2A_VERSION = "1.0"


class A2AError(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class A2AApi:
    principal_id = "a2a-external"
    actor_id = "a2a-gateway"

    def __init__(self, api: AgentHubApi) -> None:
        self.api = api
        self._ensure_identity()

    def agent_card(self, base_url: str) -> dict[str, Any]:
        return {
            "name": "AgentSociety Hub",
            "description": "Durable task delegation to AgentSociety agents and devices.",
            "supportedInterfaces": [
                {
                    "url": f"{base_url.rstrip('/')}/a2a",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": A2A_VERSION,
                }
            ],
            "version": "0.1.0",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "extendedAgentCard": False,
            },
            "securitySchemes": {
                "hubBearer": {
                    "httpAuthSecurityScheme": {
                        "scheme": "Bearer",
                        "description": "The existing AgentSociety Hub token.",
                    }
                }
            },
            "securityRequirements": [
                {"schemes": {"hubBearer": {"list": []}}}
            ],
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "skills": [
                {
                    "id": "durable-task-delegation",
                    "name": "Durable task delegation",
                    "description": "Delegate, inspect, continue, and cancel work executed by registered agents.",
                    "tags": ["agents", "tasks", "delegation", "collaboration"],
                    "examples": ["Inspect the repository and report failing tests."],
                }
            ],
        }

    def handle(self, request: dict[str, Any], *, version: str) -> dict[str, Any]:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        if version != A2A_VERSION:
            return self._error(
                request_id,
                -32009,
                f"A2A version {version or '0.3'} is not supported; use {A2A_VERSION}",
            )
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid parameters")
        try:
            method = str(request["method"])
            if method == "SendMessage":
                result = self._send_message(params)
            elif method == "GetTask":
                result = self._get_task(params)
            elif method == "ListTasks":
                result = self._list_tasks(params)
            elif method == "CancelTask":
                result = self._cancel_task(params)
            else:
                return self._error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ApiError as exc:
            if exc.status in (HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT):
                return self._error(request_id, -32001, "Task not found")
            return self._error(request_id, -32602, str(exc))
        except A2AError as exc:
            return self._error(request_id, exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return self._error(request_id, -32602, str(exc))

    def _send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise A2AError(-32602, "message is required")
        if str(message.get("role", "")) not in {"ROLE_USER", "user"}:
            raise A2AError(-32602, "message role must be ROLE_USER")
        objective = self._parts_text(message.get("parts"))
        message_id = str(message.get("messageId") or uuid.uuid4()).strip()
        if not message_id:
            raise A2AError(-32602, "messageId cannot be empty")
        task_id = str(message.get("taskId") or "").strip()
        if task_id:
            _, response = self.api.get(
                f"/v1/hub/tasks/{quote(task_id, safe='')}", "", None
            )
            task = response["task"]
            if TaskStatus(task["status"]) in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                raise A2AError(-32004, "terminal task cannot accept messages")
            context_id = str(message.get("contextId") or "").strip()
            if context_id and context_id != str(task.get("context_id") or ""):
                raise A2AError(-32602, "contextId does not match task")
            self.api.post(
                f"/v1/hub/tasks/{quote(task_id, safe='')}/controls",
                {
                    "actor_id": self.actor_id,
                    "kind": "follow_up",
                    "message": objective,
                },
                None,
            )
            return {"task": self._task(task)}

        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        society = metadata.get("agentSociety")
        society = society if isinstance(society, dict) else {}
        required = society.get("requiredCapabilities")
        required_capabilities = (
            tuple(str(item) for item in required)
            if isinstance(required, list)
            else ()
        )
        submission = {
            "principal_id": self.principal_id,
            "delegator_actor_id": self.actor_id,
            "objective": objective,
            "assignee_actor_id": (
                str(society["assigneeActorId"])
                if society.get("assigneeActorId")
                else None
            ),
            "context_id": str(message.get("contextId") or uuid.uuid4()),
            "idempotency_key": f"a2a:{message_id}",
            "required_capabilities": list(required_capabilities),
            "input": (
                society.get("input") if isinstance(society.get("input"), dict) else {}
            ),
            "metadata": {"a2a_message_id": message_id},
            "origin": "a2a",
        }
        _, response = self.api.post("/v1/hub/tasks", submission, None)
        task = response["task"]
        return {"task": self._task(task)}

    def _get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or "").strip()
        if not task_id:
            raise A2AError(-32602, "id is required")
        _, response = self.api.get(
            f"/v1/hub/tasks/{quote(task_id, safe='')}", "", None
        )
        task = self._task(response["task"])
        if int(params.get("historyLength", 1)) == 0:
            task.pop("history", None)
        return {"task": task}

    def _list_tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        page_size = min(max(int(params.get("pageSize", 50)), 1), 100)
        try:
            offset = max(int(params.get("pageToken") or 0), 0)
        except (TypeError, ValueError) as exc:
            raise A2AError(-32602, "pageToken is invalid") from exc
        raw_status = str(params.get("status") or "").strip()
        status = self._from_a2a_state(raw_status) if raw_status else None
        context_id = str(params.get("contextId") or "").strip()
        query = "limit=500"
        if status:
            query += f"&status={quote(status)}"
        _, response = self.api.get("/v1/hub/tasks", query, None)
        tasks = response["tasks"]
        if context_id:
            tasks = [task for task in tasks if task.get("context_id") == context_id]
        selected = tasks[offset : offset + page_size]
        next_offset = offset + len(selected)
        return {
            "tasks": [self._task(task) for task in selected],
            "totalSize": len(tasks),
            "pageSize": page_size,
            "nextPageToken": str(next_offset) if next_offset < len(tasks) else "",
        }

    def _cancel_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or "").strip()
        if not task_id:
            raise A2AError(-32602, "id is required")
        _, response = self.api.post(
            f"/v1/hub/tasks/{quote(task_id, safe='')}/cancel",
            {"actor_id": self.actor_id, "reason": "Cancelled through A2A"},
            None,
        )
        task = response["task"]
        return {"task": self._task(task)}

    def _task(self, task: dict[str, Any]) -> dict[str, Any]:
        timestamp = datetime.fromtimestamp(
            float(task["updated_at"]), tz=UTC
        ).isoformat().replace("+00:00", "Z")
        result: dict[str, Any] = {
            "id": task["task_id"],
            "contextId": task.get("context_id"),
            "status": {
                "state": self._to_a2a_state(str(task["status"])),
                "timestamp": timestamp,
            },
            "history": [
                {
                    "messageId": str(task.get("metadata", {}).get("a2a_message_id") or task["task_id"]),
                    "contextId": task.get("context_id"),
                    "taskId": task["task_id"],
                    "role": "ROLE_USER",
                    "parts": [{"text": task["objective"], "mediaType": "text/plain"}],
                }
            ],
            "metadata": {
                "agentSociety": {
                    "assigneeActorId": task.get("assignee_actor_id"),
                    "executorActorId": task.get("executor_actor_id"),
                    "executorNodeId": task.get("executor_node_id"),
                    "attempts": task.get("attempts", 0),
                }
            },
        }
        artifacts: list[dict[str, Any]] = []
        text = task.get("result", {}).get("text")
        if isinstance(text, str) and text:
            artifacts.append(
                {
                    "artifactId": f"result-{task['task_id']}",
                    "name": "Agent result",
                    "parts": [{"text": text, "mediaType": "text/plain"}],
                }
            )
        for artifact in task.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            artifacts.append(
                {
                    "artifactId": artifact["artifact_id"],
                    "name": artifact.get("name"),
                    "parts": [
                        {
                            "url": artifact["uri"],
                            "filename": artifact.get("name"),
                            "mediaType": artifact.get("media_type"),
                            "metadata": {
                                "sha256": artifact.get("sha256"),
                                "sizeBytes": artifact.get("size_bytes"),
                            },
                        }
                    ],
                }
            )
        if artifacts:
            result["artifacts"] = artifacts
        error = task.get("error")
        if isinstance(error, str) and error:
            result["status"]["message"] = {
                "messageId": f"status-{task['task_id']}",
                "contextId": task.get("context_id"),
                "taskId": task["task_id"],
                "role": "ROLE_AGENT",
                "parts": [{"text": error, "mediaType": "text/plain"}],
            }
        return result

    @staticmethod
    def _parts_text(value: Any) -> str:
        if not isinstance(value, list) or not value:
            raise A2AError(-32602, "message parts are required")
        texts = []
        for part in value:
            if not isinstance(part, dict):
                raise A2AError(-32602, "message parts must be objects")
            if "text" in part:
                texts.append(str(part["text"]))
            elif "data" in part:
                texts.append(json.dumps(part["data"], ensure_ascii=False))
            else:
                raise A2AError(-32005, "only text and data parts are supported")
        objective = "\n".join(texts).strip()
        if not objective:
            raise A2AError(-32602, "message content cannot be empty")
        return objective

    @staticmethod
    def _to_a2a_state(status: str) -> str:
        return {
            "submitted": "TASK_STATE_SUBMITTED",
            "working": "TASK_STATE_WORKING",
            "completed": "TASK_STATE_COMPLETED",
            "failed": "TASK_STATE_FAILED",
            "cancelled": "TASK_STATE_CANCELED",
        }[status]

    @staticmethod
    def _from_a2a_state(status: str) -> str:
        mapping = {
            "TASK_STATE_SUBMITTED": "submitted",
            "TASK_STATE_WORKING": "working",
            "TASK_STATE_COMPLETED": "completed",
            "TASK_STATE_FAILED": "failed",
            "TASK_STATE_CANCELED": "cancelled",
        }
        if status not in mapping:
            raise A2AError(-32602, "unsupported task state")
        return mapping[status]

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _ensure_identity(self) -> None:
        from .domain import ActorRegistration, PrincipalRegistration

        self.api.register_gateway_identity(
            PrincipalRegistration(
                principal_id=self.principal_id,
                kind="service",
                display_name="A2A clients",
                metadata={"protocol": "a2a"},
            ),
            ActorRegistration(
                actor_id=self.actor_id,
                principal_id=self.principal_id,
                kind="service",
                display_name="A2A gateway",
                capabilities=(),
                metadata={"protocol": "a2a", "version": A2A_VERSION},
            ),
        )
