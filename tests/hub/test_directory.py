from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.domain import (
    ActorRegistration,
    ArtifactSubmission,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
    RunSubmission,
    SharedEventAppend,
    TaskStatus,
    TaskSubmission,
    TaskUpdate,
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


class DirectoryStoreTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _upsert(self, session_id: str, **row) -> dict:
        return self.store.upsert_directory_row(
            tenant_id="default",
            principal_id="principal-a",
            session_id=session_id,
            actor_id="actor-a",
            node_id="node-a",
            row={
                "title": "Session title",
                "workspace": "/repo",
                "status": "idle",
                "last_active_at": 1,
                "session_mode": "per_task",
                "tool_policy": "full",
                "invocations": [],
                **row,
            },
        )

    def test_latest_row_wins_per_session(self) -> None:
        self._upsert("session-1", status="idle", title="old")
        self._upsert("session-1", status="working", title="new")
        rows = self.store.list_directory(tenant_id="default")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["status"], "working")
        self.assertEqual(rows[0]["payload"]["title"], "new")
        self.assertEqual(rows[0]["session_id"], "session-1")

    def test_incremental_and_filters(self) -> None:
        first = self._upsert("session-1", title="alpha beta")
        second = self._upsert("session-2", title="gamma", status="working")
        all_rows = self.store.list_directory(tenant_id="default")
        self.assertEqual(len(all_rows), 2)
        after = self.store.list_directory(
            tenant_id="default", after_seq=first["seq"]
        )
        self.assertEqual([r["session_id"] for r in after], ["session-2"])
        query = self.store.list_directory(
            tenant_id="default", query="alpha"
        )
        self.assertEqual([r["session_id"] for r in query], ["session-1"])
        working = self.store.list_directory(
            tenant_id="default", status="working"
        )
        self.assertEqual([r["session_id"] for r in working], ["session-2"])
        actor = self.store.list_directory(
            tenant_id="default", actor_id="actor-a"
        )
        self.assertEqual(len(actor), 2)

    def test_get_row_and_artifacts_for_session(self) -> None:
        self._upsert("session-1")
        row = self.store.get_directory_row(
            tenant_id="default", principal_id=None, session_id="session-1"
        )
        assert row is not None
        self.assertEqual(row["payload"]["title"], "Session title")
        missing = self.store.get_directory_row(
            tenant_id="default", principal_id=None, session_id="nope"
        )
        self.assertIsNone(missing)

        # A claimed run with dsh_session_id in its result plus an artifact
        # should be found by artifacts_for_session.
        task, _ = self.store.create_task(
            TaskSubmission(
                principal_id="principal-a",
                delegator_actor_id="actor-a",
                objective="Run it",
                assignee_actor_id="actor-a",
                required_capabilities=("code",),
                input={},
                metadata={},
                origin="hub",
                context_id=None,
                idempotency_key=None,
            )
        )
        claim = self.store.claim_task(
            actor_id="actor-a",
            node_id="node-a",
            wait_seconds=0,
            lease_seconds=120,
        )
        assert claim is not None
        self.store.update_task(
            claim["task"]["task_id"],
            TaskUpdate(
                run_id=claim["run"]["run_id"],
                lease_token=claim["lease_token"],
                status=TaskStatus.COMPLETED,
                message="done",
                result={"text": "ok", "dsh_session_id": "session-1"},
            ),
        )
        self.store.add_artifact(
            ArtifactSubmission(
                name="transcript.jsonl",
                media_type="application/x-ndjson",
                uri="file:///tmp/transcript.jsonl",
                task_id=claim["task"]["task_id"],
                run_id=claim["run"]["run_id"],
                created_by_actor_id="actor-a",
                sha256=None,
                size_bytes=None,
                metadata={"dsh_session_id": "session-1"},
            )
        )
        artifacts = self.store.artifacts_for_session(
            tenant_id="default", session_id="session-1"
        )
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["name"], "transcript.jsonl")


class DirectoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        for principal in ("principal-a", "principal-b"):
            self.store.register_principal(
                PrincipalRegistration(
                    principal_id=principal,
                    kind="human",
                    display_name=principal,
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
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-b",
                principal_id="principal-b",
                kind="agent",
                display_name="B worker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-b",
                actor_id="actor-b",
                display_name="Node B",
                capabilities=("code",),
                metadata={},
            )
        )
        self.api = AgentHubApi(self.store)
        self.server = HubHttpServer(("127.0.0.1", 0), self.api, API_TOKEN)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temporary.cleanup()

    def _node_token(self) -> str:
        raw, _ = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="default",
                role="node",
                principal_id="principal-a",
                actor_id="actor-a",
                node_id="node-a",
                label="node-a",
                expires_at=None,
            )
        )
        return raw

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

    def test_node_token_upsert_and_depth_expansion(self) -> None:
        port = self.server.server_address[1]
        token = self._node_token()
        row = self._request(
            port,
            "/v1/hub/directory/session-a",
            token=token,
            payload={
                "title": "Build docs",
                "workspace": "/repo",
                "status": "idle",
                "last_active_at": 42,
                "session_mode": "per_task",
                "tool_policy": "full",
                "invocations": [],
            },
        )["row"]
        self.assertEqual(row["principal_id"], "principal-a")
        self.assertEqual(row["actor_id"], "actor-a")
        self.assertEqual(row["node_id"], "node-a")
        # Seed a consensus digest for the depth-2 expansion.
        self.store.append_shared_event(
            SharedEventAppend(
                scope="consensus",
                kind="digest",
                payload={"summary": "built"},
                principal_id="principal-a",
                session_id="session-a",
                actor_id="actor-a",
                node_id="node-a",
                ttl_hours=720,
                event_id="digest-1",
            )
        )
        deep = self._request(
            port, "/v1/hub/directory/session-a?depth=2", token=token
        )["row"]
        self.assertEqual(len(deep["consensus"]), 1)
        self.assertEqual(deep["consensus"][0]["kind"], "digest")
        self.assertNotIn("artifacts", deep)

    def test_directory_list_and_principal_isolation(self) -> None:
        port = self.server.server_address[1]
        token_a = self._node_token()
        self._request(
            port,
            "/v1/hub/directory/session-a",
            token=token_a,
            payload={
                "title": "session of a",
                "workspace": "/a",
                "status": "working",
                "last_active_at": 1,
                "session_mode": "per_task",
                "tool_policy": "full",
                "invocations": [],
            },
        )
        self._request(
            port,
            "/v1/hub/directory/session-b",
            payload={
                "principal_id": "principal-b",
                "actor_id": "actor-b",
                "node_id": "node-b",
                "title": "session of b",
                "workspace": "/b",
                "status": "idle",
                "last_active_at": 1,
                "session_mode": "per_task",
                "tool_policy": "full",
                "invocations": [],
            },
        )
        rows_a = self._request(
            port, "/v1/hub/directory", token=token_a
        )["rows"]
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["session_id"], "session-a")
        admin_rows = self._request(port, "/v1/hub/directory")["rows"]
        self.assertEqual(len(admin_rows), 2)

    def test_mcp_directory_tools(self) -> None:
        service = McpService(self.api)
        self.store.upsert_directory_row(
            tenant_id="default",
            principal_id="principal-a",
            session_id="session-1",
            actor_id="actor-a",
            node_id="node-a",
            row={
                "title": "Fix the parser",
                "workspace": "/repo",
                "status": "idle",
                "last_active_at": 1,
                "session_mode": "per_task",
                "tool_policy": "full",
                "invocations": [],
            },
        )
        listed = _mcp_call(
            service,
            "tools/call",
            {"name": "hub_directory_list", "arguments": {}},
        )
        rows = json.loads(listed["result"]["content"][0]["text"])["rows"]
        self.assertEqual(len(rows), 1)
        searched = _mcp_call(
            service,
            "tools/call",
            {"name": "hub_directory_search", "arguments": {"query": "parser"}},
        )
        rows = json.loads(searched["result"]["content"][0]["text"])["rows"]
        self.assertEqual(len(rows), 1)
        got = _mcp_call(
            service,
            "tools/call",
            {
                "name": "hub_directory_get",
                "arguments": {"session_id": "session-1", "depth": 1},
            },
        )
        row = json.loads(got["result"]["content"][0]["text"])["row"]
        self.assertEqual(row["payload"]["title"], "Fix the parser")


if __name__ == "__main__":
    unittest.main()
