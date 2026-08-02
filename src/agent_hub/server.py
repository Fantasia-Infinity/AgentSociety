from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import urlparse

from .api import AgentHubApi
from .config import HubSettings
from .store import AgentHubStore


logger = logging.getLogger(__name__)


class HubHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        api: AgentHubApi,
        api_token: str,
    ) -> None:
        super().__init__(address, HubRequestHandler)
        self.api = api
        self.api_token = api_token


class HubRequestHandler(BaseHTTPRequestHandler):
    server: HubHttpServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if not AgentHubApi.matches(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authorized():
            return
        try:
            response = self.server.api.get(parsed.path, parsed.query)
        except (LookupError, PermissionError, ValueError) as exc:
            self._send_api_error(exc)
            return
        if response is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(*response)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not AgentHubApi.matches(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authorized():
            return
        try:
            payload = self._read_json()
            response = self.server.api.post(parsed.path, payload)
        except (json.JSONDecodeError, LookupError, PermissionError, ValueError) as exc:
            self._send_api_error(exc)
            return
        if response is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(*response)

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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_api_error(self, error: Exception) -> None:
        if isinstance(error, LookupError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(error, PermissionError):
            status = HTTPStatus.CONFLICT
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_json(status, {"error": str(error)})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = HubSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    store = AgentHubStore(settings.state_db)
    api = AgentHubApi(store)
    server = HubHttpServer(
        (settings.api_host, settings.api_port), api, settings.api_token
    )
    logger.info(
        "agent_hub_started host=%s port=%s state_db=%s",
        settings.api_host,
        settings.api_port,
        settings.state_db,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
