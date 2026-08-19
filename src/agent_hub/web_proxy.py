"""Hub-side DSH Web tunnel coordination: device WebSocket loop and the
browser-facing request/response pairing. Transport-agnostic apart from the
WebSocket object it is handed by the HTTP server.
"""

from __future__ import annotations

import base64
import json
import logging
import posixpath
import queue
import secrets
import threading
from urllib.parse import unquote, urlsplit
from typing import Any

from .tunnel import TunnelRegistry, SendFn, CloseFn
from .websocket import WebSocket, WebSocketProtocolError

logger = logging.getLogger(__name__)

PROXY_TIMEOUT_SECONDS = 30.0
MAX_PROXY_REQUEST_BODY = 16 * 1024 * 1024  # 16 MiB
MAX_PROXY_RESPONSE_BODY = 32 * 1024 * 1024  # 32 MiB
MAX_WS_PATH = 512
MAX_WS_FRAME = 16 * 1024 * 1024
WS_OPEN_TIMEOUT_SECONDS = 10.0
# Keep upgraded WebSocket connections active without removing the ordinary
# HTTP socket timeout used for non-upgraded requests.
TUNNEL_KEEPALIVE_SECONDS = 25.0
ALLOWED_WS_PATHS = frozenset({"/api/events.mux", "/api/events.host"})

# Headers allowed to pass from the browser to the device. Never forward
# Authorization, Cookie, Host, Connection, Upgrade, or Content-Length: Hub
# credentials and session cookies must not leak to the device.
FORWARDED_REQUEST_HEADERS = frozenset(
    {"accept", "content-type", "x-requested-with"}
)
# Response headers allowed back to the browser.
FORWARDED_RESPONSE_HEADERS = frozenset(
    {"content-type", "cache-control", "content-encoding", "etag", "last-modified"}
)

ALLOWED_PROXY_PATHS = ("/api", "/api/", "/assets/", "/")


class WebTunnelCoordinator:
    """Owns pending proxy requests and dispatches device messages."""

    def __init__(self, registry: TunnelRegistry) -> None:
        self.registry = registry
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._ws_lock = threading.Lock()
        self._ws_channels: dict[str, queue.Queue[dict[str, Any]]] = {}

    # -- device side ------------------------------------------------------

    def handle_device_ws(self, ws: WebSocket, node_id: str) -> None:
        """Blocking loop for one device tunnel connection."""
        send: SendFn = lambda message: ws.send_json(message)  # noqa: E731
        close: CloseFn = lambda: ws.close()  # noqa: E731
        self.registry.attach(node_id, send, close)
        logger.info("tunnel_online node=%s", node_id)
        try:
            while True:
                opcode, payload = ws.recv_message()
                if opcode != 0x1:
                    continue  # binary frames are not part of the contract
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.warning("tunnel_bad_json node=%s", node_id)
                    continue
                if not isinstance(message, dict):
                    continue
                self._dispatch_device_message(node_id, message)
        except (WebSocketProtocolError, OSError, ValueError) as exc:
            logger.info("tunnel_offline node=%s reason=%s", node_id, exc)
        finally:
            self.registry.detach(node_id, close)
            self._fail_node(node_id)

    def _dispatch_device_message(
        self, node_id: str, message: dict[str, Any]
    ) -> None:
        kind = message.get("type")
        if kind == "http-response":
            request_id = str(message.get("id", ""))
            if not request_id:
                return
            with self._pending_lock:
                pending = self._pending.pop(request_id, None)
            if pending is not None:
                try:
                    pending.put_nowait(message)
                except queue.Full:
                    pass
            return
        if kind == "pong":
            return
        if kind in ("ws-open-ack", "ws-frame", "ws-close"):
            stream_id = str(message.get("id", ""))
            if not stream_id:
                return
            with self._ws_lock:
                channel = self._ws_channels.get(stream_id)
                if channel is None:
                    return
            try:
                channel.put_nowait(message)
            except queue.Full:
                pass
            return
        logger.debug("tunnel_message node=%s type=%s", node_id, kind)

    def _fail_node(self, node_id: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            try:
                item.put_nowait(
                    {"type": "http-response", "status": 502, "headers": {}, "body_b64": None}
                )
            except queue.Full:
                pass
        with self._ws_lock:
            channels = list(self._ws_channels.values())
            self._ws_channels.clear()
        for channel in channels:
            try:
                channel.put_nowait({"type": "ws-close", "id": "", "code": 1006})
            except queue.Full:
                pass

    # -- browser-side event streams ---------------------------------------

    def open_event_stream(self, node_id: str, path: str) -> tuple[str, queue.Queue] | None:
        """Ask the device to open a local WebSocket event stream.

        Returns (stream_id, frames queue); the queue receives ws-open-ack /
        ws-frame / ws-close messages from the device. None when the tunnel is
        offline or the request could not be sent.
        """
        if not self.registry.is_online(node_id):
            return None
        stream_id = secrets.token_hex(8)
        channel: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        with self._ws_lock:
            self._ws_channels[stream_id] = channel
        forwarded = self.registry.send_to(
            node_id,
            {"type": "ws-open", "id": stream_id, "path": path},
        )
        if not forwarded:
            with self._ws_lock:
                self._ws_channels.pop(stream_id, None)
            return None
        return stream_id, channel

    def close_event_stream(self, node_id: str, stream_id: str) -> None:
        with self._ws_lock:
            self._ws_channels.pop(stream_id, None)
        self.registry.send_to(
            node_id, {"type": "ws-close", "id": stream_id, "code": 1000}
        )

    # -- browser side -----------------------------------------------------

    def proxy_request(
        self,
        node_id: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> dict[str, Any] | None:
        """Forward one browser request to the node tunnel and await the reply.

        Returns the http-response message, or None when the node is offline,
        the request timed out, or the tunnel rejected the send.
        """
        if not self.registry.is_online(node_id):
            return None
        request_id = secrets.token_hex(8)
        pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = pending
        forwarded = self.registry.send_to(
            node_id,
            {
                "type": "http",
                "id": request_id,
                "method": method,
                "path": path,
                "headers": headers,
                "body_b64": (
                    base64.b64encode(body).decode("ascii") if body else None
                ),
            },
        )
        if not forwarded:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return None
        try:
            return pending.get(timeout=PROXY_TIMEOUT_SECONDS)
        except queue.Empty:
            return None
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)


def normalize_proxy_path(path: str) -> str | None:
    """Return a safe origin path, rejecting URL dot-segment tricks.

    The device bridge appends this value to a loopback origin and the WHATWG
    URL parser normalizes dot segments there. Rejecting them at the Hub keeps
    the proxy allowlist meaningful instead of allowing ``/api/../...`` to
    escape the dsh web surface.
    """
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    raw_path = parsed.path
    if "\\" in raw_path or any(ord(char) < 0x20 for char in raw_path):
        return None
    # Reject both literal and once-percent-decoded dot segments. A literal
    # backslash is rejected above because WHATWG HTTP URLs treat it as a path
    # separator during normalization.
    decoded_path = unquote(raw_path)
    if (
        "\\" in decoded_path
        or any(segment in (".", "..") for segment in decoded_path.split("/"))
    ):
        return None
    # Reject multiple encoded layers as well. The device-side URL parser only
    # decodes the URL once today, but rejecting nested encodings keeps this
    # allowlist safe if another intermediary decodes before forwarding.
    decoded_again = unquote(decoded_path)
    if "\\" in decoded_again or any(
        segment in (".", "..") for segment in decoded_again.split("/")
    ):
        return None
    normalized = posixpath.normpath(raw_path)
    if raw_path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    if normalized == ".":
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    # Do not silently change duplicate separators or other path syntax. The
    # local dsh server must receive exactly the path that was authorized.
    if normalized != raw_path:
        return None
    return normalized + ("?" + parsed.query if parsed.query else "")


def validate_proxy_path(path: str) -> bool:
    """Allowlist for the device-side dsh web surface."""
    normalized = normalize_proxy_path(path)
    if normalized is None:
        return False
    path_only = urlsplit(normalized).path
    if path_only in ALLOWED_PROXY_PATHS:
        return True
    return path_only.startswith("/api/") or path_only.startswith("/assets/")


def validate_ws_path(path: str) -> bool:
    """Allowlist for the tunneled DSH event WebSocket surface."""
    return len(path) <= MAX_WS_PATH and path in ALLOWED_WS_PATHS


def decode_proxy_body(body_b64: str | None) -> bytes | None:
    if body_b64 is None:
        return None
    raw = base64.b64decode(body_b64, validate=True)
    if len(raw) > MAX_PROXY_RESPONSE_BODY:
        raise ValueError("response body too large")
    return raw
