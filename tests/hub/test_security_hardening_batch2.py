"""Regression tests for the second Hub security hardening batch.

Covers:
- node-scoped DSH Web access and node re-binding guards
- node tokens may only cancel/steer tasks on their own node
- tunnel tickets are pruned and capped
- failed registration cannot overwrite tenant metadata / claim reserved tenants
- expired lockouts reset the attempt budget; password length is bounded
- SSE subscriber count is capped
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_hub.api import AgentHubApi
from agent_hub.auth import AuthenticatedContext
from agent_hub.domain import (
    ActorRegistration,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
    TaskSubmission,
    TenantRegistration,
)
from agent_hub.errors import ApiError
from agent_hub.passwords import validate_password
from agent_hub.server import HubHttpServer, MAX_SSE_SUBSCRIBERS, node_web_allowed
from agent_hub.store import AgentHubStore
from agent_hub.tunnel import TunnelRegistry


class Batch2Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._temporary.name) / "hub.sqlite3")
        for tenant in ("tenant-a", "tenant-b"):
            self.store.create_tenant(
                TenantRegistration(
                    tenant_id=tenant, display_name=tenant, metadata={}
                )
            )
            self.store.register_principal(
                PrincipalRegistration(
                    principal_id=f"owner-{tenant}",
                    kind="human",
                    display_name=f"Owner {tenant}",
                    metadata={},
                ),
                tenant_id=tenant,
            )
            self.store.register_actor(
                ActorRegistration(
                    actor_id=f"pi-{tenant}",
                    principal_id=f"owner-{tenant}",
                    kind="agent",
                    display_name=f"Pi {tenant}",
                    capabilities=("code",),
                    metadata={},
                ),
                tenant_id=tenant,
            )
            self.store.register_node(
                NodeRegistration(
                    node_id=f"node-{tenant}",
                    actor_id=f"pi-{tenant}",
                    display_name=f"Node {tenant}",
                    capabilities=("code",),
                    metadata={"dsh_web": {"enabled": True}},
                ),
                tenant_id=tenant,
            )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def node_context(self, tenant: str) -> AuthenticatedContext:
        context = AuthenticatedContext(
            role="node",
            tenant_id=tenant,
            principal_id=f"owner-{tenant}",
            actor_id=f"pi-{tenant}",
            node_id=f"node-{tenant}",
        )
        return context


class NodeWebScopeTests(Batch2Fixture):
    def test_node_token_reaches_only_its_own_node(self) -> None:
        self.assertTrue(
            node_web_allowed(self.store, self.node_context("tenant-a"), "node-tenant-a")
        )
        self.assertFalse(
            node_web_allowed(self.store, self.node_context("tenant-a"), "node-tenant-b")
        )

    def test_sibling_node_of_same_tenant_is_not_node_scope(self) -> None:
        self.store.register_actor(
            ActorRegistration(
                actor_id="pi-sibling",
                principal_id="owner-tenant-a",
                kind="agent",
                display_name="Sibling",
                capabilities=("code",),
                metadata={},
            ),
            tenant_id="tenant-a",
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-sibling",
                actor_id="pi-sibling",
                display_name="Sibling node",
                capabilities=("code",),
                metadata={"dsh_web": {"enabled": True}},
            ),
            tenant_id="tenant-a",
        )
        self.assertFalse(
            node_web_allowed(self.store, self.node_context("tenant-a"), "node-sibling")
        )
        principal_context = AuthenticatedContext(
            role="tenant_user",
            tenant_id="tenant-a",
            principal_id="owner-tenant-a",
        )
        self.assertTrue(node_web_allowed(self.store, principal_context, "node-sibling"))
        other_principal_context = AuthenticatedContext(
            role="tenant_user",
            tenant_id="tenant-a",
            principal_id="owner-tenant-b",
        )
        self.assertFalse(
            node_web_allowed(self.store, other_principal_context, "node-sibling")
        )


class NodeRebindTests(Batch2Fixture):
    def _token(self, tenant: str) -> str:
        raw, _ = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id=tenant,
                role="node",
                principal_id=None,
                actor_id=f"pi-{tenant}",
                node_id=f"node-{tenant}",
                label="node",
                expires_at=None,
            )
        )
        return raw

    def _node_payload(self, tenant: str, node_id: str, actor_id: str) -> dict:
        return {
            "node_id": node_id,
            "actor_id": actor_id,
            "display_name": node_id,
            "capabilities": ["code"],
            "metadata": {},
        }

    def test_node_token_cannot_rebind_sibling_node(self) -> None:
        self.store.register_actor(
            ActorRegistration(
                actor_id="pi-sibling",
                principal_id="owner-tenant-a",
                kind="agent",
                display_name="Sibling",
                capabilities=("code",),
                metadata={},
            ),
            tenant_id="tenant-a",
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-sibling",
                actor_id="pi-sibling",
                display_name="Sibling node",
                capabilities=("code",),
                metadata={},
            ),
            tenant_id="tenant-a",
        )
        raw = self._token("tenant-a")
        context = self.store.authenticate_token(raw)
        assert context is not None
        api = AgentHubApi(self.store)
        with self.assertRaises(ApiError):
            api.post(
                "/v1/hub/nodes",
                self._node_payload("tenant-a", "node-sibling", "pi-tenant-a"),
                context,
            )

    def test_node_token_can_refresh_its_own_node(self) -> None:
        raw = self._token("tenant-a")
        context = self.store.authenticate_token(raw)
        assert context is not None
        api = AgentHubApi(self.store)
        status, payload = api.post(
            "/v1/hub/nodes",
            self._node_payload("tenant-a", "node-tenant-a", "pi-tenant-a"),
            context,
        )
        self.assertEqual(int(status), 200)
        self.assertEqual(payload["node"]["node_id"], "node-tenant-a")


class NodeTaskScopeTests(Batch2Fixture):
    def _submit_and_claim(self) -> dict:
        task, _ = self.store.create_task(
            TaskSubmission(
                principal_id="owner-tenant-a",
                delegator_actor_id="pi-tenant-a",
                objective="test task",
                assignee_actor_id=None,
                context_id=None,
                idempotency_key=None,
                required_capabilities=(),
                input={},
                metadata={},
                origin="local_ui",
            ),
            tenant_id="tenant-a",
        )
        claimed = self.store.claim_task(
            actor_id="pi-tenant-a",
            node_id="node-tenant-a",
            wait_seconds=0,
            lease_seconds=120,
        )
        assert claimed is not None
        return claimed["task"]

    def _node_token(self, node_id: str) -> str:
        actor = "pi-tenant-a" if node_id == "node-tenant-a" else "pi-sibling"
        raw, _ = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="tenant-a",
                role="node",
                principal_id=None,
                actor_id=actor,
                node_id=node_id,
                label="node",
                expires_at=None,
            )
        )
        return raw

    def test_node_token_cannot_cancel_another_nodes_task(self) -> None:
        self.store.register_actor(
            ActorRegistration(
                actor_id="pi-sibling",
                principal_id="owner-tenant-a",
                kind="agent",
                display_name="Sibling",
                capabilities=("code",),
                metadata={},
            ),
            tenant_id="tenant-a",
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-sibling",
                actor_id="pi-sibling",
                display_name="Sibling node",
                capabilities=("code",),
                metadata={},
            ),
            tenant_id="tenant-a",
        )
        task = self._submit_and_claim()
        raw = self._node_token("node-sibling")
        context = self.store.authenticate_token(raw)
        assert context is not None
        api = AgentHubApi(self.store)
        with self.assertRaises(ApiError) as raised:
            api.post(
                f"/v1/hub/tasks/{task['task_id']}/cancel",
                {"actor_id": "pi-sibling"},
                context,
            )
        self.assertEqual(int(raised.exception.status), 409)

    def test_node_token_can_cancel_its_own_task(self) -> None:
        task = self._submit_and_claim()
        raw = self._node_token("node-tenant-a")
        context = self.store.authenticate_token(raw)
        assert context is not None
        api = AgentHubApi(self.store)
        status, payload = api.post(
            f"/v1/hub/tasks/{task['task_id']}/cancel",
            {"actor_id": "pi-tenant-a", "reason": "test"},
            context,
        )
        self.assertEqual(int(status), 200)
        self.assertEqual(payload["task"]["status"], "cancelled")


class TunnelTicketBoundsTests(unittest.TestCase):
    def test_pending_tickets_per_node_are_capped(self) -> None:
        registry = TunnelRegistry()
        for _ in range(20):
            registry.issue_ticket("node-a")
        self.assertLessEqual(registry.pending_ticket_count, 8)
        for _ in range(20):
            registry.issue_ticket("node-b")
        self.assertLessEqual(registry.pending_ticket_count, 16)

    def test_expired_tickets_are_pruned(self) -> None:
        registry = TunnelRegistry(ticket_ttl_seconds=0)
        registry.issue_ticket("node-a")
        registry.issue_ticket("node-a")
        self.assertEqual(registry.pending_ticket_count, 1)


class RegistrationHardeningTests(Batch2Fixture):
    def test_duplicate_registration_keeps_tenant_metadata(self) -> None:
        self.store.register_user_personal(
            username="alice", password="correct-horse1", display_name="Alice"
        )
        before = self.store.get_tenant("user-alice")
        with self.assertRaises(ValueError):
            self.store.register_user_personal(
                username="alice",
                password="correct-horse1",
                display_name="Different display",
            )
        after = self.store.get_tenant("user-alice")
        self.assertEqual(before["display_name"], after["display_name"])
        self.assertEqual(before["metadata"], after["metadata"])

    def test_reserved_tenant_cannot_be_claimed_by_registration(self) -> None:
        self.store.create_tenant(
            TenantRegistration(
                tenant_id="user-bob",
                display_name="Bob team workspace",
                metadata={"kind": "team"},
            )
        )
        with self.assertRaises(PermissionError):
            self.store.register_user_personal(
                username="bob", password="correct-horse1", display_name="Bob"
            )
        tenant = self.store.get_tenant("user-bob")
        self.assertEqual(tenant["display_name"], "Bob team workspace")

    def test_normal_registration_still_works(self) -> None:
        account = self.store.register_user_personal(
            username="carol", password="correct-horse1", display_name="Carol"
        )
        self.assertEqual(account["username"], "carol")
        self.assertEqual(
            self.store.get_tenant("user-carol")["metadata"]["kind"], "personal"
        )


class LoginLockoutTests(Batch2Fixture):
    def test_expired_lockout_resets_attempt_budget(self) -> None:
        self.store.register_user(
            username="dave",
            password="correct-horse1",
            display_name="Dave",
            tenant_id="tenant-a",
        )
        for _ in range(5):
            self.assertIsNone(
                self.store.authenticate_password(
                    username="dave", password="wrong-password1"
                )
            )
        account = self.store.account_for_principal(
            self.store._account("dave")["principal_id"]
        )
        assert account is not None
        self.assertIsNotNone(account["locked_until"])
        # Simulate the lock having expired while failed_attempts stays at 5.
        with self.store._connection:
            self.store._connection.execute(
                "UPDATE hub_user_accounts SET locked_until=? WHERE username=?",
                (1.0, "dave"),
            )
        self.assertIsNone(
            self.store.authenticate_password(
                username="dave", password="still-wrong1"
            )
        )
        row = self.store._account("dave")
        self.assertEqual(int(row["failed_attempts"]), 1)
        self.assertIsNone(row["locked_until"])
        self.assertIsNotNone(
            self.store.authenticate_password(
                username="dave", password="correct-horse1"
            )
        )

    def test_password_length_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "1024"):
            validate_password("a1" + "x" * 1024)


class SseSubscriberCapTests(Batch2Fixture):
    def test_subscribers_are_capped(self) -> None:
        api = AgentHubApi(self.store)
        server = HubHttpServer(
            ("127.0.0.1", 0), api, "sse-cap-test-token-123456789"
        )
        try:
            queues = []
            for _ in range(MAX_SSE_SUBSCRIBERS):
                queue = server.subscribe("node-tenant-a", "tenant-a")
                self.assertIsNotNone(queue)
                queues.append(queue)
            self.assertIsNone(server.subscribe("node-tenant-a", "tenant-a"))
            for queue in queues:
                server.unsubscribe(queue)  # type: ignore[arg-type]
            self.assertIsNotNone(server.subscribe("node-tenant-a", "tenant-a"))
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
