from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.auth import AuthenticatedContext
from agent_hub.errors import ApiError
from agent_hub.domain import (
    ActorRegistration,
    NodeRegistration,
    NodeWebRegistration,
    PrincipalRegistration,
)
from agent_hub.store import AgentHubStore


class NodeWebRegistrationTests(unittest.TestCase):
    def test_from_dict_validates_enabled_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "enabled"):
            NodeWebRegistration.from_dict({"enabled": "yes"})

    def test_disabled_by_default_when_field_missing(self) -> None:
        item = NodeWebRegistration.from_dict({})
        self.assertFalse(item.enabled)
        self.assertIsNone(item.protocol_version)
        self.assertEqual(item.capabilities, ())

    def test_to_dict_roundtrip_is_canonical(self) -> None:
        item = NodeWebRegistration.from_dict(
            {
                "enabled": True,
                "protocol_version": "1",
                "dsh_version": "0.1.0-rc.5",
                "profile": "agent-society-web",
                "capabilities": ["session.read", "session.read"],
            }
        )
        self.assertEqual(item.capabilities, ("session.read",))
        encoded = item.to_dict()
        self.assertEqual(encoded["enabled"], True)
        self.assertEqual(encoded["protocol_version"], "1")
        self.assertEqual(encoded["capabilities"], ["session.read"])
        self.assertEqual(NodeWebRegistration.from_dict(encoded), item)


class NodeWebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._temporary.name) / "hub.sqlite3")
        self.api = AgentHubApi(self.store)
        self.admin = AuthenticatedContext(role="admin")
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
                actor_id="actor-pi",
                principal_id="principal-owner",
                kind="agent",
                display_name="Pi Agent",
                capabilities=(),
                metadata={},
            )
        )
        self.node_context = AuthenticatedContext(
            role="node",
            tenant_id="default",
            principal_id="principal-owner",
            actor_id="actor-pi",
            node_id="node-mac",
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_registration_with_web_advertises_capability_and_redacted_view(self) -> None:
        _, response = self.api.post(
            "/v1/hub/nodes",
            {
                "node_id": "node-mac",
                "actor_id": "actor-pi",
                "display_name": "Mac",
                "capabilities": ["filesystem"],
                "dsh_web": {
                    "enabled": True,
                    "protocol_version": "1",
                    "dsh_version": "0.1.0-rc.5",
                    "profile": "agent-society-web",
                    "capabilities": ["session.read"],
                },
            },
            self.admin,
        )
        node = response["node"]
        self.assertIn("dsh-web", node["capabilities"])
        web = node["web"]
        self.assertTrue(web["enabled"])
        self.assertEqual(web["protocol_version"], "1")
        self.assertEqual(web["capabilities"], ["session.read"])
        # The stored metadata uses the canonical validated shape.
        stored_web = node["metadata"]["dsh_web"]
        self.assertEqual(stored_web["enabled"], True)
        self.assertEqual(stored_web["capabilities"], ["session.read"])

    def test_registration_without_web_keeps_old_behavior(self) -> None:
        _, response = self.api.post(
            "/v1/hub/nodes",
            {
                "node_id": "node-mac",
                "actor_id": "actor-pi",
                "display_name": "Mac",
                "capabilities": ["filesystem"],
            },
            self.admin,
        )
        node = response["node"]
        self.assertEqual(node["web"], {"enabled": False})
        self.assertNotIn("dsh-web", node["capabilities"])
        self.assertNotIn("dsh_web", node["metadata"])

    def test_registration_rejects_malformed_web(self) -> None:
        with self.assertRaises(ApiError):
            self.api.post(
                "/v1/hub/nodes",
                {
                    "node_id": "node-mac",
                    "actor_id": "actor-pi",
                    "display_name": "Mac",
                    "dsh_web": {"enabled": "yes"},
                },
                self.admin,
            )

    def test_node_can_update_own_web_capability(self) -> None:
        self.store.register_node(
            NodeRegistration(
                node_id="node-mac",
                actor_id="actor-pi",
                display_name="Mac",
                capabilities=("filesystem",),
                metadata={},
            )
        )
        _, response = self.api.post(
            "/v1/hub/nodes/web",
            {
                "node_id": "node-mac",
                "web": {
                    "enabled": True,
                    "protocol_version": "1",
                    "profile": "agent-society-web",
                },
            },
            self.node_context,
        )
        self.assertTrue(response["node"]["web"]["enabled"])

    def test_node_cannot_update_another_nodes_web_capability(self) -> None:
        self.store.register_node(
            NodeRegistration(
                node_id="node-mac",
                actor_id="actor-pi",
                display_name="Mac",
                capabilities=("filesystem",),
                metadata={},
            )
        )
        with self.assertRaises(ApiError):
            self.api.post(
                "/v1/hub/nodes/web",
                {
                    "node_id": "node-other",
                    "web": {"enabled": True},
                },
                self.node_context,
            )

    def test_admin_can_update_any_node_web_capability(self) -> None:
        self.store.register_node(
            NodeRegistration(
                node_id="node-mac",
                actor_id="actor-pi",
                display_name="Mac",
                capabilities=("filesystem",),
                metadata={},
            )
        )
        _, response = self.api.post(
            "/v1/hub/nodes/web",
            {
                "node_id": "node-mac",
                "web": {"enabled": False},
            },
            self.admin,
        )
        self.assertEqual(response["node"]["web"], {"enabled": False})

    def test_list_nodes_returns_redacted_web_views(self) -> None:
        self.store.register_node(
            NodeRegistration(
                node_id="node-mac",
                actor_id="actor-pi",
                display_name="Mac",
                capabilities=("filesystem",),
                metadata={
                    "dsh_web": {
                        "enabled": True,
                        "protocol_version": "1",
                        "capabilities": ["session.read"],
                    }
                },
            )
        )
        _, response = self.api.get("/v1/hub/nodes", "", self.admin)
        node = next(item for item in response["nodes"] if item["node_id"] == "node-mac")
        self.assertTrue(node["web"]["enabled"])
        self.assertEqual(node["web"]["protocol_version"], "1")


if __name__ == "__main__":
    unittest.main()
