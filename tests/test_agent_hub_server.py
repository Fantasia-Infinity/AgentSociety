from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.config import HubSettings
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
        self.assertEqual(settings.state_db, Path("hub-state.sqlite3"))
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
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()


if __name__ == "__main__":
    unittest.main()
