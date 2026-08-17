from __future__ import annotations

from http.cookiejar import CookieJar
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from agent_hub.api import AgentHubApi
from agent_hub.domain import (
    ActorRegistration,
    NodeRegistration,
    PrincipalRegistration,
    SharedEventAppend,
)
from agent_hub.mcp import McpService
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore

API_TOKEN = "standalone-hub-token-123456789"
WEB_SECRET = "w" * 40


def _mcp_call(service, method, params, *, context=None, request_id=1):
    response = service.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
        context,
    )
    assert response is not None
    return response


class WebAnswerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="principal-a",
                kind="human",
                display_name="A",
                metadata={},
            )
        )
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="principal-b",
                kind="human",
                display_name="B",
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-asker",
                principal_id="principal-a",
                kind="agent",
                display_name="Asker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-target",
                principal_id="principal-a",
                kind="agent",
                display_name="Target",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-target",
                actor_id="actor-target",
                display_name="Target node",
                capabilities=("code",),
                metadata={},
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _ask(self) -> dict:
        return self.store.create_question(
            tenant_id="default",
            principal_id="principal-a",
            asker_actor_id="actor-asker",
            asker_task_id=None,
            asker_session_id="session-asker",
            target_actor_id="actor-target",
            message="What is the answer?",
            require="answer",
        )

    def test_web_answer_on_pending_writes_shared_event(self) -> None:
        question = self._ask()
        answered = self.store.answer_question_web(
            question["question_id"],
            actor_id=None,
            answer_text=" 42 from human ",
            tenant_id="default",
            principal_id="principal-a",
        )
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answer_text"], "42 from human")
        qa = self.store.list_shared_events(tenant_id="default", scope="qa")
        self.assertEqual(len(qa), 1)
        self.assertEqual(qa[0]["payload"]["answer"], "42 from human")

    def test_web_answer_rejects_claimed(self) -> None:
        question = self._ask()
        with self.store._condition, self.store._connection:
            self.store._connection.execute(
                "UPDATE hub_questions SET status='claimed' WHERE question_id=?",
                (question["question_id"],),
            )
        with self.assertRaises(PermissionError):
            self.store.answer_question_web(
                question["question_id"],
                actor_id=None,
                answer_text="too late",
                tenant_id="default",
                principal_id="principal-a",
            )

    def test_web_answer_rejects_other_principal(self) -> None:
        question = self._ask()
        with self.assertRaises(PermissionError):
            self.store.answer_question_web(
                question["question_id"],
                actor_id=None,
                answer_text="nope",
                tenant_id="default",
                principal_id="principal-b",
            )

    def test_web_decline_flow(self) -> None:
        question = self._ask()
        declined = self.store.decline_question(
            question["question_id"],
            actor_id=None,
            reason="not needed",
            tenant_id="default",
            principal_id="principal-a",
        )
        self.assertEqual(declined["status"], "declined")
        qa = self.store.list_shared_events(tenant_id="default", scope="qa")
        self.assertEqual(len(qa), 1)
        self.assertEqual(qa[0]["payload"]["status"], "declined")
        self.assertEqual(qa[0]["payload"]["reason"], "not needed")

    def test_mcp_hub_ask_stops_on_declined(self) -> None:
        from agent_hub.domain import AuthTokenCreation

        self.store.register_node(
            NodeRegistration(
                node_id="node-asker",
                actor_id="actor-asker",
                display_name="Asker node",
                capabilities=("code",),
                metadata={},
            )
        )
        raw, _ = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="default",
                role="node",
                principal_id="principal-a",
                actor_id="actor-asker",
                node_id="node-asker",
                label="asker-token",
                expires_at=None,
            )
        )
        api = AgentHubApi(self.store)
        service = McpService(api)
        context = api.authenticate(raw)
        assert context is not None

        question = self._ask()
        self.store.decline_question(
            question["question_id"],
            actor_id=None,
            reason="no",
            tenant_id="default",
            principal_id="principal-a",
        )
        result = _mcp_call(
            service,
            "tools/call",
            {
                "name": "hub_ask",
                "arguments": {
                    "question_id": question["question_id"],
                    "target_actor_id": "actor-target",
                    "message": "blocking question",
                    "wait_seconds": 1,
                },
            },
            context=context,
        )
        answer = json.loads(result["result"]["content"][0]["text"])["answer"]
        self.assertEqual(answer["status"], "declined")


class WebAdaptationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
        self.store.register_principal(
            PrincipalRegistration(
                principal_id="human-owner",
                kind="human",
                display_name="Owner",
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-asker",
                principal_id="human-owner",
                kind="agent",
                display_name="Asker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-target",
                principal_id="human-owner",
                kind="agent",
                display_name="Target",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-target",
                actor_id="actor-target",
                display_name="Target node",
                capabilities=("code",),
                metadata={},
            )
        )
        self.api = AgentHubApi(self.store)
        self.server = HubHttpServer(
            ("127.0.0.1", 0),
            self.api,
            API_TOKEN,
            web_secret=WEB_SECRET,
            web_cookie_secure=False,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temporary.cleanup()

    def _post(self, path: str, data: dict[str, str], opener=None) -> str:
        body = "&".join(
            f"{key}={quote(value)}" for key, value in data.items()
        ).encode()
        request = Request(
            f"{self.base}{path}",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with (opener or self.opener).open(request, timeout=5) as response:
            return response.read().decode()

    def _get(self, path: str, opener=None) -> str:
        with (opener or self.opener).open(f"{self.base}{path}", timeout=5) as response:
            return response.read().decode()

    def _login(self) -> None:
        # The test server keeps the bootstrap token enabled; the web session
        # accepts it directly (same flow as the existing web UI tests).
        self._post("/web/login", {"token": API_TOKEN})

    def _seed_data(self) -> None:
        self.store.create_question(
            tenant_id="default",
            principal_id="human-owner",
            asker_actor_id="actor-asker",
            asker_task_id=None,
            asker_session_id="session-1",
            target_actor_id="actor-target",
            message="Is the pipeline green?",
            require="status",
        )
        self.store.append_shared_event(
            SharedEventAppend(
                scope="consensus",
                kind="digest",
                payload={"summary": "built and verified"},
                principal_id="human-owner",
                session_id="session-1",
                actor_id="actor-asker",
                node_id="node-target",
                ttl_hours=720,
                event_id="web-digest-1",
            )
        )
        self.store.upsert_directory_row(
            tenant_id="default",
            principal_id="human-owner",
            session_id="session-1",
            actor_id="actor-asker",
            node_id="node-target",
            row={
                "title": "Pipeline session",
                "workspace": "/repo",
                "status": "idle",
                "last_active_at": 1_700_000_000_000,
                "session_mode": "per_task",
                "tool_policy": "full",
                "invocations": [],
            },
        )

    def test_new_pages_render_with_data(self) -> None:
        self._login()
        self._seed_data()
        questions_html = self._get("/web/questions")
        self.assertIn("Questions", questions_html)
        self.assertIn("Is the pipeline green?", questions_html)
        self.assertIn('action="/web/questions/', questions_html)

        contexts_html = self._get("/web/contexts")
        self.assertIn("Consensus", contexts_html)
        self.assertIn("built and verified", contexts_html)

        directory_html = self._get("/web/directory")
        self.assertIn("Pipeline session", directory_html)
        self.assertIn('/web/directory/session-1', directory_html)

        detail_html = self._get("/web/directory/session-1")
        self.assertIn("Tool policy", detail_html)
        self.assertIn("/repo", detail_html)

    def test_navigation_has_new_entries(self) -> None:
        self._login()
        html = self._get("/web")
        for entry in ("/web/questions", "/web/contexts", "/web/directory"):
            self.assertIn(entry, html)

    def test_web_answer_flow_reaches_the_asker(self) -> None:
        self._login()
        question = self.store.create_question(
            tenant_id="default",
            principal_id="human-owner",
            asker_actor_id="actor-asker",
            asker_task_id=None,
            asker_session_id="session-1",
            target_actor_id="actor-target",
            message="Blocking question?",
            require="answer",
        )
        html = self._get("/web/questions")
        csrf_match = html.split('name="csrf_token" value="')[1].split('"')[0]
        self._post(
            f"/web/questions/{question['question_id']}/answer",
            {"csrf_token": csrf_match, "answer_text": "yes, blocking answered"},
        )
        current = self.store.get_question(
            question["question_id"], tenant_id="default"
        )
        self.assertEqual(current["status"], "answered")
        self.assertEqual(current["answer_text"], "yes, blocking answered")

    def test_web_decline_flow(self) -> None:
        self._login()
        question = self.store.create_question(
            tenant_id="default",
            principal_id="human-owner",
            asker_actor_id="actor-asker",
            asker_task_id=None,
            asker_session_id="session-1",
            target_actor_id="actor-target",
            message="Decline me",
            require="answer",
        )
        html = self._get("/web/questions")
        csrf_match = html.split('name="csrf_token" value="')[1].split('"')[0]
        self._post(
            f"/web/questions/{question['question_id']}/decline",
            {"csrf_token": csrf_match, "reason": "not needed"},
        )
        current = self.store.get_question(
            question["question_id"], tenant_id="default"
        )
        self.assertEqual(current["status"], "declined")

    def test_web_post_requires_csrf(self) -> None:
        from urllib.error import HTTPError

        self._login()
        question = self.store.create_question(
            tenant_id="default",
            principal_id="human-owner",
            asker_actor_id="actor-asker",
            asker_task_id=None,
            asker_session_id="session-1",
            target_actor_id="actor-target",
            message="CSRF me",
            require="answer",
        )
        with self.assertRaises(HTTPError) as raised:
            self._post(
                f"/web/questions/{question['question_id']}/answer",
                {"answer_text": "no token"},
            )
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        current = self.store.get_question(
            question["question_id"], tenant_id="default"
        )
        self.assertEqual(current["status"], "pending")


if __name__ == "__main__":
    unittest.main()
