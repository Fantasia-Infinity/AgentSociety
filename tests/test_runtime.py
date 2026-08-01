from __future__ import annotations

import unittest

from wechat_bot.conversations import InMemoryConversationStore
from wechat_bot.domain import (
    ChatType,
    ContentType,
    IncomingMessage,
    ModelResponse,
    OutgoingAction,
)
from wechat_bot.runtime import ActionOutbox, BotRuntime
from wechat_bot.service import AccessPolicy, BotService


class FakeProvider:
    def complete(self, request):
        return ModelResponse(text="done")

    def health(self):
        return {"backend": "fake", "status": "ready"}


class RuntimeTests(unittest.TestCase):
    def test_health_includes_safe_model_status(self) -> None:
        provider = FakeProvider()
        service = BotService(
            provider=provider,
            conversations=InMemoryConversationStore(max_messages=10),
            policy=AccessPolicy(frozenset({"user-1"}), frozenset()),
            system_prompt="system",
        )
        runtime = BotRuntime(
            service,
            workers=1,
            queue_size=2,
            model_provider=provider,
        )

        self.assertEqual(
            runtime.health(),
            {
                "status": "ok",
                "queue_depth": 0,
                "model": {"backend": "fake", "status": "ready"},
            },
        )

    def test_action_requires_ack_and_reappears_after_lease(self) -> None:
        outbox = ActionOutbox()
        action = OutgoingAction(
            account_id="account-1",
            chat_id="user-1",
            chat_type=ChatType.DIRECT,
            content_type=ContentType.TEXT,
            content="done",
        )
        outbox.push(action)

        first = outbox.poll("account-1", timeout=0, lease_seconds=0.02)
        leased = outbox.poll("account-1", timeout=0, lease_seconds=0.02)
        redelivered = outbox.poll("account-1", timeout=0.1, lease_seconds=0.02)

        self.assertEqual(first, [action])
        self.assertEqual(leased, [])
        self.assertEqual(redelivered, [action])
        self.assertEqual(outbox.ack("account-1", [action.action_id]), 1)
        self.assertEqual(outbox.poll("account-1", timeout=0), [])

    def test_ack_is_scoped_to_account(self) -> None:
        outbox = ActionOutbox()
        action = OutgoingAction(
            account_id="account-1",
            chat_id="user-1",
            chat_type=ChatType.DIRECT,
            content_type=ContentType.TEXT,
            content="done",
        )
        outbox.push(action)
        self.assertEqual(outbox.ack("account-2", [action.action_id]), 0)
        self.assertEqual(
            outbox.poll("account-1", timeout=0, lease_seconds=1),
            [action],
        )

    def test_event_is_processed_into_account_outbox(self) -> None:
        service = BotService(
            provider=FakeProvider(),
            conversations=InMemoryConversationStore(max_messages=10),
            policy=AccessPolicy(frozenset({"user-1"}), frozenset()),
            system_prompt="system",
        )
        runtime = BotRuntime(service, workers=1, queue_size=2)
        runtime.start()
        try:
            result = runtime.submit(
                IncomingMessage(
                    message_id="message-1",
                    account_id="account-1",
                    chat_id="user-1",
                    sender_id="user-1",
                    chat_type=ChatType.DIRECT,
                    content_type=ContentType.TEXT,
                    content="hello",
                    timestamp=1,
                )
            )
            self.assertTrue(result.accepted)
            actions = runtime.poll_actions("account-1", timeout=1)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].content, "done")
        finally:
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
