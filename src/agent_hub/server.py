from __future__ import annotations

import base64
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import queue
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .auth import AuthenticatedContext, OIDCIdentityProvider
from .api import AgentHubApi
from .a2a import A2AApi
from .config import HubSettings
from .errors import ApiError, map_error
from .mcp import MCP_PROTOCOL_VERSION, McpService
from .ratelimit import AuthRateLimiter
from .store import AgentHubStore
from .object_store import build_object_store
from .tunnel import TunnelRegistry
from .web import WebSession
from .websocket import WebSocket, WebSocketProtocolError, accept_key
from .web.handlers import WebHandlersMixin
from .web_proxy import (
    FORWARDED_REQUEST_HEADERS,
    FORWARDED_RESPONSE_HEADERS,
    MAX_PROXY_REQUEST_BODY,
    MAX_WS_FRAME,
    TUNNEL_KEEPALIVE_SECONDS,
    WS_OPEN_TIMEOUT_SECONDS,
    WebTunnelCoordinator,
    decode_proxy_body,
    validate_proxy_path,
    validate_ws_path,
)


_DEVICE_WEB_HTML_PATH_RE = re.compile(
    rb'(?P<prefix>(?:src|href)=["\'])(?P<path>/(?:assets/|plugins/|api/|manifest\.webmanifest|favicon\.svg))'
)


def _device_web_mount(node_id: str) -> str:
    return f"/v1/web/{quote(node_id, safe='')}"


_DEVICE_WEB_QUOTED_PATH_RE = re.compile(
    rb'(?P<quote>["\'])(?P<path>/(?:assets/|plugins/|api/)[^"\']*|/manifest\.webmanifest(?:\?[^"\']*)?|/favicon\.svg(?:\?[^"\']*)?)(?P=quote)'
)


def _rewrite_device_web_url_script(mount: str) -> bytes:
    mount_json = json.dumps(mount + "/", ensure_ascii=True).replace("<", "\\u003c")
    return f"""<script>
(() => {{
  const mount = {mount_json};
  const prefixes = ['/api', '/plugins', '/assets'];
  const isSurfacePath = (path) => path === '/manifest.webmanifest' || path === '/favicon.svg'
    || prefixes.some((prefix) => path === prefix || path.startsWith(prefix + '/'));
  const rewrite = (input, websocket = false) => {{
    const value = input instanceof URL ? input : new URL(String(input), location.href);
    if (value.origin !== location.origin) return value;
    if (websocket && (value.pathname === '/api/events.mux' || value.pathname === '/api/events.host')) {{
      const kind = value.pathname.endsWith('.mux') ? 'mux' : 'host';
      value.pathname = mount + 'ws/events/' + kind;
      return value;
    }}
    if (!value.pathname.startsWith(mount) && isSurfacePath(value.pathname)) {{
      value.pathname = mount + value.pathname.slice(1);
    }}
    return value;
  }};
  const nativeFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = (input, init) => nativeFetch(
    input instanceof Request ? new Request(rewrite(input.url), input) : rewrite(input), init);
  const NativeWebSocket = globalThis.WebSocket;
  if (NativeWebSocket) {{
    class HubWebSocket extends NativeWebSocket {{
      constructor(url, protocols) {{ super(rewrite(url, true), protocols); }}
    }}
    for (const key of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) HubWebSocket[key] = NativeWebSocket[key];
    globalThis.WebSocket = HubWebSocket;
  }}
  const NativeEventSource = globalThis.EventSource;
  if (NativeEventSource) {{
    class HubEventSource extends NativeEventSource {{
      constructor(url, init) {{ super(rewrite(url), init); }}
    }}
    for (const key of ['CONNECTING', 'OPEN', 'CLOSED']) HubEventSource[key] = NativeEventSource[key];
    globalThis.EventSource = HubEventSource;
  }}
  globalThis.__DSH_HUB_WEB_MOUNT__ = mount;
}})();
</script>""".encode("utf-8")


def rewrite_device_web_html(body: bytes, node_id: str) -> bytes:
    """Make the origin-rooted DSH Web frontend work below a Hub node mount."""
    mount = _device_web_mount(node_id).encode("ascii") + b"/"

    def replace(match: re.Match[bytes]) -> bytes:
        path = match.group("path")
        return match.group("quote") + mount + path.lstrip(b"/") + match.group("quote")

    rewritten = _DEVICE_WEB_QUOTED_PATH_RE.sub(replace, body)
    marker = b"</head>"
    if marker in rewritten and b"__DSH_HUB_WEB_MOUNT__" not in rewritten:
        rewritten = rewritten.replace(
            marker,
            _rewrite_device_web_url_script(_device_web_mount(node_id)) + marker,
            1,
        )
    return rewritten


logger = logging.getLogger(__name__)


def forwarded_client_ip(
    headers, client_address: tuple[str, int] | None
) -> str:
    """Return the real client IP seen through the trusted loopback proxy.

    Caddy overwrites X-Forwarded-For with the direct client address before
    proxying to the loopback-bound Hub, so the first hop is trustworthy here.
    """

    forwarded = headers.get("X-Forwarded-For", "")
    first = forwarded.split(",")[0].strip() if forwarded else ""
    if first:
        return first
    return client_address[0] if client_address else "unknown"


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
        # Outbound DSH Web tunnels: devices connect out and the Hub routes
        # browser requests back to them over the live WebSocket.
        self.tunnels = TunnelRegistry()
        self.web_tunnel = WebTunnelCoordinator(self.tunnels)
        api.tunnel_registry = self.tunnels
        # SSE subscribers for /v1/hub/events: one entry per connected worker
        # node. Entries are {node_id, tenant_id, queue}; publish() fans a
        # worker-relevant event out to the matching node's stream.
        self._subscribers: list[dict[str, Any]] = []
        self._subscribers_lock = threading.Lock()
        self.api.on_event = self.publish
        self.api.on_shared_event = self.publish_tenant

    def publish_tenant(
        self, tenant_id: str, event_name: str, data: dict[str, Any]
    ) -> None:
        """Fan one tenant-wide event (shared memory / directory) out to every
        subscriber of that tenant."""
        with self._subscribers_lock:
            for subscriber in self._subscribers:
                if subscriber["tenant_id"] != tenant_id:
                    continue
                stream_queue = subscriber["queue"]
                try:
                    stream_queue.put_nowait((event_name, data))
                except queue.Full:
                    try:
                        stream_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        stream_queue.put_nowait((event_name, data))
                    except queue.Full:
                        pass

    def publish(self, node_id: str, event_name: str, data: dict[str, Any]) -> None:
        """Fan one worker-relevant event out to the matching node's SSE stream."""
        if not node_id:
            return
        with self._subscribers_lock:
            for subscriber in self._subscribers:
                if subscriber["node_id"] != node_id:
                    continue
                queue = subscriber["queue"]
                try:
                    queue.put_nowait((event_name, data))
                except queue.Full:
                    # Drop the oldest event for this subscriber: a stuck
                    # worker must not pin memory; the worker's polling
                    # fallback still recovers the state.
                    try:
                        queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        queue.put_nowait((event_name, data))
                    except queue.Full:
                        pass

    def subscribe(
        self, node_id: str, tenant_id: str
    ) -> "queue.Queue[tuple[str, dict[str, Any]]]":
        stream_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=100)
        with self._subscribers_lock:
            self._subscribers.append(
                {"node_id": node_id, "tenant_id": tenant_id, "queue": stream_queue}
            )
        return stream_queue

    def unsubscribe(self, stream_queue: "queue.Queue[tuple[str, dict[str, Any]]]") -> None:
        with self._subscribers_lock:
            self._subscribers = [
                subscriber
                for subscriber in self._subscribers
                if subscriber["queue"] is not stream_queue
            ]


class HubRequestHandler(WebHandlersMixin, BaseHTTPRequestHandler):
    server: HubHttpServer
    # Bound per-I/O waits so slowloris-style connections cannot pin threads
    # indefinitely. The MCP SSE keep-alive writes every 15s, well within this.
    timeout = 60

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if self.server.web is not None and parsed.path.startswith("/web"):
            self._web_get(parsed.path, parsed.query)
            return
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/v1/web/tunnel/ws":
            self._tunnel_device_ws(parse_qs(parsed.query))
            return
        segments = [part for part in parsed.path.split("/") if part]
        if (
            len(segments) >= 6
            and segments[0] == "v1"
            and segments[1] == "web"
            and segments[3] == "ws"
            and segments[4] == "events"
            and segments[5] in ("mux", "host")
        ):
            self._browser_event_ws(segments[2], segments[5])
            return
        if parsed.path.startswith("/v1/web/"):
            self._web_proxy("GET")
            return
        if parsed.path == "/v1/hub/events":
            self._sse_events_stream(parse_qs(parsed.query))
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
        if parsed.path.startswith("/v1/web/"):
            self._web_proxy("POST")
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
                # MCP Streamable HTTP clients (mcp-remote and friends) require
                # the initialize response to carry an Mcp-Session-Id header.
                # The Hub is stateless over this session id, but issuing one
                # keeps protocol-compliant clients happy.
                session_id = (
                    secrets.token_hex(16)
                    if isinstance(payload, dict)
                    and payload.get("method") == "initialize"
                    else None
                )
                self._send_mcp_json(response, session_id=session_id)
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

    def do_HEAD(self) -> None:
        self._web_proxy("HEAD")

    def _tunnel_device_ws(self, query: dict[str, list[str]]) -> None:
        """Device outbound tunnel endpoint: ticket-authenticated WS upgrade."""
        ticket = (query.get("ticket") or [""])[0].strip()
        node_id = self.server.tunnels.consume_ticket(ticket)
        if node_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid or expired tunnel ticket"}
            )
            return
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if not key:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "missing Sec-WebSocket-Key"}
            )
            return
        self.protocol_version = "HTTP/1.1"
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        self.close_connection = True
        ws = WebSocket(self.rfile, self.wfile)
        keepalive_stop = threading.Event()

        def keepalive() -> None:
            while not keepalive_stop.wait(TUNNEL_KEEPALIVE_SECONDS):
                try:
                    ws.ping()
                except (OSError, WebSocketProtocolError):
                    return

        keepalive_thread = threading.Thread(
            target=keepalive,
            name=f"dsh-web-tunnel-keepalive-{node_id}",
            daemon=True,
        )
        keepalive_thread.start()
        try:
            self.server.web_tunnel.handle_device_ws(ws, node_id)
        finally:
            keepalive_stop.set()

    def _authorize_browser(self, node_id: str) -> AuthenticatedContext | None:
        """Browser auth for proxied DSH Web surfaces.

        Accepts the same bearer tokens as the Hub API, or the Hub web UI
        session cookie when the Hub web app is enabled. Either way the caller
        must be allowed to reach the node's dsh_web capability (admin, same
        tenant, or same principal owner). A None return means a response was
        already written.
        """
        context: AuthenticatedContext | None = None
        if self.headers.get("Authorization", "").strip():
            context = self._authorized()
            if context is None:
                return None
        elif self.server.web is not None:
            session = self._web_session()
            if session is not None:
                _, claims = session
                context = AuthenticatedContext.from_dict(claims)
        if context is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        if not self._node_web_allowed(context, node_id):
            self._send_json(
                HTTPStatus.FORBIDDEN, {"error": "node web access denied"}
            )
            return None
        return context

    def _node_web_allowed(
        self, context: AuthenticatedContext, node_id: str
    ) -> bool:
        """The node exists, advertises dsh_web, and the caller may reach it."""
        if not context.is_admin:
            tenant_id = context.tenant_id or "default"
            principal_id = (
                None if context.role == "tenant_admin" else context.principal_id
            )
        else:
            tenant_id = None
            principal_id = None
        try:
            nodes = self.server.api.store.list_nodes(
                tenant_id=tenant_id, principal_id=principal_id
            )
        except Exception:  # noqa: BLE001 - store failure must not leak
            logger.exception("node_web_lookup_failed node=%s", node_id)
            return False
        node = next(
            (item for item in nodes if item.get("node_id") == node_id), None
        )
        if node is None:
            return False
        raw = (node.get("metadata") or {}).get("dsh_web")
        return isinstance(raw, dict) and raw.get("enabled") is True

    def _browser_event_ws(self, node_id: str, kind: str) -> None:
        """Browser DSH event downlink: /v1/web/{node}/ws/events/{mux|host}.

        Upgrades the browser socket only after the device confirms its local
        event stream opened, then pumps device frames to the browser
        (downlink-only: browser frames close 1008, matching dsh semantics).
        """
        context = self._authorize_browser(node_id)
        if context is None:
            return
        path = f"/api/events.{kind}"
        if not validate_ws_path(path):
            self._send_json(
                HTTPStatus.FORBIDDEN, {"error": "event stream not allowed"}
            )
            return
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if not key:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "missing Sec-WebSocket-Key"}
            )
            return
        opened = self.server.web_tunnel.open_event_stream(node_id, path)
        if opened is None:
            self._send_json(
                HTTPStatus.BAD_GATEWAY, {"error": "device tunnel unavailable"}
            )
            return
        stream_id, channel = opened
        try:
            ack = channel.get(timeout=WS_OPEN_TIMEOUT_SECONDS)
        except queue.Empty:
            self.server.web_tunnel.close_event_stream(node_id, stream_id)
            self._send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {"error": "device did not open the event stream"},
            )
            return
        if ack.get("type") != "ws-open-ack" or ack.get("ok") is not True:
            self.server.web_tunnel.close_event_stream(node_id, stream_id)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": ack.get("error") or "device rejected the event stream"},
            )
            return
        self.protocol_version = "HTTP/1.1"
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        self.close_connection = True
        browser_ws = WebSocket(self.rfile, self.wfile)
        self._pump_event_stream(browser_ws, channel, node_id, stream_id)

    def _pump_event_stream(
        self,
        browser_ws: WebSocket,
        channel: "queue.Queue[dict[str, Any]]",
        node_id: str,
        stream_id: str,
    ) -> None:
        """Relay device frames to the browser until either side closes."""
        def pump() -> None:
            last_ping = time.monotonic()
            try:
                while True:
                    try:
                        message = channel.get(
                            timeout=min(5.0, max(0.05, TUNNEL_KEEPALIVE_SECONDS))
                        )
                    except queue.Empty:
                        if time.monotonic() - last_ping >= TUNNEL_KEEPALIVE_SECONDS:
                            try:
                                browser_ws.ping()
                            except (OSError, WebSocketProtocolError):
                                return
                            last_ping = time.monotonic()
                        continue
                    mtype = message.get("type")
                    if mtype == "ws-frame":
                        try:
                            payload = base64.b64decode(
                                message.get("payload_b64") or "", validate=True
                            )
                            if len(payload) > MAX_WS_FRAME:
                                continue
                            if int(message.get("opcode", 1)) == 0x2:
                                browser_ws.send_bytes(payload)
                            else:
                                browser_ws.send_text(
                                    payload.decode("utf-8", errors="replace")
                                )
                        except (ValueError, WebSocketProtocolError, OSError):
                            return
                    elif mtype in ("ws-close", "ws-open-ack"):
                        return
            finally:
                try:
                    browser_ws.close()
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            while True:
                opcode, _payload = browser_ws.recv_message()
                if opcode == 0x8:
                    return
                # DSH event streams are server-to-browser only.
                browser_ws.close(1008, b"downlink only")
                return
        except (WebSocketProtocolError, OSError, ValueError):
            return
        finally:
            self.server.web_tunnel.close_event_stream(node_id, stream_id)

    def _web_proxy(self, method: str) -> None:
        """Browser-facing DSH Web proxy: /v1/web/{node_id}/<path>."""
        parsed = urlparse(self.path)
        rest = parsed.path
        # /v1/web/<node_id>[/<rest>]
        segments = [part for part in rest.split("/") if part]
        if len(segments) < 3 or segments[0] != "v1" or segments[1] != "web":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        node_id = segments[2]
        path = "/" + "/".join(segments[3:]) if len(segments) > 3 else "/"
        if parsed.query:
            path += "?" + parsed.query
        # Authenticate before revealing whether a path belongs to the proxy
        # allowlist. This keeps unauthenticated callers from probing the
        # device surface's path policy.
        context = self._authorize_browser(node_id)
        if context is None:
            return
        if not validate_proxy_path(path):
            self._send_json(
                HTTPStatus.FORBIDDEN, {"error": "path not allowed on the tunnel"}
            )
            return
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in FORWARDED_REQUEST_HEADERS
        }
        body: bytes | None = None
        if method in ("POST", "PUT", "PATCH"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"}
                )
                return
            if length > MAX_PROXY_REQUEST_BODY:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "request body too large"},
                )
                return
            if length:
                body = self.rfile.read(length)
        response = self.server.web_tunnel.proxy_request(
            node_id, method, path, headers, body
        )
        if response is None:
            self._send_json(
                HTTPStatus.BAD_GATEWAY, {"error": "device tunnel unavailable"}
            )
            return
        try:
            status = int(response.get("status", 502))
            response_headers = response.get("headers") or {}
            body_bytes = decode_proxy_body(response.get("body_b64")) or b""
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "bad tunnel response"})
            return
        self.protocol_version = "HTTP/1.1"
        self.send_response(status)
        for key, value in response_headers.items():
            if key.lower() in FORWARDED_RESPONSE_HEADERS:
                self.send_header(key, str(value))
        if status == HTTPStatus.OK and str(response_headers.get("content-type", "")).lower().startswith("text/html"):
            body_bytes = rewrite_device_web_html(body_bytes, node_id)
        self.send_header("Content-Length", str(len(body_bytes)))
        self._send_security_headers()
        self.end_headers()
        if method != "HEAD" and body_bytes:
            self.wfile.write(body_bytes)

    def _rate_limited(self, path: str) -> bool:
        limiter = self.server.rate_limiter
        if limiter is None or not limiter.enabled:
            return False
        ip = forwarded_client_ip(self.headers, self.client_address)
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

    def _send_mcp_json(
        self, payload: dict[str, Any], *, session_id: str | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = payload.get("result")
        negotiated = (
            str(result.get("protocolVersion"))
            if isinstance(result, dict) and result.get("protocolVersion")
            else MCP_PROTOCOL_VERSION
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("MCP-Protocol-Version", negotiated)
        if session_id is not None:
            self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _sse_events_stream(self, query: dict[str, list[str]]) -> None:
        """Worker push channel: long-lived SSE stream of worker-relevant events.

        The worker subscribes with its node token; the server then pushes
        `control/new`, `task/cancelled`, and (later phases) shared-context /
        directory events to the matching node. `after_seq` is accepted for
        forward compatibility with the resumable shared event stream.
        """
        context = self._authorized()
        if context is None:
            return
        node_id = (query.get("node_id") or [""])[0].strip()
        tenant_id = (query.get("tenant_id") or [""])[0].strip() or "default"
        if not node_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "node_id is required"})
            return
        if context.role == "node":
            if context.node_id != node_id:
                self._send_json(
                    HTTPStatus.FORBIDDEN, {"error": "node token cannot subscribe for another node"}
                )
                return
            if context.tenant_id not in (None, tenant_id):
                self._send_json(
                    HTTPStatus.FORBIDDEN, {"error": "tenant mismatch"}
                )
                return
        elif not context.is_admin:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "node or admin token required"})
            return

        events = self.server.subscribe(node_id, tenant_id)
        self.protocol_version = "HTTP/1.1"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(b"retry: 30000\n\n")
            self.wfile.write(
                (
                    'event: connected\ndata: {"node_id":'
                    + json.dumps(node_id)
                    + "}\n\n"
                ).encode("utf-8")
            )
            self.wfile.flush()
            keep_alive_until = time.monotonic() + 6 * 60 * 60
            while time.monotonic() < keep_alive_until:
                try:
                    event_name, data = events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.wfile.write(
                    f"event: {event_name}\ndata: ".encode("utf-8") + payload + b"\n\n"
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("sse_events_disconnected node=%s", node_id)
        finally:
            self.server.unsubscribe(events)
            self.close_connection = True

    def _send_mcp_endpoint(self) -> None:
        # MCP streamable HTTP expects GET /mcp to open a long-lived SSE stream.
        # Closing it right after the endpoint event makes clients reconnect
        # continuously (observed ~2 GET/s per client), so keep the stream open
        # with periodic keep-alive comments and a reconnect backoff hint.
        self.protocol_version = "HTTP/1.1"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(b"retry: 30000\n\n")
            self.wfile.write(b"event: endpoint\ndata: /mcp\n\n")
            self.wfile.flush()
            keep_alive_until = time.monotonic() + 6 * 60 * 60
            while time.monotonic() < keep_alive_until:
                time.sleep(15)
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("mcp_sse_disconnected")
        finally:
            self.close_connection = True

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
        removed = store.purge_expired_auth_tokens()
        if removed:
            logger.info("purged_expired_auth_tokens count=%s", removed)
    except Exception:
        logger.exception("purge_expired_auth_tokens_failed")
    purge_stop = threading.Event()

    def purge_loop() -> None:
        while not purge_stop.wait(6 * 60 * 60):
            try:
                removed = store.purge_expired_auth_tokens()
                if removed:
                    logger.info("purged_expired_auth_tokens count=%s", removed)
            except Exception:
                logger.exception("purge_expired_auth_tokens_failed")
            try:
                removed = store.purge_expired_shared_events()
                if removed:
                    logger.info("purged_expired_shared_events count=%s", removed)
            except Exception:
                logger.exception("purge_expired_shared_events_failed")
            try:
                removed = store.expire_questions()
                if removed:
                    logger.info("expired_questions count=%s", removed)
            except Exception:
                logger.exception("expire_questions_failed")

    threading.Thread(
        target=purge_loop, name="hub-token-purge", daemon=True
    ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        purge_stop.set()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
