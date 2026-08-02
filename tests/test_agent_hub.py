from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_hub.api import AgentHubApi
from agent_hub.domain import (
    ActorRegistration,
    ArtifactSubmission,
    NodeRegistration,
    PrincipalRegistration,
    RunStatus,
    RunSubmission,
    TaskStatus,
    TaskSubmission,
    TaskUpdate,
)
from agent_hub.store import AgentHubStore


class AgentHubStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = AgentHubStore(Path(self.temporary.name) / "hub.sqlite3")
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
                actor_id="actor-owner",
                principal_id="principal-owner",
                kind="human",
                display_name="Owner console",
                capabilities=(),
                metadata={},
            )
        )
        self.store.register_actor(
            ActorRegistration(
                actor_id="actor-pi",
                principal_id="principal-owner",
                kind="agent",
                display_name="Pi Agent",
                capabilities=("code", "shell"),
                metadata={"runtime": "pi"},
            )
        )
        self.store.register_node(
            NodeRegistration(
                node_id="node-mac",
                actor_id="actor-pi",
                display_name="Mac workstation",
                capabilities=("filesystem",),
                metadata={"platform": "darwin"},
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _submission(self, idempotency_key: str = "request-1") -> TaskSubmission:
        return TaskSubmission(
            principal_id="principal-owner",
            delegator_actor_id="actor-owner",
            assignee_actor_id="actor-pi",
            objective="Inspect the repository and report the tests.",
            context_id="context-1",
            idempotency_key=idempotency_key,
            required_capabilities=("code", "filesystem"),
            input={"repository": "AgentSociety"},
            metadata={},
            origin="local_ui",
        )

    def test_task_lifecycle_creates_run_and_events(self) -> None:
        task, created = self.store.create_task(self._submission())
        self.assertTrue(created)
        self.assertEqual(task["status"], TaskStatus.SUBMITTED.value)

        claim = self.store.claim_task(actor_id="actor-pi", node_id="node-mac")
        assert claim is not None
        self.assertEqual(claim["task"]["status"], TaskStatus.WORKING.value)
        self.assertEqual(claim["run"]["origin"], "remote_task")
        self.assertNotIn("lease_token", claim["task"])

        working = self.store.update_task(
            task["task_id"],
            TaskUpdate(
                run_id=claim["run"]["run_id"],
                lease_token=claim["lease_token"],
                status=TaskStatus.WORKING,
                message="Tests are running",
                result={},
            ),
        )
        self.assertEqual(working["status"], TaskStatus.WORKING.value)

        completed = self.store.update_task(
            task["task_id"],
            TaskUpdate(
                run_id=claim["run"]["run_id"],
                lease_token=claim["lease_token"],
                status=TaskStatus.COMPLETED,
                message="Done",
                result={"summary": "All tests pass"},
            ),
        )
        self.assertEqual(completed["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(completed["result"]["summary"], "All tests pass")
        self.assertEqual(
            self.store.get_run(claim["run"]["run_id"])["status"],
            RunStatus.COMPLETED.value,
        )
        self.assertEqual(
            [event["type"] for event in self.store.list_task_events(task["task_id"])],
            ["task.submitted", "task.claimed", "task.working", "task.completed"],
        )

    def test_task_submission_is_idempotent(self) -> None:
        first, first_created = self.store.create_task(self._submission())
        second, second_created = self.store.create_task(self._submission())
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["task_id"], second["task_id"])

    def test_capability_matching_and_invalid_lease(self) -> None:
        blocked = self._submission("request-blocked")
        blocked = TaskSubmission(
            principal_id=blocked.principal_id,
            delegator_actor_id=blocked.delegator_actor_id,
            assignee_actor_id=blocked.assignee_actor_id,
            objective=blocked.objective,
            context_id=blocked.context_id,
            idempotency_key=blocked.idempotency_key,
            required_capabilities=("gpu-cluster",),
            input=blocked.input,
            metadata=blocked.metadata,
            origin=blocked.origin,
        )
        self.store.create_task(blocked)
        self.assertIsNone(
            self.store.claim_task(actor_id="actor-pi", node_id="node-mac")
        )

        task, _ = self.store.create_task(self._submission("request-ready"))
        claim = self.store.claim_task(actor_id="actor-pi", node_id="node-mac")
        assert claim is not None
        with self.assertRaisesRegex(PermissionError, "invalid task lease"):
            self.store.update_task(
                task["task_id"],
                TaskUpdate(
                    run_id=claim["run"]["run_id"],
                    lease_token="wrong",
                    status=TaskStatus.COMPLETED,
                    message=None,
                    result={},
                ),
            )

    def test_local_run_and_artifact_are_first_class(self) -> None:
        run = self.store.start_run(
            RunSubmission(
                principal_id="principal-owner",
                actor_id="actor-pi",
                node_id="node-mac",
                origin="local_ui",
                objective="Answer the signed-in user",
                task_id=None,
                metadata={"client": "terminal"},
            )
        )
        artifact = self.store.add_artifact(
            ArtifactSubmission(
                name="answer.md",
                media_type="text/markdown",
                uri="file:///workspace/answer.md",
                task_id=None,
                run_id=run["run_id"],
                created_by_actor_id="actor-pi",
                sha256=None,
                size_bytes=12,
                metadata={},
            )
        )
        self.assertEqual(artifact["run_id"], run["run_id"])
        completed = self.store.update_run(
            run["run_id"],
            status=RunStatus.COMPLETED,
            result={"text": "done"},
            error=None,
        )
        self.assertEqual(completed["status"], RunStatus.COMPLETED.value)


class AgentHubApiTests(unittest.TestCase):
    def test_registration_and_idempotent_task_api(self) -> None:
        with TemporaryDirectory() as temporary:
            store = AgentHubStore(Path(temporary) / "hub.sqlite3")
            api = AgentHubApi(store)
            try:
                api.post(
                    "/v1/hub/principals",
                    {
                        "principal_id": "p",
                        "kind": "human",
                        "display_name": "Person",
                    },
                )
                api.post(
                    "/v1/hub/actors",
                    {
                        "actor_id": "human",
                        "principal_id": "p",
                        "kind": "human",
                        "display_name": "Human",
                    },
                )
                status, first = api.post(
                    "/v1/hub/tasks",
                    {
                        "principal_id": "p",
                        "delegator_actor_id": "human",
                        "objective": "Do something",
                        "idempotency_key": "same-request",
                    },
                )
                _, second = api.post(
                    "/v1/hub/tasks",
                    {
                        "principal_id": "p",
                        "delegator_actor_id": "human",
                        "objective": "Do something",
                        "idempotency_key": "same-request",
                    },
                )
                self.assertEqual(status.value, 201)
                self.assertTrue(first["created"])
                self.assertFalse(second["created"])
                self.assertEqual(
                    first["task"]["task_id"], second["task"]["task_id"]
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
