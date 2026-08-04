from __future__ import annotations

import hmac
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import AuthenticatedContext, OIDCIdentityProvider
from .api import AgentHubApi
from .a2a import A2AApi
from .config import HubSettings
from .domain import AuthTokenCreation, TaskSubmission, TenantRegistration
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
    ) -> None:
        super().__init__(address, HubRequestHandler)
        self.api = api
        self.a2a = A2AApi(api.store)
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
        except (LookupError, PermissionError, ValueError) as exc:
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
        except (json.JSONDecodeError, LookupError, PermissionError, ValueError) as exc:
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
            context = self.server.api.store.authenticate_token(raw)
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
        session = self._web_session()
        if session is None:
            self._redirect("/web/login")
            return
        session_id, claims = session
        tenant_id = (
            None
            if claims.get("role") == "admin"
            else claims.get("tenant_id") or "default"
        )
        if path in ("/web", "/web/"):
            self._send_html(
                HTTPStatus.OK,
                dashboard_page(
                    self.server.api.store.stats(tenant_id=tenant_id),
                    self.server.api.store.list_tasks(
                        limit=20, tenant_id=tenant_id
                    ),
                    self.server.api.store.list_runs(limit=10, tenant_id=tenant_id),
                ),
            )
            return
        if path == "/web/login":
            self._send_html(HTTPStatus.OK, login_page())
            return
        if path == "/web/tasks":
            query = parse_qs(query_string)
            status = (query.get("status") or [None])[0]
            self._send_html(
                HTTPStatus.OK,
                tasks_page(
                    self.server.api.store.list_tasks(
                        status=status, limit=200, tenant_id=tenant_id
                    ),
                    status_filter=status,
                    principals=self.server.api.store.list_principals(
                        tenant_id=tenant_id
                    ),
                    actors=self.server.api.store.list_actors(tenant_id=tenant_id),
                    csrf=self.server.web.csrf(session_id),
                ),
            )
            return
        if path == "/web/runs":
            self._send_html(
                HTTPStatus.OK,
                runs_page(
                    self.server.api.store.list_runs(
                        limit=200, tenant_id=tenant_id
                    )
                ),
            )
            return
        if path == "/web/artifacts":
            self._send_html(
                HTTPStatus.OK,
                artifacts_page(
                    self.server.api.store.list_artifacts(
                        limit=200, tenant_id=tenant_id
                    )
                ),
            )
            return
        if path == "/web/nodes":
            self._send_html(
                HTTPStatus.OK,
                nodes_page(
                    self.server.api.store.list_principals(tenant_id=tenant_id),
                    self.server.api.store.list_actors(tenant_id=tenant_id),
                    self.server.api.store.list_nodes(tenant_id=tenant_id),
                ),
            )
            return
        if path == "/web/tenants":
            if claims.get("role") == "admin":
                self._send_html(
                    HTTPStatus.OK,
                    tenants_page(
                        self.server.api.store.list_tenants(),
                        csrf=self.server.web.csrf(session_id),
                    ),
                )
            else:
                self._redirect(f"/web/tenants/{tenant_id or 'default'}")
            return
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[0] == "web" and parts[1] == "tenants":
            requested_tenant = parts[2]
            if claims.get("role") != "admin" and requested_tenant != tenant_id:
                self._send_html(HTTPStatus.FORBIDDEN, not_found_page())
                return
            try:
                tenant = self.server.api.store.get_tenant(requested_tenant)
                tokens = self.server.api.store.list_auth_tokens(
                    tenant_id=requested_tenant
                )
                principals = self.server.api.store.list_principals(
                    tenant_id=requested_tenant
                )
                actors = self.server.api.store.list_actors(
                    tenant_id=requested_tenant
                )
                nodes = self.server.api.store.list_nodes(
                    tenant_id=requested_tenant
                )
            except LookupError:
                self._send_html(HTTPStatus.NOT_FOUND, not_found_page())
                return
            query = parse_qs(query_string)
            created_raw = (query.get("created") or [None])[0]
            self._send_html(
                HTTPStatus.OK,
                tenant_detail_page(
                    tenant,
                    tokens=tokens,
                    principals=principals,
                    actors=actors,
                    nodes=nodes,
                    csrf=self.server.web.csrf(session_id),
                    created_raw_token=created_raw,
                ),
            )
            return
        if len(parts) == 3 and parts[0] == "web" and parts[1] == "tasks":
            task_id = parts[2]
            try:
                task = self.server.api.store.get_task(
                    task_id, tenant_id=tenant_id
                )
                events = self.server.api.store.list_task_events(
                    task_id, limit=500, tenant_id=tenant_id
                )
                runs = [
                    run
                    for run in self.server.api.store.list_runs(
                        limit=500, tenant_id=tenant_id
                    )
                    if run["task_id"] == task_id
                ]
            except LookupError:
                self._send_html(HTTPStatus.NOT_FOUND, not_found_page())
                return
            self._send_html(
                HTTPStatus.OK,
                task_detail_page(
                    task,
                    events=events,
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
                context = self.server.api.store.authenticate_token(supplied)
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
        tenant_id = claims.get("tenant_id") or "default"
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
                submission = TaskSubmission.from_dict(
                    {
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
                )
                task, _ = self.server.api.store.create_task(
                    submission, tenant_id=tenant_id
                )
            except (ValueError, LookupError, json.JSONDecodeError) as exc:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    tasks_page(
                        self.server.api.store.list_tasks(
                            limit=200, tenant_id=tenant_id
                        ),
                        status_filter=None,
                        principals=self.server.api.store.list_principals(
                            tenant_id=tenant_id
                        ),
                        actors=self.server.api.store.list_actors(
                            tenant_id=tenant_id
                        ),
                        csrf=self.server.web.csrf(session_id),
                        error=str(exc),
                    ),
                )
                return
            self._redirect(f"/web/tasks/{task['task_id']}")
            return

        parts = [part for part in path.split("/") if part]
        if parts == ["web", "tenants", "create"]:
            if claims.get("role") != "admin":
                self._send_html(HTTPStatus.FORBIDDEN, not_found_page())
                return
            try:
                tenant = self.server.api.store.create_tenant(
                    TenantRegistration.from_dict(
                        {
                            "tenant_id": (form.get("tenant_id") or [""])[0],
                            "display_name": (form.get("display_name") or [""])[0],
                        }
                    )
                )
            except (ValueError, LookupError) as exc:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    tenants_page(
                        self.server.api.store.list_tenants(),
                        csrf=self.server.web.csrf(session_id),
                        error=str(exc),
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
            if claims.get("role") != "admin":
                if claims.get("role") != "tenant_admin" or requested_tenant != tenant_id:
                    self._send_html(HTTPStatus.FORBIDDEN, not_found_page())
                    return
            try:
                item = AuthTokenCreation.from_dict(
                    {
                        "tenant_id": requested_tenant,
                        "role": (form.get("role") or [""])[0],
                        "principal_id": (form.get("principal_id") or [""])[0] or None,
                        "actor_id": (form.get("actor_id") or [""])[0] or None,
                        "node_id": (form.get("node_id") or [""])[0] or None,
                        "label": (form.get("label") or [""])[0],
                    }
                )
                raw, _ = self.server.api.store.create_auth_token(item)
            except (ValueError, LookupError, PermissionError) as exc:
                tenant = self.server.api.store.get_tenant(requested_tenant)
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    tenant_detail_page(
                        tenant,
                        tokens=self.server.api.store.list_auth_tokens(
                            tenant_id=requested_tenant
                        ),
                        principals=self.server.api.store.list_principals(
                            tenant_id=requested_tenant
                        ),
                        actors=self.server.api.store.list_actors(
                            tenant_id=requested_tenant
                        ),
                        nodes=self.server.api.store.list_nodes(
                            tenant_id=requested_tenant
                        ),
                        csrf=self.server.web.csrf(session_id),
                        error=str(exc),
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
                record = self.server.api.store.revoke_auth_token(
                    parts[2],
                    tenant_id=None
                    if claims.get("role") == "admin"
                    else tenant_id,
                )
            except (LookupError, PermissionError) as exc:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    "<!doctype html><h1>400</h1>"
                    f"<p>{escape(str(exc))}</p>",
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
                self.server.api.store.cancel_task(
                    task_id,
                    actor_id=(form.get("actor_id") or [""])[0],
                    reason=(form.get("reason") or [""])[0] or None,
                    tenant_id=tenant_id,
                )
            except (ValueError, LookupError) as exc:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    "<!doctype html><h1>400</h1>"
                    f"<p>{escape(str(exc))}</p>",
                )
                return
            self._redirect(f"/web/tasks/{task_id}")
            return
        self._send_html(HTTPStatus.NOT_FOUND, not_found_page())

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

    def _base_url(self) -> str:
        if self.server.public_url:
            return self.server.public_url
        host = self.headers.get("Host", "").strip()
        if not host or any(character in host for character in "/\\\r\n"):
            bound_host, bound_port = self.server.server_address[:2]
            host = f"{bound_host}:{bound_port}"
        return f"http://{host}"

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
