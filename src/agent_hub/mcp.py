from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from .api import AgentHubApi
from .auth import AuthenticatedContext
from .domain import ActorRegistration, PrincipalRegistration
from .errors import ApiError


MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {"2025-03-26", "2025-06-18", MCP_PROTOCOL_VERSION}
)
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
    {
        "name": "hub_context_append",
        "description": (
            "Append one entry to the shared consensus context of your "
            "principal (facts, decisions, session digests). Entries are "
            "idempotent via event_id and expire after ttl_hours. USE THIS "
            "when you reach a reusable conclusion (a fact, a decision, a "
            "result) during a task, so other sessions and devices do not "
            "have to ask again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "payload": {"type": "object"},
                "session_id": {"type": "string"},
                "event_id": {"type": "string"},
                "ttl_hours": {"type": "integer"},
            },
            "required": ["kind", "payload"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_context_read",
        "description": (
            "Read the shared consensus context of your principal. "
            "Defaults to the NEWEST entries (latest first, up to limit); "
            "pass after_seq to pull only newer entries (incremental sync). "
            "The result includes latest_seq so you know the true head of "
            "the log. USE THIS at the start of work or whenever you need "
            "facts, decisions, or results produced by other sessions before "
            "asking anyone. The runtime-context injection only shows "
            "one-line summaries; this tool returns the full entries "
            "(objective/result text), so use it before deciding to ask "
            "a session anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_seq": {"type": "integer"},
                "kind": {"type": "string"},
                "session_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_directory_list",
        "description": (
            "List the session/agent directory of your principal: one row per "
            "session (id, actor, node, title, workspace, status, last active). "
            "USE THIS to discover which sessions/agents exist and what they "
            "are doing, e.g. to find who can help or who owns a workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "status": {"type": "string"},
                "actor_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_directory_get",
        "description": (
            "Drill into one session directory row. depth 0 = identity, "
            "1 = invocation records, 2 = consensus digest, 3 = artifact refs. "
            "USE THIS after hub_directory_list/search to inspect a specific "
            "session before deciding to ask it anything: depth 2 shows the "
            "session's digests/conclusions - enough to judge whether it "
            "really holds what you need; if it does, follow up with hub_ask "
            "carrying that session_id as target_session_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "depth": {"type": "integer"},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_directory_search",
        "description": (
            "Search the session/agent directory by title, workspace, or "
            "objective text. USE THIS when you need to find the session or "
            "agent that worked on a topic or lives in a workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_ask",
        "description": (
            "Ask another agent/session of your principal a question and "
            "BLOCK until the answer arrives (default 60s, max 300s). "
            "Returns the answer text, or timeout/expired/unsupported/declined. "
            "USE THIS ONLY AFTER hub_context_read and hub_directory_search "
            "could not answer: prefer shared memory first, then a specific "
            "target from the directory. Ask concrete questions; the answer "
            "returns into your current turn. "
            "Pass target_session_id to have the question answered INSIDE that "
            "session's own context (its history becomes the prompt prefix; the "
            "question and answer are appended back to that session) - ideal "
            "when the session already holds the relevant context. Without "
            "target_session_id the answerer picks the target actor's most "
            "recent idle session automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_actor_id": {"type": "string"},
                "target_session_id": {"type": "string"},
                "message": {"type": "string"},
                "require": {"type": "string"},
                "asker_session_id": {"type": "string"},
                "asker_task_id": {"type": "string"},
                "wait_seconds": {"type": "integer"},
            },
            "required": ["target_actor_id", "message"],
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
            requested = str(params.get("protocolVersion", ""))
            negotiated = (
                requested
                if requested in SUPPORTED_PROTOCOL_VERSIONS
                else MCP_PROTOCOL_VERSION
            )
            return self._result(request_id, self._initialize(negotiated))
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            return self._result(request_id, self._tools_call(params, context))
        if method == "resources/list":
            return self._result(request_id, {"resources": []})
        if method == "resources/templates/list":
            return self._result(request_id, {"resourceTemplates": []})
        return self._error(request_id, -32601, f"Method not found: {method}")

    def _initialize(self, protocol_version: str) -> dict[str, Any]:
        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
            },
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
            if name == "hub_context_append":
                tenant_id = self._tenant(context, arguments)
                self._ensure_identity(tenant_id)
                payload = dict(arguments)
                payload.setdefault("scope", "consensus")
                payload.setdefault(
                    "principal_id", self._gateway_principal_id(tenant_id)
                )
                payload.setdefault("actor_id", self._gateway_actor_id(tenant_id))
                _, result = self.api.post(
                    "/v1/hub/contexts/append", payload, context
                )
                return self._tool_result({"event": result["event"]})
            if name == "hub_context_read":
                query_args = {
                    key: value
                    for key, value in arguments.items()
                    if value is not None
                }
                # Default to the newest entries (descending) unless an
                # incremental pull is requested; latest_seq tells the caller
                # how far the head of the log is.
                if "after_seq" not in query_args:
                    query_args.setdefault("order", "desc")
                query = "&".join(
                    f"{key}={quote(str(value))}"
                    for key, value in query_args.items()
                )
                _, result = self.api.get("/v1/hub/contexts", query, context)
                return self._tool_result(
                    {
                        "events": result["events"],
                        "latest_seq": result.get("latest_seq"),
                    }
                )
            if name == "hub_directory_list":
                query = "&".join(
                    f"{key}={quote(str(value))}"
                    for key, value in arguments.items()
                    if value is not None
                )
                _, result = self.api.get("/v1/hub/directory", query, context)
                return self._tool_result({"rows": result["rows"]})
            if name == "hub_directory_get":
                session_id = self._required(arguments, "session_id")
                depth = arguments.get("depth", 1)
                depth = depth if isinstance(depth, int) else 1
                _, result = self.api.get(
                    f"/v1/hub/directory/{quote(session_id, safe='')}",
                    f"depth={min(max(depth, 0), 3)}",
                    context,
                )
                return self._tool_result({"row": result["row"]})
            if name == "hub_directory_search":
                query_value = self._required(arguments, "query")
                limit = arguments.get("limit", 20)
                limit = limit if isinstance(limit, int) else 20
                _, result = self.api.get(
                    "/v1/hub/directory",
                    f"query={quote(query_value)}&limit={limit}",
                    context,
                )
                return self._tool_result({"rows": result["rows"]})
            if name == "hub_ask":
                return self._tool_result({"answer": self._ask(arguments, context)})
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
        payload.setdefault("principal_id", self._gateway_principal_id(tenant_id))
        payload.setdefault(
            "delegator_actor_id", self._gateway_actor_id(tenant_id)
        )
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
                "actor_id": arguments.get("actor_id")
                or self._gateway_actor_id(tenant_id),
                "reason": arguments.get("reason"),
                "tenant_id": tenant_id,
            },
            context,
        )
        return response["task"]

    def _gateway_principal_id(self, tenant_id: str) -> str:
        if tenant_id == "default":
            return MCP_PRINCIPAL_ID
        return f"{MCP_PRINCIPAL_ID}-{_safe_tenant_suffix(tenant_id)}"

    def _gateway_actor_id(self, tenant_id: str) -> str:
        if tenant_id == "default":
            return MCP_ACTOR_ID
        return f"{MCP_ACTOR_ID}-{_safe_tenant_suffix(tenant_id)}"

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
                principal_id=self._gateway_principal_id(tenant_id),
                kind="service",
                display_name="MCP clients",
                metadata={"protocol": "mcp"},
            ),
            ActorRegistration(
                actor_id=self._gateway_actor_id(tenant_id),
                principal_id=self._gateway_principal_id(tenant_id),
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

    def _ask(
        self,
        arguments: dict[str, Any],
        context: AuthenticatedContext | None,
    ) -> dict[str, Any]:
        """Create a question and block until it is answered (bounded).

        Pass `question_id` to resume waiting on an existing question (e.g.
        after a timeout); the question must belong to the caller's principal.
        """
        tenant_id = self._tenant(context, arguments)
        self._ensure_identity(tenant_id)
        existing = arguments.get("question_id")
        if isinstance(existing, str) and existing.strip():
            question_id = existing.strip()
            _, current = self.api.get(
                f"/v1/hub/questions/{quote(question_id, safe='')}",
                "",
                context,
            )
            question = current["question"]
        else:
            payload = dict(arguments)
            payload.pop("wait_seconds", None)
            payload.setdefault("principal_id", self._gateway_principal_id(tenant_id))
            payload.setdefault("asker_actor_id", self._gateway_actor_id(tenant_id))
            _, result = self.api.post("/v1/hub/questions", payload, context)
            question = result["question"]
            question_id = str(question["question_id"])
            if question["status"] == "unsupported":
                return {
                    "status": "unsupported",
                    "question_id": question_id,
                    "answer": None,
                    "detail": "target actor has no online node",
                }
        raw_wait = arguments.get("wait_seconds", 60)
        wait_seconds = raw_wait if isinstance(raw_wait, int) else 60
        wait_seconds = min(max(wait_seconds, 1), 300)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(1)
            _, current = self.api.get(
                f"/v1/hub/questions/{quote(question_id, safe='')}",
                "",
                context,
            )
            status = str(current["question"]["status"])
            if status == "answered":
                return {
                    "status": "answered",
                    "question_id": question_id,
                    "answer": current["question"].get("answer_text"),
                    "answered_by": current["question"].get("target_actor_id"),
                }
            if status in {"expired", "unsupported", "declined"}:
                return {
                    "status": status,
                    "question_id": question_id,
                    "answer": None,
                }
        return {
            "status": "timeout",
            "question_id": question_id,
            "answer": None,
            "detail": f"no answer within {wait_seconds}s; the question stays pending",
        }

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


def _safe_tenant_suffix(tenant_id: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in tenant_id
    )[:40]
