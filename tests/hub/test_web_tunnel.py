from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import time
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.server import HubHttpServer, rewrite_device_web_html
from agent_hub.store import AgentHubStore
from agent_hub.websocket import WebSocket, accept_key
from agent_hub.web_proxy import validate_proxy_path


class FakeDshWebServer:
    """Stand-in for the device-local dsh web HTTP + event WS surface."""

    def __init__(self) -> None:
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/events.mux":
                    self._serve_events()
                    return
                self._answer()
            def do_POST(self) -> None:  # noqa: N802
                self._answer()
            def log_message(self, format: str, *args: object) -> None:
                pass
            def _answer(self) -> None:
                requests.append(f"{self.command} {self.path}")
                body = json.dumps(
                    {"ok": True, "path": self.path, "device": "fake-dsh-web"}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def _serve_events(self) -> None:
                key = self.headers.get("Sec-WebSocket-Key", "").strip()
                if not key:
                    self.send_response(400)
                    self.end_headers()
                    return
                self.protocol_version = "HTTP/1.1"
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept_key(key))
                self.end_headers()
                self.close_connection = True
                ws = WebSocket(self.rfile, self.wfile)
                try:
                    for index in range(3):
                        frame = {
                            "type": "server-request",
                            "rpcId": f"fx{index}",
                            "method": "mux/update",
                            "payload": {"seq": index},
                        }
                        ws.send_text(json.dumps(frame))
                        time.sleep(0.05)
                    while True:
                        ws.recv_message()
                except Exception:  # noqa: BLE001
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.requests = requests

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class MiniWebSocketClient:
    """Minimal RFC 6455 client for the device side of the tunnel."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port))
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        extra = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"{extra}\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("handshake closed")
            response += chunk
        head, _, _ = response.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"handshake failed: {head!r}")
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-accept:"):
                expected = accept_key(key)
                if line.split(b":", 1)[1].strip().decode("ascii") != expected:
                    raise RuntimeError("bad accept key")
        self._buffer = b""

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = secrets.token_bytes(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        body = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(bytes(header) + mask + body)

    def recv_frame(self, timeout: float = 10.0) -> tuple[int, bytes]:
        self.sock.settimeout(timeout)
        while True:
            while len(self._buffer) < 2:
                self._buffer += self._read_more()
            first, second = self._buffer[0], self._buffer[1]
            opcode = first & 0x0F
            length = second & 0x7F
            offset = 2
            if length == 126:
                while len(self._buffer) < 4:
                    self._buffer += self._read_more()
                length = struct.unpack("!H", self._buffer[2:4])[0]
                offset = 4
            elif length == 127:
                while len(self._buffer) < 10:
                    self._buffer += self._read_more()
                length = struct.unpack("!Q", self._buffer[2:10])[0]
                offset = 10
            while len(self._buffer) < offset + length:
                self._buffer += self._read_more()
            frame = self._buffer[offset : offset + length]
            self._buffer = self._buffer[offset + length :]
            if opcode == 0x9:  # ping -> pong
                mask = secrets.token_bytes(4)
                header = bytes([0x8A, 0x80 | len(frame)]) + mask
                body = bytes(frame[i] ^ mask[i % 4] for i in range(len(frame)))
                self.sock.sendall(header + body)
            return opcode, frame

    def recv_text(self, timeout: float = 10.0) -> str:
        while True:
            opcode, frame = self.recv_frame(timeout)
            if opcode == 0x8:
                raise RuntimeError("closed by server")
            if opcode in (0x1, 0x2):
                return frame.decode("utf-8")

    def _read_more(self) -> bytes:
        chunk = self.sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed")
        return chunk

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class WebTunnelIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._temporary.name) / "hub.sqlite3")
        self.api = AgentHubApi(self.store)
        self.token = "standalone-hub-token-123456789"
        self.server = HubHttpServer(
            ("127.0.0.1", 0), self.api, self.token
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.fake_web = FakeDshWebServer()
        self.store.register_principal(
            __import__("agent_hub.domain", fromlist=["PrincipalRegistration"]).PrincipalRegistration(
                principal_id="principal-owner", kind="human",
                display_name="Owner", metadata={},
            )
        )
        self.store.register_actor(
            __import__("agent_hub.domain", fromlist=["ActorRegistration"]).ActorRegistration(
                actor_id="actor-pi", principal_id="principal-owner", kind="agent",
                display_name="Pi", capabilities=(), metadata={},
            )
        )
        self.store.register_node(
            __import__("agent_hub.domain", fromlist=["NodeRegistration"]).NodeRegistration(
                node_id="node-device", actor_id="actor-pi", display_name="Device",
                capabilities=("filesystem", "dsh-web"),
                metadata={"dsh_web": {"enabled": True, "protocol_version": "1"}},
            )
        )

    def tearDown(self) -> None:
        self.fake_web.close()
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self._temporary.cleanup()

    def test_proxy_path_allowlist_accepts_frontend_metadata_assets(self) -> None:
        self.assertTrue(validate_proxy_path("/manifest.webmanifest"))
        self.assertTrue(validate_proxy_path("/favicon.svg"))
        self.assertTrue(validate_proxy_path("/plugins/events"))

    def test_proxy_path_allowlist_rejects_dot_segments(self) -> None:
        self.assertFalse(validate_proxy_path("/api/../../etc/passwd"))
        self.assertFalse(validate_proxy_path("/assets/../api/session.list"))

        self.assertFalse(validate_proxy_path("/api/%2e%2e/etc/passwd"))
        self.assertFalse(validate_proxy_path("/api/%252e%2e/etc/passwd"))
        self.assertTrue(validate_proxy_path("/api/session.list?x=1"))
        self.assertTrue(validate_proxy_path("/?x=1"))
        self.assertTrue(validate_proxy_path("/plugins/@deepseek-ai/dsh-base/client.js?rev=1"))
        self.assertFalse(validate_proxy_path("/plugin/escape.js"))


    def test_frontend_html_is_rewritten_to_node_mount(self) -> None:
        html = rewrite_device_web_html(
            b'<head><script src="/assets/app.js"></script>'
            b'<link rel="manifest" href="/manifest.webmanifest">'
            b'<link href="/favicon.svg"></head>'
            b'<script>window.__DSH_BOOT__={"url":"/plugins/pkg/client.js"}</script>',
            "node/device",
        )
        self.assertIn(b'/v1/web/node%2Fdevice/assets/app.js', html)
        self.assertIn(b'/v1/web/node%2Fdevice/manifest.webmanifest', html)
        self.assertIn(b'/v1/web/node%2Fdevice/plugins/pkg/client.js', html)
        self.assertIn(b'__DSH_HUB_WEB_MOUNT__', html)
        self.assertIn(b'/api/events.mux', html)
        self.assertIn(b'ws/events/', html)

    def _open_tunnel(self) -> MiniWebSocketClient:
        request = Request(
            f"{self.base}/v1/hub/nodes/web/tunnel",
            data=json.dumps({"node_id": "node-device"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ticket = payload["ticket"]
        ws = MiniWebSocketClient(
            f"ws://127.0.0.1:{self.port}{payload['ws_path']}?ticket={ticket}"
        )
        # First message the device receives is the forwarded browser request.
        return ws

    def test_idle_device_tunnel_stays_online_with_ping_pong(self) -> None:
        import agent_hub.server as server_module

        old_keepalive = server_module.TUNNEL_KEEPALIVE_SECONDS
        old_timeout = server_module.HubRequestHandler.timeout
        server_module.TUNNEL_KEEPALIVE_SECONDS = 0.2
        server_module.HubRequestHandler.timeout = 0.5
        ws = self._open_tunnel()
        try:
            deadline = time.monotonic() + 2.0
            pings = 0
            while time.monotonic() < deadline and pings < 3:
                opcode, _payload = ws.recv_frame(timeout=1.0)
                if opcode == 0x9:
                    pings += 1
            self.assertGreaterEqual(pings, 2)
            self.assertTrue(self.server.tunnels.is_online("node-device"))
        finally:
            ws.close()
            server_module.TUNNEL_KEEPALIVE_SECONDS = old_keepalive
            server_module.HubRequestHandler.timeout = old_timeout

    def test_idle_browser_event_ws_stays_open_with_ping_pong(self) -> None:
        import agent_hub.server as server_module

        old_keepalive = server_module.TUNNEL_KEEPALIVE_SECONDS
        server_module.TUNNEL_KEEPALIVE_SECONDS = 0.2
        ws = self._open_tunnel()
        device_done = threading.Event()

        def device_loop() -> None:
            try:
                message = json.loads(ws.recv_text(timeout=5))
                self.assertEqual(message["type"], "ws-open")
                ws.send_text(
                    json.dumps(
                        {"type": "ws-open-ack", "id": message["id"], "ok": True}
                    )
                )
                while True:
                    opcode, _payload = ws.recv_frame(timeout=5)
                    if opcode == 0x8:
                        return
            except (OSError, RuntimeError, TimeoutError):
                return
            finally:
                device_done.set()

        thread = threading.Thread(target=device_loop, daemon=True)
        thread.start()
        try:
            browser = MiniWebSocketClient(
                f"ws://127.0.0.1:{self.port}/v1/web/node-device/ws/events/mux",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            try:
                pings = 0
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and pings < 2:
                    opcode, _payload = browser.recv_frame(timeout=1.0)
                    if opcode == 0x9:
                        pings += 1
                self.assertGreaterEqual(pings, 2)
            finally:
                browser.close()
        finally:
            ws.close()
            device_done.wait(timeout=2)
            server_module.TUNNEL_KEEPALIVE_SECONDS = old_keepalive

    def test_full_tunnel_proxy_roundtrip(self) -> None:
        ws = self._open_tunnel()

        def device_loop() -> None:
            message = json.loads(ws.recv_text())
            self.assertEqual(message["type"], "http")
            self.assertEqual(message["method"], "POST")
            self.assertEqual(message["path"], "/api/session.list")
            forwarded = Request(
                f"{self.fake_web.base_url}{message['path']}",
                data=base64.b64decode(message["body_b64"]),
                headers={k: v for k, v in message["headers"].items()},
                method="POST",
            )
            with urlopen(forwarded, timeout=5) as response:
                body = response.read()
            ws.send_text(
                json.dumps(
                    {
                        "type": "http-response",
                        "id": message["id"],
                        "status": response.status,
                        "headers": {"Content-Type": response.headers.get("Content-Type", "")},
                        "body_b64": base64.b64encode(body).decode("ascii"),
                    }
                )
            )

        thread = threading.Thread(target=device_loop, daemon=True)
        thread.start()
        try:
            browser_body = json.dumps(
                {"type": "client-request", "rpcId": "1", "method": "session.list", "payload": {}}
            ).encode("utf-8")
            request = Request(
                f"{self.base}/v1/web/node-device/api/session.list",
                data=browser_body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["path"], "/api/session.list")
            self.assertEqual(result["device"], "fake-dsh-web")
            self.assertEqual(
                self.fake_web.requests, ["POST /api/session.list"]
            )
        finally:
            ws.close()

    def test_frontend_metadata_paths_are_allowlisted(self) -> None:
        self.assertTrue(validate_proxy_path("/manifest.webmanifest"))
        self.assertTrue(validate_proxy_path("/favicon.svg"))

    def test_proxy_rejects_disallowed_path(self) -> None:
        request = Request(
            f"{self.base}/v1/web/node-device/etc/passwd",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 403)

    def test_proxy_requires_auth(self) -> None:
        request = Request(f"{self.base}/v1/web/node-device/api/session.list")
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 401)

    def test_proxy_offline_device_returns_bad_gateway(self) -> None:
        request = Request(
            f"{self.base}/v1/web/node-device/api/session.list",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 502)

    def test_browser_event_ws_forwards_device_frames(self) -> None:
        """Full event path: browser WS -> Hub -> tunnel -> device local WS."""
        ws = self._open_tunnel()
        relay_error: list[Exception] = []

        def device_loop() -> None:
            try:
                while True:
                    message = json.loads(ws.recv_text())
                    kind = message["type"]
                    if kind == "ws-open":
                        local = MiniWebSocketClient(
                            f"ws://127.0.0.1:{self.fake_web.server.server_address[1]}{message['path']}"
                        )
                        ws.send_text(
                            json.dumps(
                                {"type": "ws-open-ack", "id": message["id"], "ok": True}
                            )
                        )
                        try:
                            while True:
                                frame = local.recv_text(timeout=5)
                                ws.send_text(
                                    json.dumps(
                                        {
                                            "type": "ws-frame",
                                            "id": message["id"],
                                            "opcode": 1,
                                            "payload_b64": base64.b64encode(
                                                frame.encode("utf-8")
                                            ).decode("ascii"),
                                        }
                                    )
                                )
                        except Exception as exc:  # noqa: BLE001
                            relay_error.append(exc)
                            return
                    elif kind == "ws-close":
                        return
            except Exception as exc:  # noqa: BLE001
                relay_error.append(exc)

        thread = threading.Thread(target=device_loop, daemon=True)
        thread.start()
        try:
            browser = MiniWebSocketClient(
                f"ws://127.0.0.1:{self.port}/v1/web/node-device/ws/events/mux",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            received = []
            try:
                for _ in range(3):
                    frame = json.loads(browser.recv_text(timeout=10))
                    received.append(frame)
            finally:
                browser.close()
            self.assertEqual(
                [frame["payload"]["seq"] for frame in received], [0, 1, 2]
            )
            self.assertEqual(received[0]["rpcId"], "fx0")
            self.assertEqual(received[0]["method"], "mux/update")
        finally:
            ws.close()
        self.assertEqual(relay_error, [])

    def test_event_ws_rejects_disabled_node_web(self) -> None:
        # A second node without the dsh_web capability must be denied.
        self.store.register_node(
            __import__("agent_hub.domain", fromlist=["NodeRegistration"]).NodeRegistration(
                node_id="node-plain", actor_id="actor-pi", display_name="Plain",
                capabilities=("filesystem",), metadata={},
            )
        )
        try:
            browser = MiniWebSocketClient(
                f"ws://127.0.0.1:{self.port}/v1/web/node-plain/ws/events/mux",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            browser.close()
            self.fail("expected the disabled node to be rejected")
        except RuntimeError as exc:
            self.assertIn("handshake failed", str(exc))


if __name__ == "__main__":
    unittest.main()
