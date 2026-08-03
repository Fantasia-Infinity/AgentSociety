from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO

from .service import SqliteChannelService


PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-03-26", PROTOCOL_VERSION})


TOOLS: list[dict[str, Any]] = [
    {
        "name": "channel_list_conversations",
        "title": "List channel conversations",
        "description": "List conversations available through a communication channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "default": "wechat"},
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "channel_read_messages",
        "title": "Read channel messages",
        "description": "Read normalized messages from one conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "default": "wechat"},
                "account_id": {"type": "string"},
                "conversation_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "before_timestamp": {"type": "integer"},
            },
            "required": ["account_id", "conversation_id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "channel_send",
        "title": "Send a channel message",
        "description": "Queue a text message for a channel conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "default": "wechat"},
                "account_id": {"type": "string"},
                "conversation_id": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["direct", "group"]},
                "content": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["account_id", "conversation_id", "content"],
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "channel_reply",
        "title": "Reply to a channel message",
        "description": "Queue a text reply to an existing normalized message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "default": "wechat"},
                "account_id": {"type": "string"},
                "message_id": {"type": "string"},
                "content": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["account_id", "message_id", "content"],
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "channel_react",
        "title": "React to a channel message",
        "description": "React when the selected adapter advertises that capability.",
        "inputSchema": {"type": "object", "additionalProperties": True},
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "channel_download",
        "title": "Download a channel attachment",
        "description": "Download an attachment when the selected adapter supports it.",
        "inputSchema": {"type": "object", "additionalProperties": True},
        "annotations": {"readOnlyHint": True},
    },
]


class ChannelMcpServer:
    def __init__(self, service: SqliteChannelService) -> None:
        self._service = service
        self._initialize_seen = False
        self._initialized = False

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        method = str(request["method"])
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        if method == "initialize":
            requested = str(params.get("protocolVersion", ""))
            if not requested:
                return self._error(request_id, -32602, "protocolVersion is required")
            self._initialize_seen = True
            negotiated = (
                requested
                if requested in SUPPORTED_PROTOCOL_VERSIONS
                else PROTOCOL_VERSION
            )
            return self._result(
                request_id,
                {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "agent-society-channel",
                        "title": "AgentSociety Channel Tools",
                        "version": "0.1.0",
                    },
                    "instructions": "Use channel capabilities before requesting adapter-specific operations.",
                },
            )
        if method == "notifications/initialized":
            if self._initialize_seen:
                self._initialized = True
            return None
        if method == "ping":
            return self._result(request_id, {})
        if not self._initialized:
            return self._error(request_id, -32002, "Server is not initialized")
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return self._error(request_id, -32601, "Method not found")

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "arguments must be an object")
        methods = {
            "channel_list_conversations": self._service.list_conversations,
            "channel_read_messages": self._service.read_messages,
            "channel_send": self._service.send,
            "channel_reply": self._service.reply,
            "channel_react": self._service.react,
            "channel_download": self._service.download,
        }
        method = methods.get(name)
        if method is None:
            return self._error(request_id, -32602, f"Unknown tool: {name}")
        try:
            value = method(**arguments)
            payload = {"result": value} if isinstance(value, list) else value
            return self._result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False),
                        }
                    ],
                    "structuredContent": payload,
                    "isError": False,
                },
            )
        except (LookupError, RuntimeError, TypeError, ValueError) as exc:
            return self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )

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


def serve(input_stream: TextIO, output_stream: TextIO, service: SqliteChannelService) -> None:
    server = ChannelMcpServer(service)
    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = server.handle(request)
        except (json.JSONDecodeError, ValueError) as exc:
            response = ChannelMcpServer._error(None, -32700, str(exc))
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()


def main() -> None:
    path = Path(os.environ.get("AGENT_CHANNEL_STATE_DB", os.environ.get("BOT_STATE_DB", "core-state.sqlite3"))).expanduser()
    service = SqliteChannelService(path)
    try:
        serve(sys.stdin, sys.stdout, service)
    finally:
        service.close()


if __name__ == "__main__":
    main()
