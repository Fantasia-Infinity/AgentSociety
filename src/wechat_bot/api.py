from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .domain import IncomingMessage
from .persistence import (
    CoreInboxStore,
    SqliteActionOutbox,
    SqliteConversationStore,
    SqliteMessageDeduplicator,
)
from .runtime import BotRuntime
from .provider_routing import build_model_provider
from .service import AccessPolicy, BotService


logger = logging.getLogger(__name__)


def parse_action_poll_query(query_string: str) -> tuple[str, float, float]:
    query = parse_qs(query_string)
    account_id = (query.get("account_id") or [""])[0].strip()
    if not account_id:
        raise ValueError("account_id is required")
    try:
        timeout = min(max(float((query.get("timeout") or ["20"])[0]), 0), 30)
        lease_seconds = min(
            max(float((query.get("lease_seconds") or ["30"])[0]), 1),
            300,
        )
    except ValueError as exc:
        raise ValueError("invalid timeout or lease_seconds") from exc
    return account_id, timeout, lease_seconds


def parse_action_ack_payload(payload: dict[str, Any]) -> tuple[str, list[str]]:
    account_id = str(payload.get("account_id", "")).strip()
    raw_action_ids = payload.get("action_ids")
    if not account_id:
        raise ValueError("account_id is required")
    if not isinstance(raw_action_ids, list) or not raw_action_ids:
        raise ValueError("action_ids must be a non-empty array")
    action_ids = [str(item).strip() for item in raw_action_ids]
    if any(not item for item in action_ids):
        raise ValueError("action_ids cannot contain empty values")
    return account_id, action_ids


class BotHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        runtime: BotRuntime,
        api_token: str,
    ):
        super().__init__(address, BotRequestHandler)
        self.runtime = runtime
        self.api_token = api_token


class BotRequestHandler(BaseHTTPRequestHandler):
    server: BotHttpServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, self.server.runtime.health())
            return
        if parsed.path == "/v1/actions":
            if not self._authorized():
                return
            try:
                account_id, timeout, lease_seconds = parse_action_poll_query(
                    parsed.query
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            actions = self.server.runtime.poll_actions(
                account_id,
                timeout=timeout,
                lease_seconds=lease_seconds,
            )
            self._send_json(HTTPStatus.OK, {"actions": [item.to_dict() for item in actions]})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/events/wechat":
            if not self._authorized():
                return
            try:
                payload = self._read_json()
                message = IncomingMessage.from_dict(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            result = self.server.runtime.submit(message)
            status = HTTPStatus.ACCEPTED if result.accepted else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, {"accepted": result.accepted, "reason": result.reason})
            return

        if parsed.path == "/v1/actions/ack":
            if not self._authorized():
                return
            try:
                payload = self._read_json()
                account_id, action_ids = parse_action_ack_payload(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            acked = self.server.runtime.ack_actions(account_id, action_ids)
            self._send_json(HTTPStatus.OK, {"acked": acked})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("http %s", format % args)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.api_token}"
        if not hmac.compare_digest(supplied, expected):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be between 1 byte and 1 MB")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def build_runtime(settings: Settings) -> BotRuntime:
    provider = build_model_provider(settings)
    inbox = CoreInboxStore(settings.state_db)
    conversations = SqliteConversationStore(
        settings.state_db, settings.max_history_messages
    )
    deduplicator = SqliteMessageDeduplicator(settings.state_db)
    action_outbox = SqliteActionOutbox(settings.state_db)
    policy = AccessPolicy(
        allowed_users=settings.allowed_users,
        allowed_groups=settings.allowed_groups,
        group_require_mention=settings.group_require_mention,
    )
    service = BotService(
        provider=provider,
        conversations=conversations,
        policy=policy,
        system_prompt=settings.system_prompt,
        deduplicator=deduplicator,
    )
    return BotRuntime(
        service,
        workers=settings.workers,
        queue_size=settings.queue_size,
        inbox=inbox,
        action_outbox=action_outbox,
        model_provider=provider,
        closeables=(inbox, conversations, deduplicator, action_outbox),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    runtime = build_runtime(settings)
    runtime.start()
    server = BotHttpServer((settings.api_host, settings.api_port), runtime, settings.api_token)
    logger.info(
        "bot_core_started host=%s port=%s llm_backend=%s",
        settings.api_host,
        settings.api_port,
        settings.llm_backend,
    )
    model_health = runtime.health().get("model", {})
    if isinstance(model_health, dict):
        logger.info(
            "model_health backend=%s status=%s",
            model_health.get("backend", settings.llm_backend),
            model_health.get("status", "unknown"),
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        server.shutdown()
        server.server_close()
        runtime.stop()


if __name__ == "__main__":
    main()
