from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.domain import (
    ActorRegistration,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
    TaskStatus,
    TaskSubmission,
    TaskUpdate,
)
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore

API_TOKEN = "standalone-hub-token-123456789"


def _request(
    port: int,
    path: str,
    *,
    token: str | None = API_TOKEN,
    payload: dict | None = None,
    timeout: float = 5,
):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
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
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


class PartialResultTests(unittest.TestCase):
    """updateTask with partial_result appends a task.partial_result event."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="principal-owner",
                kind="human",
                display_name="Owner",
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-owner",
                principal_id="principal-owner",
                kind="human",
                display_name="Owner console",
                capabilities=(),
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-worker",
                principal_id="principal-owner",
                kind="agent",
                display_name="Worker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-a",
                actor_id="actor-worker",
                display_name="Node A",
                capabilities=("code",),
                metadata={},
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_partial_result_event_is_appended_and_result_stays_terminal(self) -> None:
        task, _ = self.store.create_task(
            TaskSubmission(
                principal_id="principal-owner",
                delegator_actor_id="actor-owner",
                objective="Run the suite",
                assignee_actor_id="actor-worker",
                required_capabilities=("code",),
                input={},
                metadata={},
                origin="local_ui",
                context_id=None,
                idempotency_key=None,
            )
        )
        claim = self.store.claim_task(
            actor_id="actor-worker",
            node_id="node-a",
            wait_seconds=0,
            lease_seconds=120,
        )
        assert claim is not None

        updated = self.store.update_task(
            claim["task"]["task_id"],
            TaskUpdate(
                run_id=claim["run"]["run_id"],
                lease_token=claim["lease_token"],
                status=TaskStatus.WORKING,
                message="working",
                result={},
                partial_result={
                    "phase": "tool",
                    "toolCount": 3,
                    "lastTool": "grep",
                },
            ),
        )
        self.assertEqual(updated["status"], TaskStatus.WORKING.value)
        events = self.store.list_task_events(claim["task"]["task_id"])
        partial = [
            event
            for event in events
            if event["type"] == "task.partial_result"
        ]
        self.assertEqual(len(partial), 1)
        self.assertEqual(
            partial[0]["payload"]["partial_result"]["lastTool"], "grep"
        )
        self.assertEqual(partial[0]["node_id"], "node-a")

        # The terminal result is written once and never merged with the
        # progressive state.
        final = self.store.update_task(
            claim["task"]["task_id"],
            TaskUpdate(
                run_id=claim["run"]["run_id"],
                lease_token=claim["lease_token"],
                status=TaskStatus.COMPLETED,
                message="done",
                result={"text": "all green"},
            ),
        )
        self.assertEqual(final["result"], {"text": "all green"})

    def test_update_parses_partial_result_from_dict(self) -> None:
        item = TaskUpdate.from_dict(
            {
                "run_id": "run_1",
                "lease_token": "token-1",
                "status": "working",
                "message": "working",
                "result": {},
                "partial_result": {"phase": "thinking"},
            }
        )
        self.assertEqual(item.partial_result, {"phase": "thinking"})

    def test_update_without_partial_result_keeps_it_none(self) -> None:
        item = TaskUpdate.from_dict(
            {
                "run_id": "run_1",
                "lease_token": "token-1",
                "status": "working",
                "message": "working",
                "result": {},
            }
        )
        self.assertIsNone(item.partial_result)


class SsePushTests(unittest.TestCase):
    """/v1/hub/events pushes worker-relevant events to the subscribed node."""

    def _serve(self) -> tuple[HubHttpServer, Thread]:
        store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        server = HubHttpServer(
            ("127.0.0.1", 0),
            AgentHubApi(store),
            API_TOKEN,
        )
        self.store = store
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _seed_identity(self, port: int) -> None:
        _request(
            port,
            "/v1/hub/principals",
            payload={
                "principal_id": "principal-owner",
                "kind": "human",
                "display_name": "Owner",
            },
        )
        _request(
            port,
            "/v1/hub/actors",
            payload={
                "actor_id": "actor-owner",
                "principal_id": "principal-owner",
                "kind": "human",
                "display_name": "Owner console",
            },
        )
        _request(
            port,
            "/v1/hub/actors",
            payload={
                "actor_id": "actor-worker",
                "principal_id": "principal-owner",
                "kind": "agent",
                "display_name": "Worker",
                "capabilities": ["code"],
            },
        )
        _request(
            port,
            "/v1/hub/nodes",
            payload={
                "node_id": "node-a",
                "actor_id": "actor-worker",
                "display_name": "Node A",
                "capabilities": ["code"],
            },
        )

    def _claim_task(self, port: int) -> dict:
        task = _request(
            port,
            "/v1/hub/tasks",
            payload={
                "principal_id": "principal-owner",
                "delegator_actor_id": "actor-owner",
                "objective": "Run the suite",
                "assignee_actor_id": "actor-worker",
                "required_capabilities": ["code"],
                "input": {},
            },
        )["task"]
        claim = _request(
            port,
            "/v1/hub/tasks/claim",
            payload={
                "actor_id": "actor-worker",
                "node_id": "node-a",
                "wait_seconds": 0,
                "lease_seconds": 120,
            },
        )["claim"]
        assert claim is not None
        return claim

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()

    def tearDown(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(timeout=2)
        self.store.close()
        self.temporary.cleanup()

    def _open_sse(self, port: int, node_id: str, token: str | None = API_TOKEN):
        request = Request(
            f"http://127.0.0.1:{port}/v1/hub/events?node_id={node_id}",
            headers={} if token is None else {"Authorization": f"Bearer {token}"},
        )
        return urlopen(request, timeout=5)

    def test_control_and_cancel_events_reach_the_executor_node(self) -> None:
        self.server, self.thread = self._serve()
        port = self.server.server_address[1]
        self._seed_identity(port)
        stream = self._open_sse(port, "node-a")
        try:
            # Drain the initial 'connected' event (and the retry hint).
            while True:
                line = stream.readline()
                if line == b"event: connected\n":
                    stream.readline()
                    stream.readline()
                    break
            claim = self._claim_task(port)
            task_id = claim["task"]["task_id"]
            run_id = claim["run"]["run_id"]
            lease_token = claim["lease_token"]

            # Worker activity (updates) is not pushed: the worker wrote them.
            _request(
                port,
                f"/v1/hub/tasks/{task_id}/updates",
                payload={
                    "run_id": run_id,
                    "lease_token": lease_token,
                    "status": "working",
                    "message": "working",
                    "result": {},
                    "partial_result": {"phase": "tool", "toolCount": 1},
                },
            )
            # White-box: the node's subscriber queue stays empty (updates do
            # not generate push events).
            self.assertTrue(self.server._subscribers)
            subscriber_queue = self.server._subscribers[0]["queue"]
            self.assertTrue(subscriber_queue.empty())

            _request(
                port,
                f"/v1/hub/tasks/{task_id}/controls",
                payload={
                    "actor_id": "actor-owner",
                    "kind": "steer",
                    "message": "prefer fast tests",
                },
            )
            event_name, data = self._next_event(stream)
            self.assertEqual(event_name, "control/new")
            self.assertEqual(data["task_id"], task_id)
            self.assertEqual(data["kind"], "steer")

            _request(
                port,
                f"/v1/hub/tasks/{task_id}/cancel",
                payload={"actor_id": "actor-owner", "reason": "scope changed"},
            )
            event_name, data = self._next_event(stream)
            self.assertEqual(event_name, "task/cancelled")
            self.assertEqual(data["task_id"], task_id)
            self.assertEqual(data["reason"], "scope changed")
        finally:
            stream.close()

    def test_sse_requires_authentication_and_node_ownership(self) -> None:
        self.server, self.thread = self._serve()
        port = self.server.server_address[1]
        with self.assertRaises(HTTPError) as anonymous:
            self._open_sse(port, "node-a", token=None)
        self.assertEqual(anonymous.exception.code, 401)
        anonymous.exception.close()

        # A node token is bound to one actor+node; subscribing for another
        # node must be rejected.
        self._seed_identity(port)
        _request(
            port,
            "/v1/hub/nodes",
            payload={
                "node_id": "node-other",
                "actor_id": "actor-worker",
                "display_name": "Node Other",
                "capabilities": ["code"],
            },
        )
        node_token = _request(
            port,
            "/v1/hub/tokens",
            payload={
                "tenant_id": "default",
                "role": "node",
                "actor_id": "actor-worker",
                "node_id": "node-other",
                "label": "other-node-token",
            },
        )["raw_token"]
        with self.assertRaises(HTTPError) as mismatch:
            self._open_sse(port, "node-a", token=node_token)
        self.assertEqual(mismatch.exception.code, 403)
        mismatch.exception.close()

        stream = self._open_sse(port, "node-other", token=node_token)
        try:
            first = stream.readline()
            self.assertTrue(first.startswith(b"retry:") or first.startswith(b"event:"))
        finally:
            stream.close()

    def _next_event(self, stream, timeout: float = 5.0):
        """Read one SSE event block; returns (name, data) or None on timeout."""
        import socket
        import time

        # Apply the timeout at the socket level: readline() must not block
        # past the caller's window (the server's keep-alive is 15s).
        sock = getattr(getattr(stream, "fp", None), "_sock", None)
        if sock is not None:
            sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = stream.readline()
            except (socket.timeout, TimeoutError):
                return None
            if not line:
                return None
            if line == b"event: connected\n":
                continue
            if line.startswith(b"event: "):
                name = line[len(b"event: ") :].strip().decode()
                data_line = stream.readline()
                if data_line.startswith(b"data: "):
                    data = json.loads(data_line[len(b"data: ") :])
                    return name, data
        return None


if __name__ == "__main__":
    unittest.main()
