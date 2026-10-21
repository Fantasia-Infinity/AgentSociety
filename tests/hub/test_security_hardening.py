"""Regression tests for the 2026-10-21 Hub security hardening.

Covers:
- node tokens are always bound to a principal (null-principal tokens rejected)
- token listing is tenant/principal scoped for node tokens
- password hashes never leave /v1/auth/me
- session revoke checks ownership before revoking
- artifacts_for_session treats LIKE metacharacters literally
- device tunnel responses are validated (status, CRLF, allowlist)
- GET /mcp and /v1/auth/* require authentication at the HTTP layer
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_hub.api import AgentHubApi
from agent_hub.auth import AuthenticatedContext
from agent_hub.domain import (
    ActorRegistration,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
    TenantRegistration,
)
from agent_hub.errors import ApiError
from agent_hub.server import HubHttpServer, validate_proxy_response
from agent_hub.store import AgentHubStore


class StoreFixture(unittest.TestCase):
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
                    metadata={},
                ),
                tenant_id=tenant,
            )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()


class NodeTokenScopeTests(StoreFixture):
    def _node_token(self, tenant: str) -> tuple[str, dict]:
        return self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id=tenant,
                role="node",
                principal_id=None,
                actor_id=f"pi-{tenant}",
                node_id=f"node-{tenant}",
                label=f"node {tenant}",
                expires_at=None,
            )
        )

    def test_node_token_without_principal_is_bound_to_actors_principal(self) -> None:
        raw, record = self._node_token("tenant-a")
        self.assertEqual(record["principal_id"], "owner-tenant-a")
        context = self.store.authenticate_token(raw)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.role, "node")
        self.assertEqual(context.principal_id, "owner-tenant-a")

    def test_legacy_null_principal_node_token_is_rejected(self) -> None:
        raw, record = self._node_token("tenant-a")
        with self.store._connection:
            self.store._connection.execute(
                "UPDATE hub_auth_tokens SET principal_id=NULL WHERE token_id=?",
                (record["token_id"],),
            )
        self.assertIsNone(self.store.authenticate_token(raw))

    def test_token_listing_for_node_token_is_tenant_and_principal_scoped(self) -> None:
        raw, _ = self._node_token("tenant-a")
        self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="tenant-b",
                role="tenant_admin",
                principal_id="owner-tenant-b",
                actor_id=None,
                node_id=None,
                label="other tenant admin",
                expires_at=None,
            )
        )
        context = self.store.authenticate_token(raw)
        assert context is not None
        api = AgentHubApi(self.store)
        status, payload = api.get("/v1/hub/tokens", "", context)
        self.assertEqual(int(status), 200)
        returned = payload["tokens"]
        self.assertTrue(returned)
        self.assertTrue(all(row["tenant_id"] == "tenant-a" for row in returned))
        self.assertTrue(
            all(row["principal_id"] == "owner-tenant-a" for row in returned)
        )

    def test_principal_scope_fails_closed_for_null_node_token(self) -> None:
        api = AgentHubApi(self.store)
        context = AuthenticatedContext(
            role="node",
            tenant_id="tenant-a",
            principal_id=None,
            actor_id="pi-tenant-a",
            node_id="node-tenant-a",
        )
        with self.assertRaises(ApiError) as raised:
            api.get("/v1/hub/tasks", "", context)
        self.assertEqual(int(raised.exception.status), 409)


class AccountEndpointTests(StoreFixture):
    def _account(self, username: str, password: str = "correct-horse-battery1"):
        account = self.store.register_user_personal(
            username=username, password=password, display_name=username
        )
        raw, _ = self.store.create_session(account)
        context = self.store.authenticate_session(raw)
        assert context is not None
        return account, raw, context

    def test_me_never_returns_password_hash(self) -> None:
        _, _, context = self._account("alice")
        api = AgentHubApi(self.store)
        status, payload = api.get("/v1/auth/me", "", context)
        self.assertEqual(int(status), 200)
        account = payload["me"]["account"]
        self.assertIsNotNone(account)
        self.assertNotIn("password_hash", account)
        self.assertNotIn("password_hash", json.dumps(payload))

    def test_node_token_cannot_read_account_endpoints(self) -> None:
        api = AgentHubApi(self.store)
        raw, _ = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="tenant-a",
                role="node",
                principal_id=None,
                actor_id="pi-tenant-a",
                node_id="node-tenant-a",
                label="node",
                expires_at=None,
            )
        )
        context = self.store.authenticate_token(raw)
        assert context is not None
        with self.assertRaises(ApiError) as raised:
            api.get("/v1/auth/me", "", context)
        self.assertEqual(int(raised.exception.status), 403)

    def test_session_revoke_checks_ownership_before_revoking(self) -> None:
        _, _, context_a = self._account("alice")
        bob_public, _, _ = self._account("bob")
        bob_account = self.store.account_for_principal(bob_public["principal_id"])
        assert bob_account is not None
        _, bob_session = self.store.create_session(bob_account)
        bob_hash = bob_session["session_token_id"]
        api = AgentHubApi(self.store)
        with self.assertRaises(ApiError) as raised:
            api.post(
                "/v1/auth/sessions/revoke",
                {"session_token_id": bob_hash},
                context_a,
            )
        self.assertEqual(int(raised.exception.status), 404)
        self.assertIsNone(self.store.get_session(bob_hash)["revoked_at"])


class ArtifactLikeEscapingTests(StoreFixture):
    def _seed_run_and_artifact(self) -> None:
        with self.store._connection:
            self.store._connection.execute(
                """
                INSERT INTO hub_runs(
                    run_id, task_id, principal_id, actor_id, node_id, origin,
                    objective, status, metadata_json, result_json, error,
                    tenant_id, started_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run-1",
                    None,
                    "owner-tenant-a",
                    "pi-tenant-a",
                    "node-tenant-a",
                    "local_ui",
                    "objective",
                    "completed",
                    "{}",
                    '{"dsh_session_id":"secret-session"}',
                    None,
                    "tenant-a",
                    1.0,
                    1.0,
                    1.0,
                ),
            )
            self.store._connection.execute(
                """
                INSERT INTO hub_artifacts(
                    artifact_id, task_id, run_id, name, media_type, uri,
                    sha256, size_bytes, created_by_actor_id, metadata_json,
                    tenant_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "artifact-1",
                    None,
                    "run-1",
                    "report.txt",
                    "text/plain",
                    "file:///tmp/report.txt",
                    None,
                    1,
                    "pi-tenant-a",
                    "{}",
                    "tenant-a",
                    1.0,
                ),
            )

    def test_wildcard_session_id_does_not_match_other_runs(self) -> None:
        self._seed_run_and_artifact()
        self.assertEqual(
            self.store.artifacts_for_session(
                tenant_id="tenant-a", session_id="%"
            ),
            [],
        )

    def test_exact_session_id_matches(self) -> None:
        self._seed_run_and_artifact()
        rows = self.store.artifacts_for_session(
            tenant_id="tenant-a", session_id="secret-session"
        )
        self.assertEqual([row["artifact_id"] for row in rows], ["artifact-1"])

    def test_principal_filter_excludes_other_principals(self) -> None:
        self._seed_run_and_artifact()
        self.assertEqual(
            self.store.artifacts_for_session(
                tenant_id="tenant-a",
                session_id="secret-session",
                principal_id="owner-tenant-b",
            ),
            [],
        )


class ProxyResponseValidationTests(unittest.TestCase):
    def test_accepts_well_formed_response(self) -> None:
        status, headers, body = validate_proxy_response(
            {
                "status": 200,
                "headers": {"Content-Type": "text/html", "X-Dropped": "no"},
                "body_b64": "aGk=",
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers, [("Content-Type", "text/html")])
        self.assertEqual(body, b"hi")

    def test_rejects_crlf_in_header_value(self) -> None:
        with self.assertRaises(ValueError):
            validate_proxy_response(
                {
                    "status": 200,
                    "headers": {"Content-Type": "text/html\r\nSet-Cookie: x=1"},
                    "body_b64": "",
                }
            )

    def test_rejects_out_of_range_status(self) -> None:
        with self.assertRaises(ValueError):
            validate_proxy_response({"status": 999, "headers": {}, "body_b64": ""})


class HttpLayerAuthTests(StoreFixture):
    def setUp(self) -> None:
        super().setUp()
        self.api = AgentHubApi(self.store, allow_registration=False)
        self.server = HubHttpServer(
            ("127.0.0.1", 0),
            self.api,
            "security-hardening-token-123456789",
            enable_mcp=True,
            mcp_max_streams=1,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _get_status(self, path: str, token: str | None = None) -> int:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = Request(self.base + path, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:  # noqa: S310 - test
                return int(response.status)
        except HTTPError as error:
            return int(error.code)

    def test_mcp_get_requires_authentication(self) -> None:
        self.assertEqual(self._get_status("/mcp"), 401)

    def test_auth_routes_are_reachable_but_require_authentication(self) -> None:
        self.assertEqual(self._get_status("/v1/auth/me"), 401)
        request = Request(self.base + "/v1/auth/logout", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)  # noqa: S310 - test
        self.assertEqual(int(raised.exception.code), 401)

    def test_authenticated_me_is_available_and_hash_free(self) -> None:
        account = self.store.register_user_personal(
            username="carol",
            password="correct-horse-battery1",
            display_name="carol",
        )
        raw, _ = self.store.create_session(account)
        request = Request(
            self.base + "/v1/auth/me",
            headers={"Authorization": f"Bearer {raw}"},
        )
        with urlopen(request, timeout=5) as response:  # noqa: S310 - test
            self.assertEqual(int(response.status), 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertNotIn("password_hash", json.dumps(payload))

    def test_mcp_stream_cap(self) -> None:
        self.assertTrue(self.server.acquire_mcp_stream())
        self.assertFalse(self.server.acquire_mcp_stream())
        self.server.release_mcp_stream()
        self.assertTrue(self.server.acquire_mcp_stream())
        self.server.release_mcp_stream()


if __name__ == "__main__":
    unittest.main()
