from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.errors import ApiError
from agent_hub.store import AgentHubStore


class PasswordAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = AgentHubStore(Path(self._tmp.name) / "state.sqlite3")
        self.api = AgentHubApi(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def post(
        self, path: str, payload: dict, context=None
    ) -> tuple[HTTPStatus, dict]:
        return self.api.post(path, payload, context)

    def test_register_login_agent_login_lifecycle(self) -> None:
        status, res = self.post(
            "/v1/auth/register",
            {
                "username": "alice",
                "password": "correct-horse-123",
                "display_name": "Alice",
            },
        )
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertEqual(res["user"]["role"], "tenant_admin")

        status, res = self.post(
            "/v1/auth/register",
            {
                "username": "bob",
                "password": "correct-horse-456",
                "display_name": "Bob",
            },
        )
        self.assertEqual(res["user"]["role"], "tenant_user")

        with self.assertRaises(ApiError) as caught:
            self.post(
                "/v1/auth/register",
                {
                    "username": "alice",
                    "password": "correct-horse-123",
                    "display_name": "A",
                },
            )
        self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)

        status, res = self.post(
            "/v1/auth/login",
            {"username": "alice", "password": "correct-horse-123", "label": "test"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        session_token = res["session_token"]
        session_ctx = self.store.authenticate_session(session_token)
        self.assertIsNotNone(session_ctx)
        self.assertEqual(session_ctx.principal_id, "human-alice")

        status, res = self.post(
            "/v1/auth/agent-login",
            {
                "username": "alice",
                "password": "correct-horse-123",
                "node_id": "test-node",
                "display_name": "Test Node",
                "capabilities": ["pi", "hub-task"],
                "metadata": {"platform": "test"},
            },
        )
        self.assertEqual(status, HTTPStatus.OK)
        node_token = res["node_token"]
        node_ctx = self.store.authenticate_token(node_token)
        self.assertIsNotNone(node_ctx)
        self.assertEqual(node_ctx.role, "node")
        self.assertEqual(node_ctx.node_id, "test-node")
        self.assertEqual(node_ctx.principal_id, "human-alice")

        status, res = self.api.get("/v1/auth/me", "", session_ctx)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(res["me"]["account"]["username"], "alice")
        self.assertTrue(any(t["node_id"] == "test-node" for t in res["tokens"]))

        token_id = res["tokens"][0]["token_id"]
        status, res = self.post(
            "/v1/auth/tokens/revoke", {"token_id": token_id}, session_ctx
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIsNone(self.store.authenticate_token(node_token))

    def test_wrong_password_lockout(self) -> None:
        self.post(
            "/v1/auth/register",
            {
                "username": "carol",
                "password": "correct-horse-777",
                "display_name": "Carol",
            },
        )
        for _ in range(5):
            with self.assertRaises(ApiError):
                self.post(
                    "/v1/auth/login",
                    {"username": "carol", "password": "wrong-password-1"},
                )
        with self.assertRaises(ApiError):
            self.post(
                "/v1/auth/login",
                {"username": "carol", "password": "correct-horse-777"},
            )

    def test_change_password_revokes_sessions(self) -> None:
        self.post(
            "/v1/auth/register",
            {
                "username": "dave",
                "password": "correct-horse-666",
                "display_name": "Dave",
            },
        )
        _, login = self.post(
            "/v1/auth/login",
            {"username": "dave", "password": "correct-horse-666", "label": "test"},
        )
        ctx = self.store.authenticate_session(login["session_token"])
        _, other = self.post(
            "/v1/auth/login",
            {"username": "dave", "password": "correct-horse-666", "label": "test2"},
        )
        other_ctx = self.store.authenticate_session(other["session_token"])

        status, _ = self.post(
            "/v1/auth/change-password",
            {
                "old_password": "correct-horse-666",
                "new_password": "brand-new-password-1",
            },
            ctx,
        )
        self.assertEqual(status, HTTPStatus.OK)
        # Current session survives; the other session is revoked.
        self.assertIsNotNone(self.store.authenticate_session(login["session_token"]))
        self.assertIsNone(self.store.authenticate_session(other["session_token"]))
        status, _ = self.post(
            "/v1/auth/login",
            {"username": "dave", "password": "brand-new-password-1", "label": "test3"},
        )
        self.assertEqual(status, HTTPStatus.OK)

    def test_registration_can_be_disabled(self) -> None:
        api = AgentHubApi(self.store, allow_registration=False)
        with self.assertRaises(ApiError) as caught:
            api.post(
                "/v1/auth/register",
                {
                    "username": "erin",
                    "password": "correct-horse-555",
                    "display_name": "Erin",
                },
                None,
            )
        self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
