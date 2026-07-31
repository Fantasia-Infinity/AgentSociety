from __future__ import annotations

import unittest

from wechat_bot.conversations import InMemoryConversationStore
from wechat_bot.domain import ChatType, ContentType, IncomingMessage, ModelResponse
from wechat_bot.runtime import BotRuntime
from wechat_bot.service import AccessPolicy, BotService


class FakeProvider:
    def complete(self, request):
        return ModelResponse(text="done")


class RuntimeTests(unittest.TestCase):
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
