from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import AuthenticatedContext, OIDCIdentityProvider
from .api import AgentHubApi
from .a2a import A2AApi
from .config import HubSettings
from .errors import ApiError, map_error
from .mcp import MCP_PROTOCOL_VERSION, McpService
from .ratelimit import AuthRateLimiter
from .store import AgentHubStore
from .object_store import build_object_store
from .web import WebSession
from .web.handlers import WebHandlersMixin


logger = logging.getLogger(__name__)


class HubHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        api: AgentHubApi,
        api_token: str,
        public_url: str | None = None,
        web_secret: str | None = None,
        web_cookie_secure: bool = True,
        disable_bootstrap: bool = False,
        oidc_provider: OIDCIdentityProvider | None = None,
        enable_mcp: bool = True,
        rate_limiter: AuthRateLimiter | None = None,
    ) -> None:
        super().__init__(address, HubRequestHandler)
        self.api = api
        self.a2a = A2AApi(api)
        self.mcp = McpService(api) if enable_mcp else None
        self.api_token = api_token
        self.public_url = public_url.rstrip("/") if public_url else None
        self.web = WebSession(web_secret) if web_secret is not None else None
        self.web_cookie_secure = web_cookie_secure
        self.disable_bootstrap = disable_bootstrap
        self.oidc_provider = oidc_provider
        self.rate_limiter = rate_limiter


class HubRequestHandler(WebHandlersMixin, BaseHTTPRequestHandler):
    server: HubHttpServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if self.server.web is not None and parsed.path.startswith("/web"):
            self._web_get(parsed.path, parsed.query)
            return
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/mcp":
            if self.server.mcp is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "mcp_disabled"})
            else:
                self._send_mcp_endpoint()
            return
        if parsed.path == "/.well-known/agent-card.json":
            self._send_json(
                HTTPStatus.OK,
                self.server.a2a.agent_card(self._base_url()),
                content_type="application/a2a+json",
                cache_control="public, max-age=300",
            )
            return
        if not AgentHubApi.matches(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        context = self._authorized()
        if context is None:
            return
        try:
            response = self.server.api.get(parsed.path, parsed.query, context)
        except ApiError as exc:
            self._send_api_error(exc)
            return
        if response is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(*response)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if self._rate_limited(parsed.path):
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "rate limited; try again later"},
            )
            return
        if self.server.web is not None and parsed.path.startswith("/web"):
            self._web_post(parsed.path)
            return
        if parsed.path == "/mcp":
            if self.server.mcp is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "mcp_disabled"})
                return
            context = self._authorized()
            if context is None:
                return
            try:
                payload = self._read_json()
                response = self.server.mcp.handle_message(payload, context)
            except (json.JSONDecodeError, ValueError) as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(exc)},
                }
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_mcp_json(response)
            return
        if parsed.path == "/a2a":
            context = self._authorized()
            if context is None:
                return
            if not context.is_admin:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "a2a requires an admin token"},
                )
                return
            try:
                payload = self._read_json()
                response = self.server.a2a.handle(
                    payload, version=self.headers.get("A2A-Version", "")
                )
            except json.JSONDecodeError as exc:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": str(exc)},
                    },
                    content_type="application/a2a+json",
                )
                return
            except ValueError as exc:
                self._send_api_error(exc)
                return
            self._send_json(
                HTTPStatus.OK, response, content_type="application/a2a+json"
            )
            return
        if AgentHubApi.is_public_auth_post(parsed.path):
            try:
                payload = self._read_json()
                response = self.server.api.post(parsed.path, payload, None)
            except (json.JSONDecodeError, ApiError) as exc:
                self._send_api_error(exc)
                return
            if response is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(*response)
            return
        if not AgentHubApi.matches(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        context = self._authorized()
        if context is None:
            return
        try:
            payload = self._read_json()
            response = self.server.api.post(parsed.path, payload, context)
        except (json.JSONDecodeError, ApiError) as exc:
            self._send_api_error(exc)
            return
        if response is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(*response)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("http %s", format % args)

    def _rate_limited(self, path: str) -> bool:
        limiter = self.server.rate_limiter
        if limiter is None or not limiter.enabled:
            return False
        ip = self.client_address[0] if self.client_address else "unknown"
        if path in ("/v1/auth/register", "/web/register"):
            return not limiter.allow_register(ip)
        if path in (
            "/v1/auth/login",
            "/v1/auth/agent-login",
            "/v1/auth/change-password",
            "/web/login",
        ):
            return not limiter.allow_auth(ip)
        return False

    def _authorized(self) -> AuthenticatedContext | None:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.api_token}"
        if not self.server.disable_bootstrap and hmac.compare_digest(
            supplied, expected
        ):
            return AuthenticatedContext(role="admin")
        if supplied.startswith("Bearer "):
            raw = supplied[len("Bearer ") :].strip()
            context = self.server.api.authenticate(raw)
            if context is not None:
                return context
            session_context = self.server.api.store.authenticate_session(raw)
            if session_context is not None:
                return session_context
            if self.server.oidc_provider is not None:
                try:
                    context = self.server.oidc_provider.validate_id_token(raw)
                except RuntimeError:
                    context = None
                if context is not None:
                    return context
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return None

    def _read_form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be between 1 byte and 1 MB")
        return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

    def _send_html(self, status: HTTPStatus, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers(html=True)
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        content_type: str = "application/json",
        cache_control: str = "no-store",
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_mcp_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("MCP-Protocol-Version", MCP_PROTOCOL_VERSION)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_mcp_endpoint(self) -> None:
        body = f"event: endpoint\ndata: /mcp\n\n".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self, *, html: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if html:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'",
            )

    def _base_url(self) -> str:
        if self.server.public_url:
            return self.server.public_url
        host = self.headers.get("Host", "").strip()
        if not host or any(character in host for character in "/\\\r\n"):
            bound_host, bound_port = self.server.server_address[:2]
            host = f"{bound_host}:{bound_port}"
        return f"http://{host}"

    def _send_api_error(self, error: Exception) -> None:
        api_error = error if isinstance(error, ApiError) else map_error(error)
        self._send_json(api_error.status, {"error": api_error.message})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = HubSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    store = AgentHubStore(settings.database_url or settings.state_db)
    api = AgentHubApi(
        store,
        build_object_store(settings.object_store_url),
        allow_registration=settings.allow_registration,
    )
    oidc_provider = None
    if settings.oidc_issuer is not None:
        from .oidc import JwksOidcProvider

        oidc_provider = JwksOidcProvider(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience or settings.oidc_issuer,
            store=store,
        )
    server = HubHttpServer(
        (settings.api_host, settings.api_port),
        api,
        settings.api_token,
        settings.public_url,
        settings.web_secret,
        settings.web_cookie_secure,
        settings.disable_bootstrap,
        oidc_provider,
        settings.enable_mcp,
        AuthRateLimiter(
            enabled=settings.rate_limit_enabled,
            auth_per_minute=settings.rate_limit_auth_per_minute,
            register_per_hour=settings.rate_limit_register_per_hour,
        ),
    )
    logger.info(
        "agent_hub_started host=%s port=%s storage=%s",
        settings.api_host,
        settings.api_port,
        "postgresql" if settings.database_url else settings.state_db,
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
