from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from agent_channel.mcp_server import ChannelMcpServer
from agent_channel.service import SqliteChannelService
from agent_hub.a2a import A2AApi
from agent_hub.domain import (
    ActorRegistration,
    NodeRegistration,
    PrincipalRegistration,
    TaskSubmission,
)
from agent_hub.object_store import FileObjectStore
from agent_hub.store import AgentHubStore
from wechat_bot.domain import (
    ChatType,
    ContentType,
    IncomingMessage,
    OutgoingAction,
)
from wechat_bot.persistence import (
    CoreInboxStore,
    SqliteActionOutbox,
    SqliteConversationStore,
    SqliteMessageDeduplicator,
    SqliteProcessingCoordinator,
)


def incoming(message_id: str = "message-1") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        account_id="account-1",
        chat_id="chat-1",
        sender_id="user-1",
        chat_type=ChatType.DIRECT,
        content_type=ContentType.TEXT,
        content="hello",
        timestamp=123,
    )


class DurableProcessingTests(unittest.TestCase):
    def test_response_consequences_commit_together(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            conversations = SqliteConversationStore(path, max_messages=10)
            dedup = SqliteMessageDeduplicator(path)
            outbox = SqliteActionOutbox(path)
            coordinator = SqliteProcessingCoordinator(path)
            try:
                message = incoming()
                self.assertTrue(inbox.insert(message))
                self.assertEqual(inbox.claim_pending()[0][0], message)
                action = OutgoingAction(
                    action_id="reply:message-1",
                    account_id="account-1",
                    chat_id="chat-1",
                    chat_type=ChatType.DIRECT,
                    content_type=ContentType.TEXT,
                    content="reply",
                    reply_to_message_id=message.message_id,
                )
                coordinator.commit(
                    message,
                    assistant_content="reply",
                    action=action,
                    reason="completed",
                )
                self.assertEqual(inbox.status(message.message_id), "completed")
                self.assertEqual(
                    [item.content for item in conversations.get(message.conversation_id)],
                    ["hello", "reply"],
                )
                self.assertFalse(dedup.acquire(message.message_id))
                self.assertEqual(
                    outbox.poll("account-1", timeout=0)[0].action_id,
                    action.action_id,
                )
            finally:
                coordinator.close()
                outbox.close()
                dedup.close()
                conversations.close()
                inbox.close()

    def test_failed_outbox_insert_rolls_back_every_consequence(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            conversations = SqliteConversationStore(path, max_messages=10)
            dedup = SqliteMessageDeduplicator(path)
            outbox = SqliteActionOutbox(path)
            coordinator = SqliteProcessingCoordinator(path)
            message = incoming("rollback-message")
            inbox.insert(message)
            inbox.claim_pending()
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TRIGGER fail_action BEFORE INSERT ON core_actions
                BEGIN SELECT RAISE(ABORT, 'simulated outbox failure'); END
                """
            )
            connection.commit()
            action = OutgoingAction(
                action_id="reply:rollback-message",
                account_id="account-1",
                chat_id="chat-1",
                chat_type=ChatType.DIRECT,
                content_type=ContentType.TEXT,
                content="reply",
            )
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    coordinator.commit(
                        message,
                        assistant_content="reply",
                        action=action,
                        reason="completed",
                    )
                self.assertEqual(inbox.status(message.message_id), "processing")
                self.assertEqual(conversations.get(message.conversation_id), ())
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM processed_messages WHERE message_id=?",
                        (message.message_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(outbox.poll("account-1", timeout=0), [])
            finally:
                connection.close()
                coordinator.close()
                outbox.close()
                dedup.close()
                conversations.close()
                inbox.close()

    def test_lost_inbox_completion_rolls_back_every_consequence(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            conversations = SqliteConversationStore(path, max_messages=10)
            dedup = SqliteMessageDeduplicator(path)
            outbox = SqliteActionOutbox(path)
            coordinator = SqliteProcessingCoordinator(path)
            message = incoming("lost-lease-message")
            inbox.insert(message)
            inbox.claim_pending()
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TRIGGER lose_inbox_completion BEFORE UPDATE ON core_inbox
                WHEN NEW.status = 'completed'
                BEGIN SELECT RAISE(IGNORE); END
                """
            )
            connection.commit()
            action = OutgoingAction(
                action_id="reply:lost-lease-message",
                account_id="account-1",
                chat_id="chat-1",
                chat_type=ChatType.DIRECT,
                content_type=ContentType.TEXT,
                content="reply",
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "lease was lost"):
                    coordinator.commit(
                        message,
                        assistant_content="reply",
                        action=action,
                        reason="completed",
                    )
                self.assertEqual(inbox.status(message.message_id), "processing")
                self.assertEqual(conversations.get(message.conversation_id), ())
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM processed_messages WHERE message_id=?",
                        (message.message_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(outbox.poll("account-1", timeout=0), [])
            finally:
                connection.close()
                coordinator.close()
                outbox.close()
                dedup.close()
                conversations.close()
                inbox.close()


class ChannelMcpTests(unittest.TestCase):
    def test_mcp_negotiates_current_legacy_and_unknown_versions(self) -> None:
        with TemporaryDirectory() as directory:
            service = SqliteChannelService(Path(directory) / "core.sqlite3")
            try:
                for requested, expected in (
                    ("2025-06-18", "2025-06-18"),
                    ("2025-03-26", "2025-03-26"),
                    ("2099-01-01", "2025-06-18"),
                ):
                    server = ChannelMcpServer(service)
                    response = server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": requested,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": requested,
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"},
                            },
                        }
                    )
                    self.assertEqual(
                        response["result"]["protocolVersion"], expected
                    )
            finally:
                service.close()

    def test_mcp_lists_reads_and_queues_idempotent_send(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            inbox.insert(incoming())
            inbox.close()
            service = SqliteChannelService(path)
            server = ChannelMcpServer(service)
            try:
                initialized = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    }
                )
                self.assertEqual(
                    initialized["result"]["protocolVersion"], "2025-06-18"
                )
                self.assertIsNone(
                    server.handle(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                        }
                    )
                )
                response = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "channel_read_messages",
                            "arguments": {
                                "account_id": "account-1",
                                "conversation_id": "chat-1",
                            },
                        },
                    }
                )
                self.assertEqual(
                    response["result"]["structuredContent"]["result"][0]["content"],
                    "hello",
                )
                send = service.send(
                    channel="wechat",
                    account_id="account-1",
                    conversation_id="chat-1",
                    content="outbound",
                    idempotency_key="same-send",
                )
                repeated = service.send(
                    channel="wechat",
                    account_id="account-1",
                    conversation_id="chat-1",
                    content="outbound",
                    idempotency_key="same-send",
                )
                self.assertEqual(send["action_id"], repeated["action_id"])
                outbox = SqliteActionOutbox(path)
                try:
                    self.assertEqual(
                        len(outbox.poll("account-1", timeout=0)), 1
                    )
                finally:
                    outbox.close()
            finally:
                service.close()


class A2ATests(unittest.TestCase):
    def test_a2a_send_get_followup_and_cancel(self) -> None:
        with TemporaryDirectory() as directory:
            store = AgentHubStore(Path(directory) / "hub.sqlite3")
            api = A2AApi(store)
            try:
                sent = api.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "SendMessage",
                        "params": {
                            "message": {
                                "messageId": "a2a-message-1",
                                "role": "ROLE_USER",
                                "parts": [{"text": "Run the tests"}],
                            }
                        },
                    },
                    version="1.0",
                )
                task = sent["result"]["task"]
                self.assertEqual(task["status"]["state"], "TASK_STATE_SUBMITTED")
                fetched = api.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "GetTask",
                        "params": {"id": task["id"]},
                    },
                    version="1.0",
                )
                self.assertEqual(fetched["result"]["task"]["id"], task["id"])
                followed = api.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "SendMessage",
                        "params": {
                            "message": {
                                "messageId": "a2a-message-2",
                                "taskId": task["id"],
                                "role": "ROLE_USER",
                                "parts": [{"text": "Also report coverage"}],
                            }
                        },
                    },
                    version="1.0",
                )
                self.assertEqual(followed["result"]["task"]["id"], task["id"])
                self.assertIn(
                    "task.control.follow_up",
                    [
                        event["type"]
                        for event in store.list_task_events(task["id"])
                    ],
                )
                cancelled = api.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "CancelTask",
                        "params": {"id": task["id"]},
                    },
                    version="1.0",
                )
                self.assertEqual(
                    cancelled["result"]["task"]["status"]["state"],
                    "TASK_STATE_CANCELED",
                )
            finally:
                store.close()


class StorageTests(unittest.TestCase):
    def test_file_object_store_is_content_addressed(self) -> None:
        with TemporaryDirectory() as directory:
            store = FileObjectStore(Path(directory))
            first = store.put(b"payload", name="a.txt", media_type="text/plain")
            second = store.put(b"payload", name="b.txt", media_type="text/plain")
            self.assertEqual(first, second)
            self.assertEqual(Path(first.uri.removeprefix("file://")).read_bytes(), b"payload")


class HubLeaseTests(unittest.TestCase):
    def test_unassigned_task_remains_portable_and_controls_are_acked(self) -> None:
        with TemporaryDirectory() as directory:
            store = AgentHubStore(Path(directory) / "hub.sqlite3")
            try:
                store.register_principal(
                    PrincipalRegistration("p", "human", "Person", {})
                )
                store.register_actor(
                    ActorRegistration("human", "p", "human", "Human", (), {})
                )
                store.register_actor(
                    ActorRegistration("agent", "p", "agent", "Agent", (), {})
                )
                store.register_node(NodeRegistration("node", "agent", "Node", (), {}))
                task, _ = store.create_task(
                    TaskSubmission(
                        principal_id="p",
                        delegator_actor_id="human",
                        objective="work",
                        assignee_actor_id=None,
                        context_id=None,
                        idempotency_key=None,
                        required_capabilities=(),
                        input={},
                        metadata={},
                        origin="test",
                    )
                )
                claim = store.claim_task(
                    actor_id="agent", node_id="node", lease_seconds=17
                )
                self.assertIsNotNone(claim)
                self.assertIsNone(claim["task"]["assignee_actor_id"])
                control = store.create_task_control(
                    task["task_id"], actor_id="human", kind="steer", message="focus"
                )
                controls = store.claim_task_controls(
                    task["task_id"],
                    run_id=claim["run"]["run_id"],
                    lease_token=claim["lease_token"],
                )
                self.assertEqual(controls[0]["control_id"], control["control_id"])
                delivered = store.acknowledge_task_control(
                    task["task_id"],
                    control["control_id"],
                    run_id=claim["run"]["run_id"],
                    lease_token=controls[0]["lease_token"],
                )
                self.assertEqual(delivered["status"], "delivered")
            finally:
                store.close()

    def test_expired_unassigned_task_can_move_to_another_node(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hub.sqlite3"
            store = AgentHubStore(path)
            try:
                store.register_principal(
                    PrincipalRegistration("p", "human", "Person", {})
                )
                store.register_actor(
                    ActorRegistration("human", "p", "human", "Human", (), {})
                )
                for suffix in ("one", "two"):
                    store.register_actor(
                        ActorRegistration(
                            f"agent-{suffix}",
                            "p",
                            "agent",
                            f"Agent {suffix}",
                            (),
                            {},
                        )
                    )
                    store.register_node(
                        NodeRegistration(
                            f"node-{suffix}",
                            f"agent-{suffix}",
                            f"Node {suffix}",
                            (),
                            {},
                        )
                    )
                task, _ = store.create_task(
                    TaskSubmission(
                        principal_id="p",
                        delegator_actor_id="human",
                        objective="portable work",
                        assignee_actor_id=None,
                        context_id=None,
                        idempotency_key=None,
                        required_capabilities=(),
                        input={},
                        metadata={},
                        origin="test",
                    )
                )
                first = store.claim_task(
                    actor_id="agent-one", node_id="node-one", lease_seconds=10
                )
                self.assertIsNotNone(first)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(
                        "UPDATE hub_tasks SET lease_until=0 WHERE task_id=?",
                        (task["task_id"],),
                    )
                    connection.commit()
                finally:
                    connection.close()
                second = store.claim_task(
                    actor_id="agent-two", node_id="node-two", lease_seconds=10
                )
                self.assertIsNotNone(second)
                self.assertEqual(
                    second["task"]["executor_node_id"], "node-two"
                )
                self.assertIsNone(second["task"]["assignee_actor_id"])
                self.assertEqual(
                    store.get_run(first["run"]["run_id"])["error"],
                    "lease_expired",
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
