from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from wechat_bot.domain import (
    ChatType,
    ContentType,
    IncomingMessage,
    ModelResponse,
)
from wechat_bot.model_provider import ModelProviderError
from wechat_bot.persistence import (
    CoreInboxStore,
    SqliteActionOutbox,
    SqliteConversationStore,
    SqliteMessageDeduplicator,
)
from wechat_bot.runtime import BotRuntime
from wechat_bot.service import AccessPolicy, BotService


class FakeProvider:
    def complete(self, request):
        return ModelResponse(text="persistent reply")


class RecoveringProvider:
    def __init__(self) -> None:
        self.available = False
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if not self.available:
            raise ModelProviderError("local unavailable")
        return ModelResponse(text="recovered reply")


def wait_until(predicate, timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def message(message_id: str = "persistent-message") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        account_id="account-1",
        chat_id="user-1",
        sender_id="user-1",
        chat_type=ChatType.DIRECT,
        content_type=ContentType.TEXT,
        content="hello",
        timestamp=1,
    )


class PersistenceTests(unittest.TestCase):
    def test_local_model_outage_retries_without_duplicate_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            conversations = SqliteConversationStore(path, max_messages=10)
            dedup = SqliteMessageDeduplicator(path)
            outbox = SqliteActionOutbox(path)
            provider = RecoveringProvider()
            service = BotService(
                provider=provider,
                conversations=conversations,
                policy=AccessPolicy(frozenset({"user-1"}), frozenset()),
                system_prompt="system",
                deduplicator=dedup,
            )
            runtime = BotRuntime(
                service,
                workers=1,
                queue_size=10,
                inbox=inbox,
                action_outbox=outbox,
                model_provider=provider,
                closeables=(inbox, conversations, dedup, outbox),
            )
            runtime.start()
            try:
                runtime.submit(message("local-recovery-message"))
                self.assertTrue(wait_until(lambda: provider.calls >= 1))
                provider.available = True

                actions = []

                def collect_action() -> bool:
                    actions.extend(
                        outbox.poll("account-1", timeout=0.05, lease_seconds=1)
                    )
                    return bool(actions)

                self.assertTrue(wait_until(collect_action, timeout=3))
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0].content, "recovered reply")
                self.assertEqual(
                    outbox.ack("account-1", [actions[0].action_id]),
                    1,
                )
                self.assertEqual(outbox.poll("account-1", timeout=0), [])
            finally:
                runtime.stop()

    def test_core_inbox_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            self.assertTrue(inbox.insert(message()))
            inbox.close()

            reopened = CoreInboxStore(path)
            try:
                claimed = reopened.claim_pending()
                self.assertEqual(claimed[0][0].message_id, "persistent-message")
                reopened.mark_completed("persistent-message", "completed")
                self.assertEqual(reopened.status("persistent-message"), "completed")
            finally:
                reopened.close()

    def test_conversation_and_action_outbox_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            conversations = SqliteConversationStore(path, max_messages=10)
            conversations.append_exchange(
                "account-1:direct:user-1",
                "hello",
                "reply",
                message_id="message-1",
            )
            conversations.close()

            reopened_conversations = SqliteConversationStore(path, max_messages=10)
            self.assertEqual(
                [item.content for item in reopened_conversations.get("account-1:direct:user-1")],
                ["hello", "reply"],
            )
            reopened_conversations.close()

            from wechat_bot.domain import OutgoingAction

            outbox = SqliteActionOutbox(path)
            outbox.push(
                OutgoingAction(
                    account_id="account-1",
                    chat_id="user-1",
                    chat_type=ChatType.DIRECT,
                    content_type=ContentType.TEXT,
                    content="reply",
                    action_id="action-1",
                )
            )
            outbox.close()
            reopened_outbox = SqliteActionOutbox(path)
            try:
                actions = reopened_outbox.poll(
                    "account-1", timeout=0, lease_seconds=1
                )
                self.assertEqual([item.action_id for item in actions], ["action-1"])
                self.assertEqual(reopened_outbox.ack("account-1", ["action-1"]), 1)
            finally:
                reopened_outbox.close()

    def test_runtime_recovers_pending_message_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.sqlite3"
            inbox = CoreInboxStore(path)
            inbox.insert(message("restart-message"))
            inbox.close()
            inbox = CoreInboxStore(path)

            conversations = SqliteConversationStore(path, max_messages=10)
            dedup = SqliteMessageDeduplicator(path)
            outbox = SqliteActionOutbox(path)
            service = BotService(
                provider=FakeProvider(),
                conversations=conversations,
                policy=AccessPolicy(frozenset({"user-1"}), frozenset()),
                system_prompt="system",
                deduplicator=dedup,
            )
            runtime = BotRuntime(
                service,
                workers=1,
                queue_size=10,
                inbox=inbox,
                action_outbox=outbox,
                closeables=(inbox, conversations, dedup, outbox),
            )
            runtime.start()
            try:
                actions = []

                def collect_action() -> bool:
                    actions.extend(
                        outbox.poll("account-1", timeout=0.05, lease_seconds=1)
                    )
                    return bool(actions)

                self.assertTrue(wait_until(collect_action))
                self.assertEqual(actions[0].reply_to_message_id, "restart-message")
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
