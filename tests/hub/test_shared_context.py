from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.auth import AuthenticatedContext
from agent_hub.domain import (
    ActorRegistration,
    NodeRegistration,
    PrincipalRegistration,
    SharedEventAppend,
)
from agent_hub.mcp import McpService
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore

API_TOKEN = "standalone-hub-token-123456789"


def _mcp_call(service, method, params, *, context=None, request_id=1):
    response = service.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
        context,
    )
    assert response is not None
    return response


class SharedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="principal-a",
                kind="human",
                display_name="A",
                metadata={},
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _append(self, **overrides) -> dict:
        item = SharedEventAppend(
            scope=overrides.pop("scope", "consensus"),
            kind=overrides.pop("kind", "digest"),
            payload=overrides.pop("payload", {"title": "T", "summary": "S"}),
            principal_id=overrides.pop("principal_id", "principal-a"),
            session_id=overrides.pop("session_id", "session-1"),
            ttl_hours=overrides.pop("ttl_hours", 720),
            **overrides,
        )
        return self.store.append_shared_event(item)

    def test_append_is_idempotent_by_event_id(self) -> None:
        first = self._append(event_id="evt-1")
        second = self._append(event_id="evt-1")
        self.assertEqual(first["seq"], second["seq"])
        events = self.store.list_shared_events(tenant_id="default")
        self.assertEqual(len(events), 1)

    def test_incremental_pull_and_filters(self) -> None:
        self._append(kind="digest", session_id="session-1", event_id="evt-1")
        self._append(
            kind="fact",
            payload={"fact": "x"},
            session_id=None,
            event_id="evt-2",
        )
        self._append(
            kind="digest", session_id="session-2", event_id="evt-3"
        )
        all_events = self.store.list_shared_events(tenant_id="default")
        self.assertEqual(len(all_events), 3)
        after = self.store.list_shared_events(
            tenant_id="default", after_seq=all_events[0]["seq"]
        )
        self.assertEqual(len(after), 2)
        digests = self.store.list_shared_events(
            tenant_id="default", kind="digest"
        )
        self.assertEqual(len(digests), 2)
        session1 = self.store.list_shared_events(
            tenant_id="default", session_id="session-1"
        )
        self.assertEqual(len(session1), 1)
        scoped = self.store.list_shared_events(
            tenant_id="default", scope="qa"
        )
        self.assertEqual(scoped, [])

    def test_ttl_purge(self) -> None:
        self._append(event_id="evt-ttl", ttl_hours=1)
        with self.store._connection:
            self.store._connection.execute(
                "UPDATE hub_shared_events SET expires_at=? WHERE event_id='evt-ttl'",
                (0,),
            )
        removed = self.store.purge_expired_shared_events()
        self.assertEqual(removed, 1)
        events = self.store.list_shared_events(tenant_id="default")
        self.assertEqual(events, [])

    def test_snapshot_compacts_digests_and_recent_facts(self) -> None:
        self._append(kind="digest", session_id="s1", event_id="evt-1")
        self._append(
            kind="digest", session_id="s1", payload={"title": "new"}, event_id="evt-2"
        )
        self._append(kind="fact", payload={"fact": 1}, event_id="evt-3")
        snapshot = self.store.shared_snapshot(tenant_id="default")
        self.assertEqual(
            snapshot["digests"]["s1"]["payload"]["title"], "new"
        )
        self.assertEqual(len(snapshot["recent"]), 1)

    def test_expired_entries_hidden_from_pull(self) -> None:
        self._append(event_id="evt-live")
        self._append(event_id="evt-dead", ttl_hours=1)
        with self.store._connection:
            self.store._connection.execute(
                "UPDATE hub_shared_events SET expires_at=? WHERE event_id='evt-dead'",
                (0,),
            )
        events = self.store.list_shared_events(tenant_id="default")
        self.assertEqual([e["event_id"] for e in events], ["evt-live"])


class SharedContextApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="principal-a",
                kind="human",
                display_name="A",
                metadata={},
            )
        )
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="principal-b",
                kind="human",
                display_name="B",
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-a",
                principal_id="principal-a",
                kind="agent",
                display_name="A worker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-a",
                actor_id="actor-a",
                display_name="Node A",
                capabilities=("code",),
                metadata={},
            )
        )
        self.api = AgentHubApi(self.store)
        self.server = HubHttpServer(
            ("127.0.0.1", 0),
            self.api,
            API_TOKEN,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temporary.cleanup()

    def _node_token(self) -> str:
        raw, _record = self.store.create_auth_token(
            self._token_item()
        )
        return raw

    def _token_item(self):
        from agent_hub.domain import AuthTokenCreation

        return AuthTokenCreation(
            tenant_id="default",
            role="node",
            principal_id="principal-a",
            actor_id="actor-a",
            node_id="node-a",
            label="node-a-token",
            expires_at=None,
        )

    def _request(self, port, path, *, token=API_TOKEN, payload=None):
        headers = {"Authorization": f"Bearer {token}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        request = Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_node_token_appends_under_its_own_identity(self) -> None:
        port = self.server.server_address[1]
        node_token = self._node_token()
        appended = self._request(
            port,
            "/v1/hub/contexts/append",
            token=node_token,
            payload={
                "kind": "fact",
                "payload": {"fact": "hub is up"},
                "session_id": "session-a",
            },
        )["event"]
        self.assertEqual(appended["principal_id"], "principal-a")
        self.assertEqual(appended["actor_id"], "actor-a")
        self.assertEqual(appended["node_id"], "node-a")
        self.assertEqual(appended["scope"], "consensus")

    def test_principal_isolation_on_read(self) -> None:
        port = self.server.server_address[1]
        self._request(
            port,
            "/v1/hub/contexts/append",
            payload={
                "principal_id": "principal-a",
                "kind": "digest",
                "payload": {"title": "secret-of-a"},
                "session_id": "session-a",
            },
        )
        self._request(
            port,
            "/v1/hub/contexts/append",
            payload={
                "principal_id": "principal-b",
                "kind": "digest",
                "payload": {"title": "secret-of-b"},
                "session_id": "session-b",
            },
        )
        # Admin sees both.
        admin_events = self._request(
            port, "/v1/hub/contexts"
        )["events"]
        self.assertEqual(len(admin_events), 2)
        # A node token of principal-a sees only principal-a entries.
        node_token = self._node_token()
        node_events = self._request(
            port, "/v1/hub/contexts", token=node_token
        )["events"]
        self.assertEqual(len(node_events), 1)
        self.assertEqual(node_events[0]["principal_id"], "principal-a")

    def test_shared_event_pushed_to_tenant_subscribers(self) -> None:
        port = self.server.server_address[1]
        stream = urlopen(
            Request(
                f"http://127.0.0.1:{port}/v1/hub/events?node_id=node-a",
                headers={"Authorization": f"Bearer {API_TOKEN}"},
            ),
            timeout=5,
        )
        try:
            # Drain the connected event.
            while True:
                line = stream.readline()
                if line == b"event: connected\n":
                    stream.readline()
                    stream.readline()
                    break
            self._request(
                port,
                "/v1/hub/contexts/append",
                payload={
                    "principal_id": "principal-a",
                    "kind": "decision",
                    "payload": {"decision": "use zstd"},
                    "session_id": "session-a",
                },
            )
            while True:
                line = stream.readline()
                if line.startswith(b"event: "):
                    name = line[len(b"event: ") :].strip().decode()
                    data_line = stream.readline()
                    data = json.loads(data_line[len(b"data: ") :])
                    break
            self.assertEqual(name, "shared/event")
            self.assertEqual(data["kind"], "decision")
        finally:
            stream.close()

    def test_mcp_context_tools(self) -> None:
        service = McpService(self.api)
        appended = _mcp_call(
            service,
            "tools/call",
            {
                "name": "hub_context_append",
                "arguments": {
                    "kind": "note",
                    "payload": {"note": "hello"},
                    "session_id": "session-mcp",
                },
            },
        )["result"]["content"][0]["text"]
        event = json.loads(appended)["event"]
        self.assertEqual(event["kind"], "note")
        self.assertEqual(event["scope"], "consensus")
        read = _mcp_call(
            service,
            "tools/call",
            {"name": "hub_context_read", "arguments": {"limit": 10}},
        )
        events = json.loads(read["result"]["content"][0]["text"])["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], event["event_id"])


if __name__ == "__main__":
    unittest.main()
