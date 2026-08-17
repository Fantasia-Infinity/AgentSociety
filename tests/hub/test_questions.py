from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.domain import (
    ActorRegistration,
    AuthTokenCreation,
    NodeRegistration,
    PrincipalRegistration,
)
from agent_hub.mcp import McpService
from agent_hub.server import HubHttpServer
from agent_hub.store import AgentHubStore

API_TOKEN = "standalone-hub-token-123456789"


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


class QuestionStoreTests(unittest.TestCase):
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
        for actor in ("actor-asker", "actor-answerer"):
            self.store.register_actor(
                ActorRegistration(
                    actor_id=actor,
                    principal_id="principal-a",
                    kind="agent",
                    display_name=actor,
                    capabilities=("code",),
                    metadata={},
                )
            )
        self.store.register_node(
            NodeRegistration(
                node_id="node-answerer",
                actor_id="actor-answerer",
                display_name="Answerer node",
                capabilities=("code",),
                metadata={},
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _ask(self, **overrides) -> dict:
        return self.store.create_question(
            tenant_id=overrides.pop("tenant_id", "default"),
            principal_id=overrides.pop("principal_id", "principal-a"),
            asker_actor_id=overrides.pop("asker_actor_id", "actor-asker"),
            asker_task_id=overrides.pop("asker_task_id", None),
            asker_session_id=overrides.pop("asker_session_id", "session-asker"),
            target_actor_id=overrides.pop("target_actor_id", "actor-answerer"),
            message=overrides.pop("message", "What is the status?"),
            require=overrides.pop("require", "status"),
            **overrides,
        )

    def test_full_lifecycle_with_shared_answer(self) -> None:
        question = self._ask()
        self.assertEqual(question["status"], "pending")
        claimed = self.store.claim_questions(
            actor_id="actor-answerer",
            node_id="node-answerer",
            tenant_id="default",
        )
        self.assertEqual(len(claimed), 1)
        answered = self.store.answer_question(
            question["question_id"],
            lease_token=claimed[0]["lease_token"],
            answer_text="all green",
            tenant_id="default",
        )
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answer_text"], "all green")
        # The answer joined the shared memory (qa scope).
        events = self.store.list_shared_events(
            tenant_id="default", scope="qa"
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "answer")
        self.assertEqual(
            events[0]["payload"]["answer"], "all green"
        )
        self.assertEqual(events[0]["session_id"], "session-asker")

    def test_answer_writes_task_event_when_asker_task_known(self) -> None:
        from agent_hub.domain import TaskSubmission

        task, _ = self.store.create_task(
            TaskSubmission(
                principal_id="principal-a",
                delegator_actor_id="actor-asker",
                objective="Run it",
                assignee_actor_id="actor-asker",
                required_capabilities=("code",),
                input={},
                metadata={},
                origin="hub",
                context_id=None,
                idempotency_key=None,
            )
        )
        question = self._ask(asker_task_id=task["task_id"])
        claimed = self.store.claim_questions(
            actor_id="actor-answerer",
            node_id="node-answerer",
            tenant_id="default",
        )
        self.store.answer_question(
            question["question_id"],
            lease_token=claimed[0]["lease_token"],
            answer_text="42",
            tenant_id="default",
        )
        events = self.store.list_task_events(
            task["task_id"], tenant_id="default"
        )
        self.assertEqual(events[-1]["type"], "question.answered")

    def test_unsupported_when_target_has_no_online_node(self) -> None:
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-ghost",
                principal_id="principal-a",
                kind="agent",
                display_name="Ghost",
                capabilities=(),
                metadata={},
            )
        )
        question = self._ask(target_actor_id="actor-ghost")
        self.assertEqual(question["status"], "unsupported")

    def test_claim_and_answer_enforce_lease_and_actor(self) -> None:
        question = self._ask()
        with self.assertRaises(PermissionError):
            self.store.claim_questions(
                actor_id="actor-asker",
                node_id="node-answerer",
                tenant_id="default",
            )
        claimed = self.store.claim_questions(
            actor_id="actor-answerer",
            node_id="node-answerer",
            tenant_id="default",
        )
        with self.assertRaises(PermissionError):
            self.store.answer_question(
                question["question_id"],
                lease_token="wrong-lease",
                answer_text="x",
                tenant_id="default",
            )

    def test_expiry(self) -> None:
        self._ask()
        with self.store._connection:
            self.store._connection.execute(
                "UPDATE hub_questions SET created_at=?",
                (0,),
            )
        expired = self.store.expire_questions()
        self.assertEqual(expired, 1)
        question = self.store.get_question(
            self.store.list_questions(
                tenant_id="default"
            )[0]["question_id"],
            tenant_id="default",
        )
        self.assertEqual(question["status"], "expired")


class QuestionApiTests(unittest.TestCase):
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
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-a",
                principal_id="principal-a",
                kind="agent",
                display_name="A worker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-a",
                actor_id="actor-a",
                display_name="Node A",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-b",
                principal_id="principal-a",
                kind="agent",
                display_name="B worker",
                capabilities=("code",),
                metadata={},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-b",
                actor_id="actor-b",
                display_name="Node B",
                capabilities=("code",),
                metadata={},
            )
        )
        self.api = AgentHubApi(self.store)
        self.server = HubHttpServer(("127.0.0.1", 0), self.api, API_TOKEN)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temporary.cleanup()

    def _node_token(self, actor: str, node: str, principal: str) -> str:
        raw, _ = self.store.create_auth_token(
            AuthTokenCreation(
                tenant_id="default",
                role="node",
                principal_id=principal,
                actor_id=actor,
                node_id=node,
                label=f"{node}-token",
                expires_at=None,
            )
        )
        return raw

    def _request(self, port, path, *, token=API_TOKEN, payload=None):
        headers = {"Authorization": f"Bearer {token}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        request = Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_node_token_ask_and_answer_flow_with_sse(self) -> None:
        port = self.server.server_address[1]
        asker_token = self._node_token("actor-a", "node-a", "principal-a")
        answerer_token = self._node_token("actor-b", "node-b", "principal-a")

        # The answerer subscribes first: it must receive question/new.
        stream = urlopen(
            Request(
                f"http://127.0.0.1:{port}/v1/hub/events?node_id=node-b",
                headers={"Authorization": f"Bearer {answerer_token}"},
            ),
            timeout=5,
        )
        try:
            while True:
                line = stream.readline()
                if line == b"event: connected\n":
                    stream.readline()
                    stream.readline()
                    break
            question = self._request(
                port,
                "/v1/hub/questions",
                token=asker_token,
                payload={
                    "target_actor_id": "actor-b",
                    "message": "What is the answer?",
                    "require": "answer",
                    "asker_session_id": "session-a-1",
                },
            )["question"]
            self.assertEqual(question["asker_actor_id"], "actor-a")
            self.assertEqual(question["status"], "pending")

            event_name, data = self._next_event(stream)
            self.assertEqual(event_name, "question/new")
            self.assertEqual(data["question_id"], question["question_id"])

            claimed = self._request(
                port,
                "/v1/hub/questions/claim",
                token=answerer_token,
                payload={"actor_id": "actor-b", "node_id": "node-b"},
            )["questions"]
            self.assertEqual(len(claimed), 1)

            answered = self._request(
                port,
                f"/v1/hub/questions/{question['question_id']}/answer",
                token=answerer_token,
                payload={
                    "lease_token": claimed[0]["lease_token"],
                    "answer_text": "the answer is 42",
                },
            )["question"]
            self.assertEqual(answered["status"], "answered")

            # The asker can read its own questions.
            mine = self._request(
                port, "/v1/hub/questions?status=answered", token=asker_token
            )["questions"]
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0]["answer_text"], "the answer is 42")
        finally:
            stream.close()

    def test_mcp_hub_ask_blocks_until_answer(self) -> None:
        service = McpService(self.api)
        asker_token = self._node_token("actor-a", "node-a", "principal-a")
        answerer_token = self._node_token("actor-b", "node-b", "principal-a")

        context = self.api.authenticate(asker_token)
        assert context is not None

        def answer_later() -> None:
            import time

            time.sleep(1.5)
            claimed = self.store.claim_questions(
                actor_id="actor-b",
                node_id="node-b",
                tenant_id="default",
            )
            self.store.answer_question(
                claimed[0]["question_id"],
                lease_token=claimed[0]["lease_token"],
                answer_text="from thread",
                tenant_id="default",
            )

        thread = Thread(target=answer_later, daemon=True)
        thread.start()
        result = _mcp_call(
            service,
            "tools/call",
            {
                "name": "hub_ask",
                "arguments": {
                    "target_actor_id": "actor-b",
                    "message": "blocking question",
                    "wait_seconds": 10,
                },
            },
            context=context,
        )
        answer = json.loads(result["result"]["content"][0]["text"])["answer"]
        self.assertEqual(answer["status"], "answered")
        self.assertEqual(answer["answer"], "from thread")

    def _next_event(self, stream, timeout: float = 5.0):
        import time

        sock = getattr(getattr(stream, "fp", None), "_sock", None)
        if sock is not None:
            sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = stream.readline()
            if not line:
                return None
            if line == b"event: connected\n":
                continue
            if line.startswith(b"event: "):
                name = line[len(b"event: ") :].strip().decode()
                data_line = stream.readline()
                if data_line.startswith(b"data: "):
                    return name, json.loads(data_line[len(b"data: ") :])
        return None


if __name__ == "__main__":
    unittest.main()
