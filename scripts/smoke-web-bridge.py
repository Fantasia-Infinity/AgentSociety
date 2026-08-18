"""Cross-stack smoke: real Python Hub + compiled TS WebBridge + fake dsh web.

Covers both HTTP proxy and the browser WebSocket event downlink through the
TS bridge (ws-open / ws-open-ack / ws-frame relay).
"""
from __future__ import annotations

import base64
import json
import secrets
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

sys.path.insert(0, "src")
from agent_hub.api import AgentHubApi
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore
from agent_hub.domain import PrincipalRegistration, ActorRegistration, NodeRegistration
from agent_hub.auth import AuthenticatedContext
from agent_hub.websocket import WebSocket, accept_key


class MiniWebSocketClient:
    """Minimal RFC 6455 client for smoke driving."""

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
        self._buffer = b""

    def recv_text(self, timeout: float = 10.0) -> str:
        self.sock.settimeout(timeout)
        while True:
            if len(self._buffer) >= 2:
                first, second = self._buffer[0], self._buffer[1]
                opcode = first & 0x0F
                length = second & 0x7F
                offset = 2
                if length == 126:
                    if len(self._buffer) < 4:
                        self._buffer += self._read_more()
                        continue
                    length = struct.unpack("!H", self._buffer[2:4])[0]
                    offset = 4
                elif length == 127:
                    if len(self._buffer) < 10:
                        self._buffer += self._read_more()
                        continue
                    length = struct.unpack("!Q", self._buffer[2:10])[0]
                    offset = 10
                if len(self._buffer) < offset + length:
                    self._buffer += self._read_more()
                    continue
                frame = self._buffer[offset : offset + length]
                self._buffer = self._buffer[offset + length :]
                if opcode in (0x1, 0x2):
                    return frame.decode("utf-8")
                if opcode == 0x8:
                    raise RuntimeError("closed by server")
            else:
                self._buffer += self._read_more()

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


class FakeDshWeb(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/events.mux":
            self._serve_events()
            return
        self._answer()

    def do_POST(self):
        self._answer()

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.dumps({"ok": True, "path": self.path, "echo": body.decode("utf-8")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_events(self):
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
                ws.send_text(
                    json.dumps(
                        {
                            "type": "server-request",
                            "rpcId": f"fx{index}",
                            "method": "mux/update",
                            "payload": {"seq": index},
                        }
                    )
                )
                time.sleep(0.05)
            while True:
                ws.recv_message()
        except Exception:
            pass


web = ThreadingHTTPServer(("127.0.0.1", 0), FakeDshWeb)
threading.Thread(target=web.serve_forever, daemon=True).start()

with TemporaryDirectory() as tmp:
    store = AgentHubStore(Path(tmp) / "hub.sqlite3")
    api = AgentHubApi(store)
    token = "smoke-token-123456789"
    server = HubHttpServer(("127.0.0.1", 0), api, token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    store.register_principal(PrincipalRegistration("p", "human", "Owner", {}))
    store.register_actor(ActorRegistration("a", "p", "agent", "Agent", (), {}))
    store.register_node(
        NodeRegistration(
            "node-smoke", "a", "Node", ("filesystem", "dsh-web"),
            {"dsh_web": {"enabled": True, "protocol_version": "1"}},
        )
    )
    raw, _ = store.create_auth_token(
        __import__("agent_hub.domain", fromlist=["AuthTokenCreation"]).AuthTokenCreation(
            tenant_id="default", role="node", principal_id="p", actor_id="a",
            node_id="node-smoke", label="smoke", expires_at=None,
        )
    )
    hub_url = f"http://127.0.0.1:{port}"
    target = f"http://127.0.0.1:{web.server_address[1]}"
    print(f"SMOKE_HUB={hub_url}")
    print(f"SMOKE_TARGET={target}")
    print(f"SMOKE_NODE_TOKEN={raw}")
    print(f"SMOKE_NODE_ID=node-smoke")
    print(f"SMOKE_ADMIN_TOKEN={token}")
    sys.stdout.flush()

    marker = Path("/tmp/dsh-bridge-smoke-ready")
    for _ in range(90):
        if marker.exists():
            break
        time.sleep(0.5)
    if not marker.exists():
        print("SMOKE_RESULT=bridge-marker-timeout")
        sys.exit(1)

    # 1) HTTP proxy roundtrip
    request = Request(
        f"{hub_url}/v1/web/node-smoke/api/session.list",
        data=json.dumps({"type": "client-request", "rpcId": "1", "method": "session.list", "payload": {}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    print(f"SMOKE_HTTP_RESULT={json.dumps(result, ensure_ascii=False)}")

    # 2) Browser event WebSocket downlink through the TS bridge
    browser = MiniWebSocketClient(
        f"ws://127.0.0.1:{port}/v1/web/node-smoke/ws/events/mux",
        headers={"Authorization": f"Bearer {token}"},
    )
    frames = []
    try:
        for _ in range(3):
            frames.append(json.loads(browser.recv_text(timeout=15)))
    finally:
        browser.close()
    seqs = [frame["payload"]["seq"] for frame in frames]
    print(f"SMOKE_WS_RESULT={json.dumps({'seqs': seqs, 'rpcIds': [f['rpcId'] for f in frames]})}")
    store.close()
    server.shutdown()
    web.shutdown()
