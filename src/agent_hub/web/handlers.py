from __future__ import annotations

from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
import hmac
import json
from typing import Any
from urllib.parse import parse_qs, quote

from ..auth import AuthenticatedContext
from ..errors import ApiError, map_error
from .pages import (
    account_page,
    artifacts_page,
    dashboard_page,
    landing_page,
    login_page,
    nodes_page,
    not_found_page,
    register_page,
    runs_page,
    task_detail_page,
    tasks_page,
    tenant_detail_page,
    tenants_page,
)
from .session import SESSION_COOKIE, WebSessionError


class WebHandlersMixin:
    """Server-rendered web UI handlers (login, dashboards, mutations).

    Mixed into the HTTP request handler; everything is routed through the
    AgentHubApi facade instead of reaching into the store directly.
    """

    def _web_get(self, path: str, query_string: str) -> None:
        if path in ("/web", "/web/"):
            session = self._web_session()
            if session is None:
                self._send_html(
                    HTTPStatus.OK,
                    landing_page(
                        registration_open=self.server.api.allow_registration
                    ),
                )
                return
        if path == "/web/login":
            self._send_html(
                HTTPStatus.OK,
                login_page(
                    registration_open=self.server.api.allow_registration
                ),
            )
            return
        if path == "/web/register":
            self._send_html(
                HTTPStatus.OK,
                register_page(
                    error=(
                        "Registration is disabled."
                        if not self.server.api.allow_registration
                        else None
                    )
                ),
            )
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
            user = None
            if context.principal_id:
                try:
                    _, me = self.server.api.get("/v1/auth/me", "", context)
                    user = {
                        "username": (me["me"]["account"] or {}).get("username"),
                        "display_name": me["me"]["principal"].get("display_name"),
                        "role": context.role,
                    }
                except ApiError:
                    user = {"username": None, "display_name": None, "role": context.role}
            self._send_html(
                HTTPStatus.OK,
                dashboard_page(
                    stats,
                    tasks["tasks"],
                    runs["runs"],
                    user=user,
                    admin=is_admin,
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
                    admin=is_admin,
                ),
            )
            return
        if path == "/web/runs":
            _, runs = self.server.api.get("/v1/hub/runs", "limit=200", context)
            self._send_html(
                HTTPStatus.OK,
                runs_page(runs["runs"], admin=is_admin),
            )
            return
        if path == "/web/artifacts":
            _, artifacts = self.server.api.get(
                "/v1/hub/artifacts", "limit=200", context
            )
            self._send_html(
                HTTPStatus.OK,
                artifacts_page(artifacts["artifacts"], admin=is_admin),
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
                    admin=is_admin,
                ),
            )
            return
        if path == "/web/account":
            try:
                _, data = self.server.api.get("/v1/auth/me", "", context)
            except ApiError as exc:
                self._send_html(
                    exc.status,
                    login_page(str(exc.message)),
                )
                return
            query = parse_qs(query_string)
            notice = "Password changed." if query.get("changed") else None
            self._send_html(
                HTTPStatus.OK,
                account_page(
                    data["me"],
                    data["sessions"],
                    data["tokens"],
                    csrf=self.server.web.csrf(session_id),
                    notice=notice,
                    admin=is_admin,
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
                        admin=is_admin,
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
                can_manage_tokens = is_admin or context.role == "tenant_admin"
                if can_manage_tokens:
                    _, tokens = self.server.api.get(
                        f"/v1/hub/tenants/{quote(requested_tenant, safe='')}/tokens",
                        "",
                        context,
                    )
                else:
                    tokens = {"tokens": []}
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
                    admin=is_admin,
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
                    admin=is_admin,
                ),
            )
            return
        self._send_html(HTTPStatus.NOT_FOUND, not_found_page())

    def _web_post(self, path: str) -> None:
        form = self._read_form()
        if path == "/web/login":
            username = (form.get("username") or [""])[0].strip()
            password = (form.get("password") or [""])[0]
            supplied = (form.get("token") or [""])[0]
            if username and password:
                try:
                    _, login = self.server.api.post(
                        "/v1/auth/login",
                        {"username": username, "password": password, "label": "web"},
                        None,
                    )
                except ApiError as exc:
                    self._send_html(
                        exc.status,
                        login_page(
                            str(exc.message),
                            registration_open=self.server.api.allow_registration,
                        ),
                    )
                    return
                user = login["user"]
                _, cookie = self.server.web.create(
                    {
                        "role": user["role"],
                        "tenant_id": user["tenant_id"],
                        "principal_id": user["principal_id"],
                    }
                )
            elif supplied:
                if (
                    not self.server.disable_bootstrap
                    and hmac.compare_digest(supplied, self.server.api_token)
                ):
                    _, cookie = self.server.web.create({"role": "admin"})
                else:
                    context = self.server.api.authenticate(supplied)
                    if context is None and self.server.oidc_provider is not None:
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
                                "Invalid token. Use the bootstrap token or a tenant token.",
                                registration_open=self.server.api.allow_registration,
                            ),
                        )
                        return
                    _, cookie = self.server.web.create(context.to_dict())
            else:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    login_page(
                        "Enter your username and password (or an admin token).",
                        registration_open=self.server.api.allow_registration,
                    ),
                )
                return
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
        if path == "/web/register":
            if not self.server.api.allow_registration:
                self._send_html(
                    HTTPStatus.FORBIDDEN,
                    register_page("Registration is disabled."),
                )
                return
            password = (form.get("password") or [""])[0]
            password2 = (form.get("password2") or [""])[0]
            if password != password2:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    register_page(
                        "Passwords do not match.",
                        username=(form.get("username") or [""])[0],
                        display_name=(form.get("display_name") or [""])[0],
                    ),
                )
                return
            try:
                self.server.api.post(
                    "/v1/auth/register",
                    {
                        "username": (form.get("username") or [""])[0],
                        "password": password,
                        "display_name": (form.get("display_name") or [""])[0],
                    },
                    None,
                )
            except ApiError as exc:
                self._send_html(
                    exc.status,
                    register_page(
                        str(exc.message),
                        username=(form.get("username") or [""])[0],
                        display_name=(form.get("display_name") or [""])[0],
                    ),
                )
                return
            self._redirect("/web/login")
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

        if path == "/web/account/change-password":
            try:
                self.server.api.post(
                    "/v1/auth/change-password",
                    {
                        "old_password": (form.get("old_password") or [""])[0],
                        "new_password": (form.get("new_password") or [""])[0],
                    },
                    context,
                )
            except ApiError as exc:
                _, data = self.server.api.get("/v1/auth/me", "", context)
                self._send_html(
                    exc.status,
                    account_page(
                        data["me"],
                        data["sessions"],
                        data["tokens"],
                        csrf=self.server.web.csrf(session_id),
                        error=exc.message,
                        admin=is_admin,
                    ),
                )
                return
            self._redirect("/web/account?changed=1")
            return
        if path == "/web/account/sessions/revoke":
            try:
                self.server.api.post(
                    "/v1/auth/sessions/revoke",
                    {
                        "session_token_id": (
                            form.get("session_token_id") or [""]
                        )[0]
                    },
                    context,
                )
            except ApiError as exc:
                _, data = self.server.api.get("/v1/auth/me", "", context)
                self._send_html(
                    exc.status,
                    account_page(
                        data["me"],
                        data["sessions"],
                        data["tokens"],
                        csrf=self.server.web.csrf(session_id),
                        error=exc.message,
                        admin=is_admin,
                    ),
                )
                return
            self._redirect("/web/account")
            return
        if path == "/web/account/tokens/revoke":
            try:
                self.server.api.post(
                    "/v1/auth/tokens/revoke",
                    {"token_id": (form.get("token_id") or [""])[0]},
                    context,
                )
            except ApiError as exc:
                _, data = self.server.api.get("/v1/auth/me", "", context)
                self._send_html(
                    exc.status,
                    account_page(
                        data["me"],
                        data["sessions"],
                        data["tokens"],
                        csrf=self.server.web.csrf(session_id),
                        error=exc.message,
                        admin=is_admin,
                    ),
                )
                return
            self._redirect("/web/account")
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
                        admin=is_admin,
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
                        admin=is_admin,
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
                        admin=is_admin,
                    ),
                )
                return
            data = self._web_tenant_detail_data(
                context, requested_tenant, is_admin
            )
            self._send_html(
                HTTPStatus.OK,
                tenant_detail_page(
                    data["tenant"],
                    tokens=data["tokens"],
                    principals=data["principals"],
                    actors=data["actors"],
                    nodes=data["nodes"],
                    csrf=self.server.web.csrf(session_id),
                    created_raw_token=raw,
                    admin=is_admin,
                ),
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
        if is_admin or context.role == "tenant_admin":
            _, tokens = self.server.api.get(
                f"/v1/hub/tenants/{quote(requested_tenant, safe='')}/tokens",
                "",
                context,
            )
        else:
            tokens = {"tokens": []}
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
