from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.errors import ApiError
from agent_hub.store import AgentHubStore


class PrincipalIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._tmp.name) / "state.sqlite3")
        self.api = AgentHubApi(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _register(self, username: str, password: str) -> None:
        status, res = self.api.post(
            "/v1/auth/register",
            {"username": username, "password": password, "display_name": username},
            None,
        )
        self.assertEqual(status, HTTPStatus.CREATED)

    def _login(self, username: str, password: str):
        _, res = self.api.post(
            "/v1/auth/login",
            {"username": username, "password": password, "label": "test"},
            None,
        )
        return self.store.authenticate_session(res["session_token"])

    def _agent_login(self, username: str, password: str, node_id: str):
        status, res = self.api.post(
            "/v1/auth/agent-login",
            {
                "username": username,
                "password": password,
                "node_id": node_id,
                "display_name": node_id,
                "capabilities": ["pi", "hub-task"],
                "metadata": {"platform": "test"},
            },
            None,
        )
        self.assertEqual(status, HTTPStatus.OK)
        return res

    def test_registration_creates_private_tenant(self) -> None:
        status, res = self.api.post(
            "/v1/auth/register",
            {
                "username": "mallory",
                "password": "correct-horse-111",
                "display_name": "Mallory",
                "tenant_id": "somewhere-else",
            },
            None,
        )
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertEqual(res["user"]["tenant_id"], "user-mallory")
        self.assertEqual(res["user"]["role"], "tenant_admin")

    def test_registration_isolates_each_user_in_own_tenant(self) -> None:
        self._register("alice", "correct-horse-222")
        self._register("bob", "correct-horse-333")
        alice = self._login("alice", "correct-horse-222")
        bob = self._login("bob", "correct-horse-333")
        self.assertEqual(alice.tenant_id, "user-alice")
        self.assertEqual(bob.tenant_id, "user-bob")
        self.assertNotEqual(alice.tenant_id, bob.tenant_id)
        self.assertEqual(alice.role, "tenant_admin")
        self.assertEqual(bob.role, "tenant_admin")

    def test_node_token_registers_only_own_identities(self) -> None:
        self._register("alice", "correct-horse-222")
        res = self._agent_login("alice", "correct-horse-222", "node-alice")
        node_ctx = self.store.authenticate_token(res["node_token"])

        status, _ = self.api.post(
            "/v1/hub/principals",
            {
                "principal_id": "human-alice",
                "kind": "human",
                "display_name": "Alice",
                "metadata": {},
            },
            node_ctx,
        )
        self.assertEqual(status, HTTPStatus.OK)

        status, _ = self.api.post(
            "/v1/hub/actors",
            {
                "actor_id": "opencode-node-alice",
                "principal_id": "human-alice",
                "kind": "agent",
                "display_name": "OpenCode on node-alice",
                "capabilities": ["code"],
                "metadata": {},
            },
            node_ctx,
        )
        self.assertEqual(status, HTTPStatus.OK)

        status, _ = self.api.post(
            "/v1/hub/nodes",
            {
                "node_id": "node-alice-opencode",
                "actor_id": "opencode-node-alice",
                "display_name": "node-alice-opencode",
                "capabilities": [],
                "metadata": {},
            },
            node_ctx,
        )
        self.assertEqual(status, HTTPStatus.OK)

        with self.assertRaises(ApiError):
            self.api.post(
                "/v1/hub/actors",
                {
                    "actor_id": "rogue",
                    "principal_id": "human-mallory",
                    "kind": "agent",
                    "display_name": "Rogue",
                    "capabilities": [],
                    "metadata": {},
                },
                node_ctx,
            )
        self._register("mallory", "correct-horse-333")
        self._agent_login("mallory", "correct-horse-333", "node-mallory")
        with self.assertRaises(ApiError):
            self.api.post(
                "/v1/hub/nodes",
                {
                    "node_id": "node-alice-rogue",
                    "actor_id": "pi-node-mallory",
                    "display_name": "node-alice-rogue",
                    "capabilities": [],
                    "metadata": {},
                },
                node_ctx,
            )

    def test_tenant_user_only_sees_own_data(self) -> None:
        self._register("alice", "correct-horse-222")
        self._register("bob", "correct-horse-333")
        self._agent_login("alice", "correct-horse-222", "node-alice")
        self._agent_login("bob", "correct-horse-333", "node-bob")

        alice = self._login("alice", "correct-horse-222")
        bob = self._login("bob", "correct-horse-333")
        # Each registration owns a private tenant, so both are tenant_admins.
        self.assertEqual(alice.role, "tenant_admin")
        self.assertEqual(bob.role, "tenant_admin")

        _, nodes = self.api.get("/v1/hub/nodes", "", bob)
        self.assertEqual([n["node_id"] for n in nodes["nodes"]], ["node-bob"])
        _, actors = self.api.get("/v1/hub/actors", "", bob)
        self.assertEqual([a["actor_id"] for a in actors["actors"]], ["pi-node-bob"])
        _, principals = self.api.get("/v1/hub/principals", "", bob)
        self.assertEqual(
            [p["principal_id"] for p in principals["principals"]],
            ["human-bob"],
        )

    def test_tenant_user_only_sees_own_tokens(self) -> None:
        self._register("alice", "correct-horse-222")
        self._register("bob", "correct-horse-333")
        self._agent_login("alice", "correct-horse-222", "node-alice")
        self._agent_login("bob", "correct-horse-333", "node-bob")
        alice = self._login("alice", "correct-horse-222")
        bob = self._login("bob", "correct-horse-333")

        status, created = self.api.post(
            "/v1/hub/tenants/user-alice/tokens",
            {
                "tenant_id": "user-alice",
                "role": "tenant_user",
                "principal_id": "human-alice",
                "label": "alice-app-token",
            },
            alice,
        )
        self.assertEqual(status, HTTPStatus.CREATED)

        _, alice_tokens = self.api.get("/v1/hub/tokens", "", alice)
        alice_labels = {t["label"] for t in alice_tokens["tokens"]}
        self.assertIn("alice-app-token", alice_labels)
        self.assertIn("agent-login node-alice", alice_labels)
        _, bob_tokens = self.api.get("/v1/hub/tokens", "", bob)
        self.assertEqual(
            [t["label"] for t in bob_tokens["tokens"]],
            ["agent-login node-bob"],
        )

        # Each tenant_admin sees only their own private tenant.
        _, nodes = self.api.get("/v1/hub/nodes", "", alice)
        self.assertEqual(
            {n["node_id"] for n in nodes["nodes"]}, {"node-alice"}
        )
        _, nodes = self.api.get("/v1/hub/nodes", "", bob)
        self.assertEqual(
            {n["node_id"] for n in nodes["nodes"]}, {"node-bob"}
        )

    def test_tenant_user_cannot_impersonate_another_principal(self) -> None:
        self._register("alice", "correct-horse-222")
        self._register("bob", "correct-horse-333")
        self._agent_login("alice", "correct-horse-222", "node-alice")
        self._agent_login("bob", "correct-horse-333", "node-bob")
        alice = self._login("alice", "correct-horse-222")
        bob = self._login("bob", "correct-horse-333")

        # Bob cannot create a task as Alice.
        with self.assertRaises(ApiError) as caught:
            self.api.post(
                "/v1/hub/tasks",
                {
                    "principal_id": "human-alice",
                    "delegator_actor_id": "pi-node-alice",
                    "objective": "impersonation attempt",
                    "assignee_actor_id": "pi-node-alice",
                },
                bob,
            )
        self.assertIn(
            caught.exception.status,
            (HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST, HTTPStatus.FORBIDDEN),
        )

        # Bob cannot delegate with Alice's actor even with his own principal.
        with self.assertRaises(ApiError):
            self.api.post(
                "/v1/hub/tasks",
                {
                    "delegator_actor_id": "pi-node-alice",
                    "objective": "cross-user delegate",
                    "assignee_actor_id": "pi-node-alice",
                },
                bob,
            )

        # Bob can create a task with his own actor.
        status, res = self.api.post(
            "/v1/hub/tasks",
            {
                "delegator_actor_id": "pi-node-bob",
                "objective": "bob task",
                "assignee_actor_id": "pi-node-bob",
            },
            bob,
        )
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertEqual(res["task"]["principal_id"], "human-bob")
        self.assertEqual(res["task"]["delegator_actor_id"], "pi-node-bob")

        # Alice's task is invisible to Bob.
        _, alice_task = self.api.post(
            "/v1/hub/tasks",
            {
                "principal_id": "human-alice",
                "delegator_actor_id": "pi-node-alice",
                "objective": "alice task",
                "assignee_actor_id": "pi-node-alice",
            },
            alice,
        )
        with self.assertRaises(ApiError) as caught:
            self.api.get(
                f"/v1/hub/tasks/{alice_task['task']['task_id']}", "", bob
            )
        self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)

        # Bob cannot list tenant tokens.
        with self.assertRaises(ApiError):
            self.api.get("/v1/hub/tenants/default/tokens", "", bob)

    def test_tenant_user_cannot_target_others_or_open_tasks(self) -> None:
        self._register("alice", "correct-horse-222")
        self._agent_login("alice", "correct-horse-222", "node-alice")
        # Add a plain tenant_user into Alice's private tenant.
        self.store.register_user(
            username="charlie",
            password="correct-horse-444",
            display_name="Charlie",
            tenant_id="user-alice",
        )
        self._agent_login("charlie", "correct-horse-444", "node-charlie")
        charlie = self._login("charlie", "correct-horse-444")
        self.assertEqual(charlie.role, "tenant_user")

        # Charlie cannot assign a task to Alice's actor.
        with self.assertRaises(ApiError) as caught:
            self.api.post(
                "/v1/hub/tasks",
                {
                    "delegator_actor_id": "pi-node-charlie",
                    "objective": "target alice",
                    "assignee_actor_id": "pi-node-alice",
                },
                charlie,
            )
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

        # Charlie cannot create an open (unassigned) task.
        with self.assertRaises(ApiError) as caught:
            self.api.post(
                "/v1/hub/tasks",
                {
                    "delegator_actor_id": "pi-node-charlie",
                    "objective": "open task",
                },
                charlie,
            )
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

        # Charlie can create a task assigned to his own actor.
        status, res = self.api.post(
            "/v1/hub/tasks",
            {
                "delegator_actor_id": "pi-node-charlie",
                "objective": "charlie task",
                "assignee_actor_id": "pi-node-charlie",
            },
            charlie,
        )
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertEqual(res["task"]["principal_id"], "human-charlie")


if __name__ == "__main__":
    unittest.main()
