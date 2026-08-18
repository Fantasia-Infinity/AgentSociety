"""Cross-stack smoke: real Python Hub + compiled TS WebBridge + fake dsh web."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
import sys

sys.path.insert(0, "src")
from agent_hub.api import AgentHubApi
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore
from agent_hub.domain import PrincipalRegistration, ActorRegistration, NodeRegistration
from agent_hub.auth import AuthenticatedContext

class FakeDshWeb(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.dumps({"ok": True, "path": self.path, "echo": body.decode("utf-8")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *args):
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
    store.register_node(NodeRegistration("node-smoke", "a", "Node", ("filesystem",), {}))
    admin = AuthenticatedContext(role="admin")
    # create a node token the bridge can use
    raw, _ = store.create_auth_token(
        __import__("agent_hub.domain", fromlist=["AuthTokenCreation"]).AuthTokenCreation(
            tenant_id="default", role="node", principal_id="p", actor_id="a",
            node_id="node-smoke", label="smoke", expires_at=None,
        )
    )
    node_token = raw
    hub_url = f"http://127.0.0.1:{port}"
    target = f"http://127.0.0.1:{web.server_address[1]}"
    print(f"SMOKE_HUB={hub_url}")
    print(f"SMOKE_TARGET={target}")
    print(f"SMOKE_NODE_TOKEN={node_token}")
    print(f"SMOKE_NODE_ID=node-smoke")
    print(f"SMOKE_ADMIN_TOKEN={token}")
    sys.stdout.flush()

    # wait for the bridge marker file to appear, then drive a browser request
    import time
    marker = Path("/tmp/dsh-bridge-smoke-ready")
    for _ in range(90):
        if marker.exists():
            break
        time.sleep(0.5)
    if not marker.exists():
        print("SMOKE_RESULT=bridge-marker-timeout")
        sys.exit(1)

    request = Request(
        f"{hub_url}/v1/web/node-smoke/api/session.list",
        data=json.dumps({"type": "client-request", "rpcId": "1", "method": "session.list", "payload": {}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    print(f"SMOKE_RESULT={json.dumps(result, ensure_ascii=False)}")
    store.close()
    server.shutdown()
    web.shutdown()
