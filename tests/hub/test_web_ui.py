from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from unittest.mock import patch

from agent_hub.api import AgentHubApi
from agent_hub.config import HubSettings
from agent_hub.domain import ActorRegistration, PrincipalRegistration
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore
from agent_hub.web import WebSession, WebSessionError
from agent_hub.web.pages import login_page, task_detail_page, tasks_page


class WebSessionTests(unittest.TestCase):
    def test_create_verify_and_csrf_roundtrip(self) -> None:
        session = WebSession("s" * 40)
        session_id, cookie = session.create()
        verified_id, claims = session.verify(cookie)
        self.assertEqual(verified_id, session_id)
        self.assertEqual(claims["role"], "admin")
        self.assertEqual(len(session.csrf(session_id)), 64)

    def test_rejects_tampered_cookie(self) -> None:
        session = WebSession("s" * 40)
        _, cookie = session.create()
        tampered = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
        with self.assertRaises(WebSessionError):
            session.verify(tampered)

    def test_rejects_expired_session(self) -> None:
        session = WebSession("s" * 40, ttl_seconds=-1)
        _, cookie = session.create()
        with self.assertRaises(WebSessionError):
            session.verify(cookie)

    def test_cookie_attributes(self) -> None:
        session = WebSession("s" * 40)
        cookie = session.set_cookie("value", secure=True)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        cleared = session.clear_cookie(secure=False)
        self.assertIn("Max-Age=0", cleared)
        self.assertNotIn("Secure", cleared)


class WebPageTests(unittest.TestCase):
    def test_login_page_does_not_leak_token(self) -> None:
        html = login_page()
        self.assertIn("Hub login", html)
        self.assertNotIn("Bearer ", html)

    def test_pages_escape_user_content(self) -> None:
        task = {
            "task_id": "task_1",
            "context_id": None,
            "principal_id": "p",
            "delegator_actor_id": "a",
            "assignee_actor_id": None,
            "executor_actor_id": None,
            "executor_node_id": None,
            "objective": "<script>alert(1)</script>",
            "required_capabilities": ["code"],
            "input": {"workspace": "x"},
            "metadata": {},
            "origin": "hub",
            "status": "submitted",
            "result": {},
            "error": None,
            "lease_until": 0,
            "lease_seconds": 120,
            "attempts": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "completed_at": None,
            "artifacts": [],
        }
        html = task_detail_page(task, events=[], runs=[], csrf="c" * 64)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_tasks_page_requires_fields(self) -> None:
        html = tasks_page(
            [],
            status_filter=None,
            principals=[],
            actors=[],
            csrf="c" * 64,
        )
        self.assertIn("Create task", html)
        self.assertIn('name="csrf_token"', html)


class WebUiIntegrationTests(unittest.TestCase):
    def test_login_dashboard_create_task_and_csrf(self) -> None:
        try:
            with TemporaryDirectory() as temporary:
                store = AgentHubStore(Path(temporary) / "hub.sqlite3")
                server = HubHttpServer(
                    ("127.0.0.1", 0),
                    AgentHubApi(store),
                    "standalone-hub-token-123456789",
                    web_secret="w" * 40,
                    web_cookie_secure=False,
                )
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                port = server.server_address[1]
                base = f"http://127.0.0.1:{port}"
                try:
                    store.register_principal(
                        PrincipalRegistration(
                            principal_id="human-owner",
                            kind="human",
                            display_name="Owner",
                            metadata={},
                        )
                    )
                    store.register_actor(
                        ActorRegistration(
                            actor_id="pi-test",
                            principal_id="human-owner",
                            kind="agent",
                            display_name="Pi Test",
                            capabilities=("code",),
                            metadata={},
                        )
                    )
                    opener = build_opener(HTTPCookieProcessor(CookieJar()))

                    def post(path: str, data: dict[str, str]) -> str:
                        body = "&".join(
                            f"{key}={quote(value)}"
                            for key, value in data.items()
                        ).encode()
                        request = Request(
                            f"{base}{path}",
                            data=body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            method="POST",
                        )
                        with opener.open(request, timeout=2) as response:
                            return response.read().decode()

                    # Unauthenticated /web redirects to login.
                    with urlopen(f"{base}/web/", timeout=2) as response:
                        self.assertIn("/web/login", response.geturl())

                    # Login with the raw bootstrap token.
                    dashboard = post("/web/login", {"token": "standalone-hub-token-123456789"})
                    self.assertIn("Dashboard", dashboard)

                    tasks_html = opener.open(f"{base}/web/tasks", timeout=2).read().decode()
                    match = re.search(
                        r'name="csrf_token" value="([0-9a-f]+)"', tasks_html
                    )
                    self.assertIsNotNone(match)
                    csrf = match.group(1)

                    # CSRF is enforced.
                    with self.assertRaises(Exception):
                        post("/web/tasks/create", {"objective": "x"})

                    created = post(
                        "/web/tasks/create",
                        {
                            "csrf_token": csrf,
                            "principal_id": "human-owner",
                            "delegator_actor_id": "pi-test",
                            "assignee_actor_id": "",
                            "objective": "Run the test suite",
                            "required_capabilities": "code",
                            "input_json": '{"workspace":"."}',
                        },
                    )
                    self.assertIn("Run the test suite", created)

                    detail = opener.open(f"{base}/web/tasks", timeout=2).read().decode()
                    self.assertIn("Run the test suite", detail)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
                    store.close()
        except PermissionError as exc:
            self.skipTest(f"sandbox does not permit binding a loopback socket: {exc}")


class HubSettingsWebTests(unittest.TestCase):
    def load(self, values: dict[str, str]) -> HubSettings:
        with (
            patch("agent_hub.config._load_env_file"),
            patch.dict(os.environ, values, clear=True),
        ):
            return HubSettings.from_env()

    def test_web_secret_is_optional_but_minimum_length_enforced(self) -> None:
        settings = self.load({"AGENT_HUB_TOKEN": "a-secure-test-token-123456789"})
        self.assertIsNone(settings.web_secret)
        with self.assertRaisesRegex(ValueError, "at least 32"):
            self.load(
                {
                    "AGENT_HUB_TOKEN": "a-secure-test-token-123456789",
                    "AGENT_HUB_WEB_SECRET": "short",
                }
            )
        self.assertTrue(
            self.load(
                {
                    "AGENT_HUB_TOKEN": "a-secure-test-token-123456789",
                    "AGENT_HUB_WEB_SECRET": "w" * 40,
                }
            ).web_cookie_secure
        )


if __name__ == "__main__":
    unittest.main()
