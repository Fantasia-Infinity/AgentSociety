from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.auth import AuthenticatedContext
from agent_hub.domain import TenantRegistration
from agent_hub.mcp import McpService
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore


def mcp_call(
    service: McpService,
    method: str,
    params: dict,
    *,
    context: AuthenticatedContext | None = None,
    request_id: int = 1,
) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    response = service.handle_message(payload, context)
    assert response is not None
    return response


class McpServiceTests(unittest.TestCase):
    def make_service(self, temporary: str) -> McpService:
        store = AgentHubStore(Path(temporary) / "hub.sqlite3")
        return McpService(AgentHubApi(store))

    def test_initialize_and_list_tools(self) -> None:
        with TemporaryDirectory() as temporary:
            service = self.make_service(temporary)
            initialized = mcp_call(service, "initialize", {})
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("tools", initialized["result"]["capabilities"])
            listed = mcp_call(service, "tools/list", {})
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertEqual(
                names,
                {
                    "hub_list_actors",
                    "hub_list_nodes",
                    "hub_list_tasks",
                    "hub_get_task",
                    "hub_get_task_events",
                    "hub_create_task",
                    "hub_cancel_task",
                },
            )

    def test_create_get_events_cancel_flow(self) -> None:
        with TemporaryDirectory() as temporary:
            service = self.make_service(temporary)
            admin = AuthenticatedContext(role="admin")
            created = mcp_call(
                service,
                "tools/call",
                {
                    "name": "hub_create_task",
                    "arguments": {
                        "objective": "run the tests",
                        "required_capabilities": ["code"],
                    },
                },
                context=admin,
            )
            self.assertFalse(created["result"]["isError"])
            task = json.loads(created["result"]["content"][0]["text"])["task"]
            self.assertEqual(task["status"], "submitted")
            task_id = task["task_id"]

            fetched = mcp_call(
                service,
                "tools/call",
                {"name": "hub_get_task", "arguments": {"task_id": task_id}},
                context=admin,
            )
            self.assertFalse(fetched["result"]["isError"])

            events = mcp_call(
                service,
                "tools/call",
                {
                    "name": "hub_get_task_events",
                    "arguments": {"task_id": task_id},
                },
                context=admin,
            )
            self.assertFalse(events["result"]["isError"])
            event_list = json.loads(events["result"]["content"][0]["text"])["events"]
            self.assertEqual(event_list[0]["type"], "task.submitted")

            cancelled = mcp_call(
                service,
                "tools/call",
                {
                    "name": "hub_cancel_task",
                    "arguments": {"task_id": task_id, "reason": "test"},
                },
                context=admin,
            )
            self.assertFalse(cancelled["result"]["isError"])
            cancelled_task = json.loads(cancelled["result"]["content"][0]["text"])[
                "task"
            ]
            self.assertEqual(cancelled_task["status"], "cancelled")

    def test_tenant_scope_isolates_data(self) -> None:
        with TemporaryDirectory() as temporary:
            store = AgentHubStore(Path(temporary) / "hub.sqlite3")
            service = McpService(AgentHubApi(store))
            admin = AuthenticatedContext(role="admin")
            tenant_user = AuthenticatedContext(
                role="tenant_user", tenant_id="team-a"
            )
            store.create_tenant(
                TenantRegistration(
                    tenant_id="team-a",
                    display_name="Team A",
                    metadata={},
                )
            )
            created = mcp_call(
                service,
                "tools/call",
                {"name": "hub_create_task", "arguments": {"objective": "team task"}},
                context=tenant_user,
            )
            self.assertFalse(created["result"]["isError"])
            task_id = json.loads(created["result"]["content"][0]["text"])["task"][
                "task_id"
            ]

            wrong_tenant = mcp_call(
                service,
                "tools/call",
                {"name": "hub_get_task", "arguments": {"task_id": task_id}},
                context=AuthenticatedContext(role="tenant_user", tenant_id="other"),
            )
            self.assertTrue(wrong_tenant["result"]["isError"])

            listed = mcp_call(
                service,
                "tools/call",
                {"name": "hub_list_tasks", "arguments": {}},
                context=tenant_user,
            )
            tasks = json.loads(listed["result"]["content"][0]["text"])["tasks"]
            self.assertEqual([task["task_id"] for task in tasks], [task_id])

            admin_sees_nothing_in_default = mcp_call(
                service,
                "tools/call",
                {"name": "hub_list_tasks", "arguments": {"tenant_id": "default"}},
                context=admin,
            )
            default_tasks = json.loads(
                admin_sees_nothing_in_default["result"]["content"][0]["text"]
            )["tasks"]
            self.assertEqual(default_tasks, [])

    def test_unknown_tool_and_invalid_request(self) -> None:
        with TemporaryDirectory() as temporary:
            service = self.make_service(temporary)
            unknown = mcp_call(
                service,
                "tools/call",
                {"name": "hub_nope", "arguments": {}},
                context=AuthenticatedContext(role="admin"),
            )
            self.assertTrue(unknown["result"]["isError"])
            invalid = service.handle_message(
                {"jsonrpc": "1.0", "id": 1, "method": "ping"}, None
            )
            assert invalid is not None
            self.assertEqual(invalid["error"]["code"], -32600)


class McpHttpTests(unittest.TestCase):
    def post_mcp(
        self,
        port: int,
        payload: dict,
        *,
        token: str | None = "mcp-http-token-123456789",
    ) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def start_server(
        self, temporary: str, *, enable_mcp: bool = True
    ) -> tuple[HubHttpServer, int, Thread]:
        store = AgentHubStore(Path(temporary) / "hub.sqlite3")
        server = HubHttpServer(
            ("127.0.0.1", 0),
            AgentHubApi(store),
            "mcp-http-token-123456789",
            enable_mcp=enable_mcp,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, server.server_address[1], thread

    def test_http_requires_auth(self) -> None:
        with TemporaryDirectory() as temporary:
            server, port, _ = self.start_server(temporary)
            try:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        Request(
                            f"http://127.0.0.1:{port}/mcp",
                            data=json.dumps(
                                {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                            ).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        ),
                        timeout=2,
                    )
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
            finally:
                server.server_close()

    def test_http_initialize_and_create_task(self) -> None:
        with TemporaryDirectory() as temporary:
            server, port, _ = self.start_server(temporary)
            try:
                status, initialized = self.post_mcp(
                    port, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")

                status, created = self.post_mcp(
                    port,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "hub_create_task",
                            "arguments": {"objective": "mcp http task"},
                        },
                    },
                )
                self.assertEqual(status, 200)
                self.assertFalse(created["result"]["isError"])
                task = json.loads(created["result"]["content"][0]["text"])["task"]
                self.assertEqual(task["objective"], "mcp http task")
            finally:
                server.server_close()

    def test_endpoint_disabled(self) -> None:
        with TemporaryDirectory() as temporary:
            server, port, _ = self.start_server(temporary, enable_mcp=False)
            try:
                with self.assertRaises(HTTPError) as raised:
                    self.post_mcp(
                        port, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                    )
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
