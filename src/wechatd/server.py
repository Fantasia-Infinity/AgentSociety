from __future__ import annotations

import hashlib
import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import GatewayAction
from .runtime import WechatdRuntime


logger = logging.getLogger(__name__)


class WechatdHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        runtime: WechatdRuntime,
        api_token: str,
        max_request_bytes: int,
    ) -> None:
        super().__init__(address, WechatdRequestHandler)
        self.runtime = runtime
        self.api_token = api_token
        self.max_request_bytes = max_request_bytes


class WechatdRequestHandler(BaseHTTPRequestHandler):
    server: WechatdHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info(
            "wechatd_http %s %s",
            self.address_string(),
            format % args,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if not self._require_token():
                return
            if parsed.path == "/v1/status":
                self._json(HTTPStatus.OK, self.server.runtime.status())
                return
            if parsed.path == "/v1/chats":
                limit = self._int_param(query, "limit", default=100, minimum=1, maximum=500)
                self._json(
                    HTTPStatus.OK,
                    {"chats": self.server.runtime.list_chats(limit=limit)},
                )
                return
            if parsed.path == "/v1/messages":
                chat_id = self._str_param(query, "chat_id")
                after = query.get("after_message_id", [""])[0].strip() or None
                before = query.get("before_timestamp", [""])[0].strip() or None
                limit = self._int_param(query, "limit", default=50, minimum=1, maximum=500)
                before_timestamp = None
                if before:
                    try:
                        before_timestamp = float(before)
                    except ValueError as exc:
                        raise ValueError("before_timestamp must be a number") from exc
                messages = self.server.runtime.read_messages(
                    chat_id=chat_id,
                    after_message_id=after,
                    before_timestamp=before_timestamp,
                    limit=limit,
                )
                next_cursor = (
                    messages[-1].message_id if messages and before_timestamp is None else None
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "chat_id": chat_id,
                        "messages": [message.to_dict() for message in messages],
                        "next_cursor": next_cursor,
                    },
                )
                return
            if parsed.path == "/v1/message":
                message_id = self._str_param(query, "message_id")
                message = self.server.runtime.get_message(message_id)
                if message is None:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "not_found", "message": "message not found"},
                    )
                    return
                self._json(HTTPStatus.OK, {"message": message.to_dict()})
                return
            if parsed.path == "/v1/agent_cursor":
                chat_id = self._str_param(query, "chat_id")
                self._json(
                    HTTPStatus.OK,
                    {
                        "chat_id": chat_id,
                        "cursor": self.server.runtime.get_agent_cursor(chat_id),
                    },
                )
                return
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "unknown path"},
            )
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self._require_token():
                return
            if parsed.path != "/v1/send":
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "message": "unknown path"},
                )
                return
            payload = self._read_json()
            chat_id = str(payload.get("chat_id", "")).strip()
            content = str(payload.get("content", ""))
            if not chat_id:
                raise ValueError("chat_id is required")
            if not content.strip():
                raise ValueError("content is required")
            chat_type = str(payload.get("chat_type", "direct"))
            if chat_type not in {"direct", "group"}:
                raise ValueError("chat_type must be direct or group")
            idempotency_key = str(payload.get("idempotency_key", "")).strip()
            if idempotency_key:
                digest = hashlib.sha256(
                    f"wechatd\0{idempotency_key}".encode("utf-8")
                ).hexdigest()
                action_id = f"wechatd:{digest}"
            else:
                action_id = f"wechatd:{uuid.uuid4().hex}"
            action = GatewayAction(
                action_id=action_id,
                account_id=self.server.runtime.account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                content_type="text",
                content=content,
            )
            sent = self.server.runtime.send(action)
            self._json(
                HTTPStatus.OK,
                {
                    "action_id": action_id,
                    "sent": sent,
                    "duplicate": not sent,
                    "chat_id": chat_id,
                },
            )
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self._require_token():
                return
            if parsed.path != "/v1/agent_cursor":
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "message": "unknown path"},
                )
                return
            payload = self._read_json()
            chat_id = str(payload.get("chat_id", "")).strip()
            cursor = str(payload.get("cursor", "")).strip()
            if not chat_id:
                raise ValueError("chat_id is required")
            if not cursor:
                raise ValueError("cursor is required")
            self.server.runtime.set_agent_cursor(chat_id, cursor)
            self._json(
                HTTPStatus.OK,
                {"chat_id": chat_id, "cursor": cursor},
            )
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})

    def _require_token(self) -> bool:
        token = self.server.api_token
        if not token:
            return True
        expected = f"Bearer {token}"
        provided = self.headers.get("Authorization", "")
        if not hmac.compare_digest(provided, expected):
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthorized", "message": "invalid or missing token"},
            )
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            raw_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if raw_length <= 0:
            raise ValueError("request body is required")
        if raw_length > self.server.max_request_bytes:
            raise ValueError("request body too large")
        raw = self.rfile.read(raw_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _str_param(query: dict[str, list[str]], name: str) -> str:
        value = query.get(name, [""])[0].strip()
        if not value:
            raise ValueError(f"{name} is required")
        return value

    @staticmethod
    def _int_param(
        query: dict[str, list[str]],
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = query.get(name, [""])[0].strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if value < minimum or value > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value
