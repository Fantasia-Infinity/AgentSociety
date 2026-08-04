from __future__ import annotations

import hmac
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .auth import AuthenticatedContext, OIDCIdentityProvider
from .api import AgentHubApi
from .a2a import A2AApi
from .config import HubSettings
from .errors import ApiError, map_error
from .mcp import MCP_PROTOCOL_VERSION, McpService
from .store import AgentHubStore
from .object_store import build_object_store
from .web import (
    SESSION_COOKIE,
    WebSession,
    WebSessionError,
    artifacts_page,
    dashboard_page,
    login_page,
    nodes_page,
    not_found_page,
    runs_page,
    tenant_detail_page,
    tenants_page,
    task_detail_page,
    tasks_page,
)


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
        oidc_provider: OIDCIdentityProvider | None = None,
        enable_mcp: bool = True,
    ) -> None:
        super().__init__(address, HubRequestHandler)
        self.api = api
        self.a2a = A2AApi(api)
        self.mcp = McpService(api) if enable_mcp else None
        self.api_token = api_token
        self.public_url = public_url.rstrip("/") if public_url else None
        self.web = WebSession(web_secret) if web_secret is not None else None
        self.web_cookie_secure = web_cookie_secure
        self.oidc_provider = oidc_provider


class HubRequestHandler(BaseHTTPRequestHandler):
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

    def _authorized(self) -> AuthenticatedContext | None:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.api_token}"
        if hmac.compare_digest(supplied, expected):
            return AuthenticatedContext(role="admin")
        if supplied.startswith("Bearer "):
            raw = supplied[len("Bearer ") :].strip()
            context = self.server.api.authenticate(raw)
            if context is not None:
                return context
            if self.server.oidc_provider is not None:
                try:
                    context = self.server.oidc_provider.validate_id_token(raw)
                except RuntimeError:
                    context = None
                if context is not None:
                    return context
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return None

    def _web_get(self, path: str, query_string: str) -> None:
        if path == "/web/login":
            self._send_html(HTTPStatus.OK, login_page())
            return
        session = self._web_session()
        if session is None:
            self._redirect("/web/login")
            return
        session_id, claims = session
        context = AuthenticatedContext.from_dict(claims)
        is_admin = context.is_admin
        if path in ("/web", "/web/"):
            _, base = self.server.api.get("/v1/hub", "", context)
            stats = dict(base)
            stats.pop("status", None)
            _, tasks = self.server.api.get(
                "/v1/hub/tasks", "limit=20", context
            )
            _, runs = self.server.api.get("/v1/hub/runs", "limit=10", context)
            self._send_html(
                HTTPStatus.OK,
                dashboard_page(
                    stats,
                    tasks["tasks"],
                    runs["runs"],
                ),
            )
            return
        if path == "/web/tasks":
            query = parse_qs(query_string)
            status = (query.get("status") or [None])[0]
            params = "limit=200"
            if status:
                params += f"&status={quote(status)}"
            _, tasks = self.server.api.get("/v1/hub/tasks", params, context)
            _, principals = self.server.api.get(
                "/v1/hub/principals", "", context
            )
            _, actors = self.server.api.get("/v1/hub/actors", "", context)
            self._send_html(
                HTTPStatus.OK,
                tasks_page(
                    tasks["tasks"],
                    status_filter=status,
                    principals=principals["principals"],
                    actors=actors["actors"],
                    csrf=self.server.web.csrf(session_id),
                ),
            )
            return
        if path == "/web/runs":
            _, runs = self.server.api.get("/v1/hub/runs", "limit=200", context)
            self._send_html(
                HTTPStatus.OK,
                runs_page(runs["runs"]),
            )
            return
        if path == "/web/artifacts":
            _, artifacts = self.server.api.get(
                "/v1/hub/artifacts", "limit=200", context
            )
            self._send_html(
                HTTPStatus.OK,
                artifacts_page(artifacts["artifacts"]),
            )
            return
        if path == "/web/nodes":
            _, principals = self.server.api.get(
                "/v1/hub/principals", "", context
            )
            _, actors = self.server.api.get("/v1/hub/actors", "", context)
            _, nodes = self.server.api.get("/v1/hub/nodes", "", context)
            self._send_html(
                HTTPStatus.OK,
                nodes_page(
                    principals["principals"],
                    actors["actors"],
                    nodes["nodes"],
                ),
            )
            return
        if path == "/web/tenants":
            if is_admin:
                _, tenants = self.server.api.get(
                    "/v1/hub/tenants", "", context
                )
                self._send_html(
                    HTTPStatus.OK,
                    tenants_page(
                        tenants["tenants"],
                        csrf=self.server.web.csrf(session_id),
                    ),
                )
            else:
                self._redirect(
                    f"/web/tenants/{context.tenant_id or 'default'}"
                )
            return
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[0] == "web" and parts[1] == "tenants":
            requested_tenant = parts[2]
            if (
                not is_admin
                and requested_tenant != (context.tenant_id or "default")
            ):
                self._send_html(HTTPStatus.FORBIDDEN, not_found_page())
                return
            try:
                _, tenant = self.server.api.get(
                    f"/v1/hub/tenants/{quote(requested_tenant, safe='')}",
                    "",
                    context,
                )
                _, tokens = self.server.api.get(
                    f"/v1/hub/tenants/{quote(requested_tenant, safe='')}/tokens",
                    "",
                    context,
                )
                tenant_scope = (
                    f"tenant_id={quote(requested_tenant)}" if is_admin else ""
                )
                _, principals = self.server.api.get(
                    "/v1/hub/principals", tenant_scope, context
                )
                _, actors = self.server.api.get(
                    "/v1/hub/actors", tenant_scope, context
                )
                _, nodes = self.server.api.get(
                    "/v1/hub/nodes", tenant_scope, context
                )
            except ApiError as exc:
                self._send_html(exc.status, not_found_page())
                return
            query = parse_qs(query_string)
            created_raw = (query.get("created") or [None])[0]
            self._send_html(
                HTTPStatus.OK,
                tenant_detail_page(
                    tenant["tenant"],
                    tokens=tokens["tokens"],
                    principals=principals["principals"],
                    actors=actors["actors"],
                    nodes=nodes["nodes"],
                    csrf=self.server.web.csrf(session_id),
                    created_raw_token=created_raw,
                ),
            )
            return
        if len(parts) == 3 and parts[0] == "web" and parts[1] == "tasks":
            task_id = parts[2]
            try:
                _, task = self.server.api.get(
                    f"/v1/hub/tasks/{quote(task_id, safe='')}", "", context
                )
                _, events = self.server.api.get(
                    f"/v1/hub/tasks/{quote(task_id, safe='')}/events",
                    "limit=500",
                    context,
                )
                _, runs = self.server.api.get(
                    "/v1/hub/runs", "limit=500", context
                )
                runs = [
                    run
                    for run in runs["runs"]
                    if run["task_id"] == task_id
                ]
            except ApiError as exc:
                self._send_html(exc.status, not_found_page())
                return
            self._send_html(
                HTTPStatus.OK,
                task_detail_page(
                    task["task"],
                    events=events["events"],
                    runs=runs,
                    csrf=self.server.web.csrf(session_id),
                ),
            )
            return
        self._send_html(HTTPStatus.NOT_FOUND, not_found_page())

    def _web_post(self, path: str) -> None:
        form = self._read_form()
        if path == "/web/login":
            supplied = (form.get("token") or [""])[0]
            if hmac.compare_digest(supplied, self.server.api_token):
                _, cookie = self.server.web.create({"role": "admin"})
            else:
                context = self.server.api.authenticate(supplied)
                if (
                    context is None
                    and self.server.oidc_provider is not None
                ):
                    try:
                        context = self.server.oidc_provider.validate_id_token(
                            supplied
                        )
                    except RuntimeError:
                        context = None
                if context is None or context.role == "node":
                    self._send_html(
                        HTTPStatus.UNAUTHORIZED,
                        login_page(
                            "Invalid token. Use the bootstrap token or a tenant token."
                        ),
                    )
                    return
                _, cookie = self.server.web.create(context.to_dict())
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header(
                "Set-Cookie",
                self.server.web.set_cookie(
                    cookie, secure=self.server.web_cookie_secure
                ),
            )
            self.send_header("Location", "/web")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        session = self._web_session()
        if session is None:
            self._redirect("/web/login")
            return
        session_id, claims = session
        context = AuthenticatedContext.from_dict(claims)
        is_admin = context.is_admin
        tenant_id = context.tenant_id or "default"
        csrf = (form.get("csrf_token") or [""])[0]
        if not hmac.compare_digest(csrf, self.server.web.csrf(session_id)):
            self._send_html(
                HTTPStatus.FORBIDDEN,
                "<!doctype html><h1>403</h1><p>Invalid or missing CSRF token.</p>",
            )
            return

        if path == "/web/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header(
                "Set-Cookie",
                self.server.web.clear_cookie(secure=self.server.web_cookie_secure),
            )
            self.send_header("Location", "/web/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/web/tasks/create":
            try:
                input_json = (form.get("input_json") or [""])[0].strip()
                parsed_input = json.loads(input_json) if input_json else {}
                if not isinstance(parsed_input, dict):
                    raise ValueError("input_json must be a JSON object")
                capabilities = [
                    item.strip()
                    for item in (form.get("required_capabilities") or [""])[0].split(",")
                    if item.strip()
                ]
                payload = {
                    "principal_id": (form.get("principal_id") or [""])[0],
                    "delegator_actor_id": (
                        form.get("delegator_actor_id") or [""]
                    )[0],
                    "objective": (form.get("objective") or [""])[0],
                    "assignee_actor_id": (
                        form.get("assignee_actor_id") or [""]
                    )[0]
                    or None,
                    "required_capabilities": capabilities,
                    "input": parsed_input,
                    "idempotency_key": (form.get("idempotency_key") or [""])[0]
                    or None,
                    "origin": "web_ui",
                }
                _, created = self.server.api.post(
                    "/v1/hub/tasks", payload, context
                )
                task = created["task"]
            except (ApiError, ValueError, json.JSONDecodeError) as exc:
                error = exc if isinstance(exc, ApiError) else map_error(exc)
                _, tasks = self.server.api.get(
                    "/v1/hub/tasks", "limit=200", context
                )
                _, principals = self.server.api.get(
                    "/v1/hub/principals", "", context
                )
                _, actors = self.server.api.get("/v1/hub/actors", "", context)
                self._send_html(
                    error.status,
                    tasks_page(
                        tasks["tasks"],
                        status_filter=None,
                        principals=principals["principals"],
                        actors=actors["actors"],
                        csrf=self.server.web.csrf(session_id),
                        error=error.message,
                    ),
                )
                return
            self._redirect(f"/web/tasks/{task['task_id']}")
            return

        parts = [part for part in path.split("/") if part]
        if parts == ["web", "tenants", "create"]:
            if not is_admin:
                self._send_html(HTTPStatus.FORBIDDEN, not_found_page())
                return
            try:
                _, created = self.server.api.post(
                    "/v1/hub/tenants",
                    {
                        "tenant_id": (form.get("tenant_id") or [""])[0],
                        "display_name": (form.get("display_name") or [""])[0],
                    },
                    context,
                )
                tenant = created["tenant"]
            except ApiError as exc:
                _, tenants = self.server.api.get(
                    "/v1/hub/tenants", "", context
                )
                self._send_html(
                    exc.status,
                    tenants_page(
                        tenants["tenants"],
                        csrf=self.server.web.csrf(session_id),
                        error=exc.message,
                    ),
                )
                return
            self._redirect(f"/web/tenants/{tenant['tenant_id']}")
            return
        if (
            len(parts) == 5
            and parts[0] == "web"
            and parts[1] == "tenants"
            and parts[3] == "tokens"
            and parts[4] == "create"
        ):
            requested_tenant = parts[2]
            if not is_admin:
                if context.role != "tenant_admin" or requested_tenant != tenant_id:
                    self._send_html(HTTPStatus.FORBIDDEN, not_found_page())
                    return
            try:
                _, created = self.server.api.post(
                    f"/v1/hub/tenants/{quote(requested_tenant, safe='')}/tokens",
                    {
                        "role": (form.get("role") or [""])[0],
                        "principal_id": (form.get("principal_id") or [""])[0] or None,
                        "actor_id": (form.get("actor_id") or [""])[0] or None,
                        "node_id": (form.get("node_id") or [""])[0] or None,
                        "label": (form.get("label") or [""])[0],
                    },
                    context,
                )
                raw = created["raw_token"]
            except ApiError as exc:
                data = self._web_tenant_detail_data(
                    context, requested_tenant, is_admin
                )
                self._send_html(
                    exc.status,
                    tenant_detail_page(
                        data["tenant"],
                        tokens=data["tokens"],
                        principals=data["principals"],
                        actors=data["actors"],
                        nodes=data["nodes"],
                        csrf=self.server.web.csrf(session_id),
                        error=exc.message,
                    ),
                )
                return
            self._redirect(
                f"/web/tenants/{requested_tenant}?created={raw}"
            )
            return
        if (
            len(parts) == 4
            and parts[0] == "web"
            and parts[1] == "tokens"
            and parts[3] == "revoke"
        ):
            try:
                _, revoked = self.server.api.post(
                    f"/v1/hub/tokens/{quote(parts[2], safe='')}/revoke",
                    {},
                    context,
                )
                record = revoked["token"]
            except ApiError as exc:
                self._send_html(
                    exc.status,
                    "<!doctype html><h1>{}</h1>"
                    "<p>{}</p>".format(exc.status.value, escape(exc.message)),
                )
                return
            self._redirect(f"/web/tenants/{record['tenant_id']}")
            return
        if (
            len(parts) == 4
            and parts[0] == "web"
            and parts[1] == "tasks"
            and parts[3] == "cancel"
        ):
            task_id = parts[2]
            try:
                self.server.api.post(
                    f"/v1/hub/tasks/{quote(task_id, safe='')}/cancel",
                    {
                        "actor_id": (form.get("actor_id") or [""])[0],
                        "reason": (form.get("reason") or [""])[0] or None,
                    },
                    context,
                )
            except ApiError as exc:
                self._send_html(
                    exc.status,
                    "<!doctype html><h1>{}</h1>"
                    "<p>{}</p>".format(exc.status.value, escape(exc.message)),
                )
                return
            self._redirect(f"/web/tasks/{task_id}")
            return
        self._send_html(HTTPStatus.NOT_FOUND, not_found_page())

    def _web_tenant_detail_data(
        self,
        context: AuthenticatedContext,
        requested_tenant: str,
        is_admin: bool,
    ) -> dict[str, Any]:
        _, tenant = self.server.api.get(
            f"/v1/hub/tenants/{quote(requested_tenant, safe='')}",
            "",
            context,
        )
        _, tokens = self.server.api.get(
            f"/v1/hub/tenants/{quote(requested_tenant, safe='')}/tokens",
            "",
            context,
        )
        scope = f"tenant_id={quote(requested_tenant)}" if is_admin else ""
        _, principals = self.server.api.get(
            "/v1/hub/principals", scope, context
        )
        _, actors = self.server.api.get("/v1/hub/actors", scope, context)
        _, nodes = self.server.api.get("/v1/hub/nodes", scope, context)
        return {
            "tenant": tenant["tenant"],
            "tokens": tokens["tokens"],
            "principals": principals["principals"],
            "actors": actors["actors"],
            "nodes": nodes["nodes"],
        }

    def _web_session(self) -> tuple[str, dict[str, Any]] | None:
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        if morsel is None:
            return None
        try:
            return self.server.web.verify(morsel.value)
        except WebSessionError:
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
        self.end_headers()
        self.wfile.write(body)

    def _send_mcp_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("MCP-Protocol-Version", MCP_PROTOCOL_VERSION)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_mcp_endpoint(self) -> None:
        body = f"event: endpoint\ndata: /mcp\n\n".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    api = AgentHubApi(store, build_object_store(settings.object_store_url))
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
        oidc_provider,
        settings.enable_mcp,
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
