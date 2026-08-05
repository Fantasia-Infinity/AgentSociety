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

    def test_registration_ignores_custom_tenant(self) -> None:
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
        self.assertEqual(res["user"]["tenant_id"], "default")

    def test_tenant_user_only_sees_own_data(self) -> None:
        self._register("alice", "correct-horse-222")
        self._register("bob", "correct-horse-333")
        self._agent_login("alice", "correct-horse-222", "node-alice")
        self._agent_login("bob", "correct-horse-333", "node-bob")

        alice = self._login("alice", "correct-horse-222")
        bob = self._login("bob", "correct-horse-333")
        # First user is tenant_admin; bob is a plain tenant_user.
        self.assertEqual(alice.role, "tenant_admin")
        self.assertEqual(bob.role, "tenant_user")

        _, nodes = self.api.get("/v1/hub/nodes", "", bob)
        self.assertEqual([n["node_id"] for n in nodes["nodes"]], ["node-bob"])
        _, actors = self.api.get("/v1/hub/actors", "", bob)
        self.assertEqual([a["actor_id"] for a in actors["actors"]], ["pi-node-bob"])
        _, principals = self.api.get("/v1/hub/principals", "", bob)
        self.assertEqual(
            [p["principal_id"] for p in principals["principals"]],
            ["human-bob"],
        )

        # Alice (tenant_admin) sees everything in the tenant.
        _, nodes = self.api.get("/v1/hub/nodes", "", alice)
        self.assertEqual(
            {n["node_id"] for n in nodes["nodes"]}, {"node-alice", "node-bob"}
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
        self.assertIn(caught.exception.status, (HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST))

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


if __name__ == "__main__":
    unittest.main()
