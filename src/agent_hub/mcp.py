from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .api import AgentHubApi
from .auth import AuthenticatedContext
from .domain import ActorRegistration, PrincipalRegistration
from .errors import ApiError


MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_PRINCIPAL_ID = "mcp-external"
MCP_ACTOR_ID = "mcp-gateway"


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "hub_list_actors",
        "description": "List human, agent, and service actors registered in the Hub.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "hub_list_nodes",
        "description": "List worker nodes currently registered with the Hub.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "hub_list_tasks",
        "description": "List durable tasks, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["submitted", "working", "completed", "failed", "cancelled"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_get_task",
        "description": "Read one task, including its result and artifact references.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "minLength": 1}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_get_task_events",
        "description": "Read the event stream of one task (submitted, claimed, working, completed, ...).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "after_seq": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_create_task",
        "description": "Create a durable task for a registered agent to claim and execute.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "minLength": 1},
                "principal_id": {"type": "string"},
                "delegator_actor_id": {"type": "string"},
                "assignee_actor_id": {"type": "string"},
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "input": {"type": "object"},
                "idempotency_key": {"type": "string"},
                "tenant_id": {"type": "string"},
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_cancel_task",
        "description": "Cancel an active Hub task. The worker executing it is expected to stop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]


class McpService:
    """Minimal MCP JSON-RPC 2.0 server mapping Hub tools to the existing API."""

    def __init__(self, api: AgentHubApi) -> None:
        self.api = api
        self._identity_tenants: set[str] = set()

    def handle_message(
        self,
        payload: Any,
        context: AuthenticatedContext | None,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        request_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        if method == "initialize":
            return self._result(request_id, self._initialize())
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(
                request_id, {"tools": TOOL_DEFINITIONS, "nextCursor": None}
            )
        if method == "tools/call":
            return self._result(request_id, self._tools_call(params, context))
        return self._error(request_id, -32601, f"Method not found: {method}")

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "AgentSociety Hub", "version": "0.4.0"},
            "instructions": (
                "AgentSociety Hub task coordination. Use hub_create_task to "
                "delegate work to registered agents, hub_get_task and "
                "hub_get_task_events to observe progress, and hub_cancel_task "
                "to stop active work."
            ),
        }

    def _tools_call(
        self,
        params: dict[str, Any],
        context: AuthenticatedContext | None,
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self._tool_error(-32602, "name and arguments object are required")
        try:
            if name == "hub_list_actors":
                _, result = self.api.get(
                    "/v1/hub/actors",
                    f"tenant_id={quote(self._tenant(context, arguments))}",
                    context,
                )
                return self._tool_result({"actors": result["actors"]})
            if name == "hub_list_nodes":
                _, result = self.api.get(
                    "/v1/hub/nodes",
                    f"tenant_id={quote(self._tenant(context, arguments))}",
                    context,
                )
                return self._tool_result({"nodes": result["nodes"]})
            if name == "hub_list_tasks":
                status = arguments.get("status")
                status = status if isinstance(status, str) and status else None
                limit = arguments.get("limit", 100)
                limit = limit if isinstance(limit, int) else 100
                query = [
                    f"limit={limit}",
                    f"tenant_id={quote(self._tenant(context, arguments))}",
                ]
                if status:
                    query.append(f"status={quote(status)}")
                _, result = self.api.get("/v1/hub/tasks", "&".join(query), context)
                return self._tool_result({"tasks": result["tasks"]})
            if name == "hub_get_task":
                task_id = self._required(arguments, "task_id")
                _, result = self.api.get(
                    f"/v1/hub/tasks/{quote(task_id, safe='')}",
                    f"tenant_id={quote(self._tenant(context, arguments))}",
                    context,
                )
                return self._tool_result({"task": result["task"]})
            if name == "hub_get_task_events":
                task_id = self._required(arguments, "task_id")
                after_seq = arguments.get("after_seq", 0)
                after_seq = after_seq if isinstance(after_seq, int) else 0
                limit = arguments.get("limit", 500)
                limit = limit if isinstance(limit, int) else 500
                _, result = self.api.get(
                    f"/v1/hub/tasks/{quote(task_id, safe='')}/events",
                    (
                        f"after_seq={after_seq}&limit={limit}"
                        f"&tenant_id={quote(self._tenant(context, arguments))}"
                    ),
                    context,
                )
                return self._tool_result({"events": result["events"]})
            if name == "hub_create_task":
                return self._tool_result(
                    {"task": self._create_task(arguments, context)}
                )
            if name == "hub_cancel_task":
                return self._tool_result(
                    {"task": self._cancel_task(arguments, context)}
                )
            return self._tool_error(-32601, f"Unknown tool: {name}")
        except (ApiError, ValueError) as exc:
            return self._tool_error(-32602, str(exc))

    def _create_task(
        self,
        arguments: dict[str, Any],
        context: AuthenticatedContext | None,
    ) -> dict[str, Any]:
        tenant_id = self._tenant(context, arguments)
        self._ensure_identity(tenant_id)
        payload = dict(arguments)
        payload.setdefault("principal_id", MCP_PRINCIPAL_ID)
        payload.setdefault("delegator_actor_id", MCP_ACTOR_ID)
        payload.setdefault("origin", "mcp")
        payload.setdefault("tenant_id", tenant_id)
        _, response = self.api.post("/v1/hub/tasks", payload, context)
        return response["task"]

    def _cancel_task(
        self,
        arguments: dict[str, Any],
        context: AuthenticatedContext | None,
    ) -> dict[str, Any]:
        tenant_id = self._tenant(context, arguments)
        self._ensure_identity(tenant_id)
        _, response = self.api.post(
            f"/v1/hub/tasks/{quote(self._required(arguments, 'task_id'), safe='')}/cancel",
            {
                "actor_id": arguments.get("actor_id") or MCP_ACTOR_ID,
                "reason": arguments.get("reason"),
                "tenant_id": tenant_id,
            },
            context,
        )
        return response["task"]

    def _tenant(
        self,
        context: AuthenticatedContext | None,
        arguments: dict[str, Any],
    ) -> str:
        if context is not None and not context.is_admin:
            return context.tenant_id or "default"
        requested = arguments.get("tenant_id")
        return str(requested).strip() if requested else "default"

    def _ensure_identity(self, tenant_id: str) -> None:
        if tenant_id in self._identity_tenants:
            return
        self.api.register_gateway_identity(
            PrincipalRegistration(
                principal_id=MCP_PRINCIPAL_ID,
                kind="service",
                display_name="MCP clients",
                metadata={"protocol": "mcp"},
            ),
            ActorRegistration(
                actor_id=MCP_ACTOR_ID,
                principal_id=MCP_PRINCIPAL_ID,
                kind="service",
                display_name="MCP gateway",
                capabilities=(),
                metadata={"protocol": "mcp", "version": MCP_PROTOCOL_VERSION},
            ),
            tenant_id=tenant_id,
        )
        self._identity_tenants.add(tenant_id)

    @staticmethod
    def _required(arguments: dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
        return value.strip()

    @staticmethod
    def _tool_result(value: Any) -> dict[str, Any]:
        import json

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(value, ensure_ascii=False),
                }
            ],
            "isError": False,
        }

    @staticmethod
    def _tool_error(code: int, message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
            "structuredContent": {"error": {"code": code, "message": message}},
        }

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
