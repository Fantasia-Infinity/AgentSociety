from __future__ import annotations

import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.config import HubSettings
from agent_hub.ratelimit import AuthRateLimiter
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore


class HubSettingsTests(unittest.TestCase):
    def load(self, values: dict[str, str]) -> HubSettings:
        with (
            patch("agent_hub.config._load_env_file"),
            patch.dict(os.environ, values, clear=True),
        ):
            return HubSettings.from_env()

    def test_defaults_to_independent_loopback_service(self) -> None:
        settings = self.load(
            {"AGENT_HUB_TOKEN": "a-secure-test-token-123456789"}
        )
        self.assertEqual(settings.api_host, "127.0.0.1")
        self.assertEqual(settings.api_port, 8090)
        self.assertEqual(settings.state_db, Path(".private/state/hub-state.sqlite3"))
        self.assertFalse(settings.allow_non_loopback_bind)

    def test_rejects_short_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 24"):
            self.load({"AGENT_HUB_TOKEN": "short"})

    def test_rejects_public_bind_without_explicit_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be loopback"):
            self.load(
                {
                    "AGENT_HUB_TOKEN": "a-secure-test-token-123456789",
                    "AGENT_HUB_HOST": "0.0.0.0",
                }
            )

    def test_allows_container_bind_with_explicit_override(self) -> None:
        settings = self.load(
            {
                "AGENT_HUB_TOKEN": "a-secure-test-token-123456789",
                "AGENT_HUB_HOST": "0.0.0.0",
                "AGENT_HUB_ALLOW_NON_LOOPBACK_BIND": "true",
            }
        )
        self.assertEqual(settings.api_host, "0.0.0.0")
        self.assertTrue(settings.allow_non_loopback_bind)


class HubHttpServerTests(unittest.TestCase):
    def _serve(self, store, limiter=None, web_secret=None):
        server = HubHttpServer(
            ("127.0.0.1", 0),
            AgentHubApi(store),
            "standalone-hub-token-123456789",
            web_secret=web_secret,
            rate_limiter=limiter,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_health_is_public_but_hub_api_requires_its_own_token(self) -> None:
        with TemporaryDirectory() as temporary:
            store = AgentHubStore(Path(temporary) / "hub.sqlite3")
            server = HubHttpServer(
                ("127.0.0.1", 0),
                AgentHubApi(store),
                "standalone-hub-token-123456789",
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"http://127.0.0.1:{port}/v1/hub/actors", timeout=2)
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
                request = Request(
                    f"http://127.0.0.1:{port}/v1/hub/actors",
                    headers={
                        "Authorization": "Bearer standalone-hub-token-123456789"
                    },
                )
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(
                    f"http://127.0.0.1:{port}/.well-known/agent-card.json",
                    timeout=2,
                ) as response:
                    card = json.load(response)
                    self.assertEqual(
                        card["supportedInterfaces"][0]["protocolVersion"], "1.0"
                    )
                    self.assertFalse(card["capabilities"]["streaming"])
                a2a = Request(
                    f"http://127.0.0.1:{port}/a2a",
                    data=json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "SendMessage",
                            "params": {
                                "message": {
                                    "messageId": "http-a2a-1",
                                    "role": "ROLE_USER",
                                    "parts": [{"text": "Run tests"}],
                                }
                            },
                        }
                    ).encode(),
                    headers={
                        "Authorization": "Bearer standalone-hub-token-123456789",
                        "Content-Type": "application/json",
                        "A2A-Version": "1.0",
                    },
                    method="POST",
                )
                with urlopen(a2a, timeout=2) as response:
                    result = json.load(response)
                    self.assertEqual(
                        result["result"]["task"]["status"]["state"],
                        "TASK_STATE_SUBMITTED",
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()

    def test_security_headers_on_html_and_json(self) -> None:
        with TemporaryDirectory() as temporary:
            store = AgentHubStore(Path(temporary) / "hub.sqlite3")
            server, thread = self._serve(
                store, web_secret="a-secure-web-secret-1234567890-abcdef"
            )
            port = server.server_address[1]
            try:
                with urlopen(f"http://127.0.0.1:{port}/web/login", timeout=2) as response:
                    self.assertEqual(
                        response.headers.get("X-Content-Type-Options"), "nosniff"
                    )
                    self.assertEqual(
                        response.headers.get("X-Frame-Options"), "DENY"
                    )
                    self.assertIn(
                        "frame-ancestors 'none'",
                        response.headers.get("Content-Security-Policy", ""),
                    )
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    self.assertEqual(
                        response.headers.get("X-Content-Type-Options"), "nosniff"
                    )
                    self.assertEqual(
                        response.headers.get("Referrer-Policy"), "no-referrer"
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()

    def test_auth_endpoints_are_rate_limited(self) -> None:
        with TemporaryDirectory() as temporary:
            store = AgentHubStore(Path(temporary) / "hub.sqlite3")
            server, thread = self._serve(
                store,
                AuthRateLimiter(auth_per_minute=2, register_per_hour=10),
            )
            port = server.server_address[1]
            try:
                body = json.dumps(
                    {"username": "alice", "password": "wrong-password-1"}
                ).encode()
                for expected in (401, 401, 429):
                    request = Request(
                        f"http://127.0.0.1:{port}/v1/auth/login",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        with urlopen(request, timeout=2) as response:
                            self.assertEqual(response.status, expected)
                    except HTTPError as raised:
                        self.assertEqual(raised.code, expected)
                        raised.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()


if __name__ == "__main__":
    unittest.main()
