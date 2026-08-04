from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

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
from agent_hub.store import AgentHubStore


class MultiTenantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._temporary.name) / "hub.sqlite3")
        self.store.create_tenant(
            TenantRegistration(
                tenant_id="tenant-a",
                display_name="Tenant A",
                metadata={},
            )
        )
        self.store.create_tenant(
            TenantRegistration(
                tenant_id="tenant-b",
                display_name="Tenant B",
                metadata={},
            )
        )
        for tenant in ("tenant-a", "tenant-b"):
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
                    metadata={},
                ),
                tenant_id=tenant,
            )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_tenants_and_token_lifecycle(self) -> None:
        raw, record = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="tenant-a",
                role="tenant_admin",
                principal_id="owner-tenant-a",
                actor_id=None,
                node_id=None,
                label="admin token",
                expires_at=None,
            )
        )
        self.assertEqual(record["tenant_id"], "tenant-a")
        context = self.store.authenticate_token(raw)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.role, "tenant_admin")
        self.assertEqual(context.tenant_id, "tenant-a")

        self.store.revoke_auth_token(record["token_id"], tenant_id="tenant-a")
        self.assertIsNone(self.store.authenticate_token(raw))

    def test_node_token_maps_to_node(self) -> None:
        raw, record = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="tenant-a",
                role="node",
                principal_id=None,
                actor_id="pi-tenant-a",
                node_id="node-tenant-a",
                label="node token",
                expires_at=None,
            )
        )
        self.assertEqual(record["role"], "node")
        context = self.store.authenticate_token(raw)
        assert context is not None
        self.assertEqual(context.actor_id, "pi-tenant-a")
        self.assertEqual(context.node_id, "node-tenant-a")

    def test_tenant_scoped_lists_and_task_visibility(self) -> None:
        for tenant in ("tenant-a", "tenant-b"):
            self.store.create_task(
                TaskSubmission(
                    principal_id=f"owner-{tenant}",
                    delegator_actor_id=f"pi-{tenant}",
                    objective=f"task for {tenant}",
                    assignee_actor_id=f"pi-{tenant}",
                    context_id=None,
                    idempotency_key=None,
                    required_capabilities=("code",),
                    input={},
                    metadata={},
                    origin="test",
                ),
                tenant_id=tenant,
            )

        tasks_a = self.store.list_tasks(tenant_id="tenant-a")
        tasks_b = self.store.list_tasks(tenant_id="tenant-b")
        self.assertEqual(len(tasks_a), 1)
        self.assertEqual(len(tasks_b), 1)
        self.assertIn("tenant-a", tasks_a[0]["objective"])
        self.assertIn("tenant-b", tasks_b[0]["objective"])

        with self.assertRaises(LookupError):
            self.store.get_task(tasks_a[0]["task_id"], tenant_id="tenant-b")

    def test_claim_is_scoped_to_tenant(self) -> None:
        self.store.create_task(
            TaskSubmission(
                principal_id="owner-tenant-a",
                delegator_actor_id="pi-tenant-a",
                objective="claim me",
                assignee_actor_id="pi-tenant-a",
                context_id=None,
                idempotency_key=None,
                required_capabilities=("code",),
                input={},
                metadata={},
                origin="test",
            ),
            tenant_id="tenant-a",
        )
        claim_b = self.store.claim_task(
            actor_id="pi-tenant-b",
            node_id="node-tenant-b",
            wait_seconds=0,
            tenant_id="tenant-b",
        )
        self.assertIsNone(claim_b)
        claim_a = self.store.claim_task(
            actor_id="pi-tenant-a",
            node_id="node-tenant-a",
            wait_seconds=0,
            tenant_id="tenant-a",
        )
        self.assertIsNotNone(claim_a)
        self.assertEqual(claim_a["task"]["tenant_id"], "tenant-a")

    def test_oidc_identity_mapping(self) -> None:
        self.store.register_oidc_identity(
            provider="https://issuer.example",
            subject="user-1",
            tenant_id="tenant-a",
            principal_id="owner-tenant-a",
            role="tenant_user",
        )
        context = self.store.authenticate_oidc(
            provider="https://issuer.example",
            subject="user-1",
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.tenant_id, "tenant-a")
        self.assertEqual(context.principal_id, "owner-tenant-a")


class MultiTenantApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._temporary.name) / "hub.sqlite3")
        self.api = AgentHubApi(self.store)
        self.admin = AuthenticatedContext(role="admin")
        self.store.create_tenant(
            TenantRegistration(tenant_id="tenant-a", display_name="A", metadata={})
        )
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="owner-a",
                kind="human",
                display_name="Owner A",
                metadata={},
            ),
            tenant_id="tenant-a",
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="pi-a",
                principal_id="owner-a",
                kind="agent",
                display_name="Pi A",
                capabilities=("code",),
                metadata={},
            ),
            tenant_id="tenant-a",
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_admin_creates_tenant_token_and_tenant_context_is_scoped(self) -> None:
        _, response = self.api.post(
            "/v1/hub/tenants/tenant-a/tokens",
            {
                "role": "tenant_admin",
                "principal_id": "owner-a",
                "label": "a-admin",
            },
            self.admin,
        )
        raw = response["raw_token"]
        tenant_context = self.store.authenticate_token(raw)
        self.assertIsNotNone(tenant_context)
        assert tenant_context is not None

        _, task_response = self.api.post(
            "/v1/hub/tasks",
            {
                "principal_id": "owner-a",
                "delegator_actor_id": "pi-a",
                "objective": "tenant task",
                "required_capabilities": ["code"],
            },
            tenant_context,
        )
        task_id = task_response["task"]["task_id"]

        status, listed = self.api.get("/v1/hub/tasks", "", tenant_context)
        self.assertEqual(status.value, 200)
        self.assertEqual([t["task_id"] for t in listed["tasks"]], [task_id])

        other_tenant = AuthenticatedContext(
            role="tenant_user", tenant_id="tenant-b"
        )
        with self.assertRaises(ApiError):
            self.api.get(f"/v1/hub/tasks/{task_id}", "", other_tenant)
        with self.assertRaises(ApiError):
            self.api.post("/v1/hub/tenants", {"tenant_id": "x"}, tenant_context)


if __name__ == "__main__":
    unittest.main()
