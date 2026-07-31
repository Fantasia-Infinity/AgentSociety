from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .conversations import InMemoryConversationStore
from .domain import IncomingMessage
from .openai_compatible import OpenAICompatibleProvider
from .runtime import BotRuntime
from .service import AccessPolicy, BotService


logger = logging.getLogger(__name__)


class BotHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runtime: BotRuntime, api_token: str):
        super().__init__(address, BotRequestHandler)
        self.runtime = runtime
        self.api_token = api_token


class BotRequestHandler(BaseHTTPRequestHandler):
    server: BotHttpServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "queue_depth": self.server.runtime.queue_depth()},
            )
            return
        if parsed.path == "/v1/actions":
            if not self._authorized():
                return
            query = parse_qs(parsed.query)
            account_id = (query.get("account_id") or [""])[0].strip()
            if not account_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "account_id is required"})
                return
            try:
                timeout = min(max(float((query.get("timeout") or ["20"])[0]), 0), 30)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid timeout"})
                return
            actions = self.server.runtime.poll_actions(account_id, timeout=timeout)
            self._send_json(HTTPStatus.OK, {"actions": [item.to_dict() for item in actions]})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/v1/events/wechat":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
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
    provider = OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    conversations = InMemoryConversationStore(settings.max_history_messages)
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
    )
    return BotRuntime(service, workers=settings.workers, queue_size=settings.queue_size)


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
    logger.info("bot_core_started host=%s port=%s", settings.api_host, settings.api_port)
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

