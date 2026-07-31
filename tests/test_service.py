from __future__ import annotations

import unittest

from wechat_bot.conversations import InMemoryConversationStore
from wechat_bot.domain import (
    ChatType,
    ContentType,
    IncomingMessage,
    ModelResponse,
)
from wechat_bot.service import AccessPolicy, BotService


class FakeProvider:
    def __init__(self) -> None:
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelResponse(text=f"reply:{request.messages[-1].content}", model="fake")


def message(**overrides) -> IncomingMessage:
    values = {
        "message_id": "message-1",
        "account_id": "account-1",
        "chat_id": "user-1",
        "sender_id": "user-1",
        "chat_type": ChatType.DIRECT,
        "content_type": ContentType.TEXT,
        "content": "hello",
        "timestamp": 1,
    }
    values.update(overrides)
    return IncomingMessage(**values)


class BotServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeProvider()
        self.service = BotService(
            provider=self.provider,
            conversations=InMemoryConversationStore(max_messages=20),
            policy=AccessPolicy(
                allowed_users=frozenset({"user-1"}),
                allowed_groups=frozenset({"group-1"}),
            ),
            system_prompt="system",
        )

    def test_allowed_direct_message_generates_reply(self) -> None:
        result = self.service.handle(message())
        self.assertTrue(result.accepted)
        self.assertEqual(result.action.content, "reply:hello")
        self.assertEqual(result.action.reply_to_message_id, "message-1")

    def test_history_is_isolated_and_reused_per_conversation(self) -> None:
        self.service.handle(message())
        self.service.handle(message(message_id="message-2", content="again"))
        second_roles = [item.role for item in self.provider.requests[1].messages]
        self.assertEqual(second_roles, ["system", "user", "assistant", "user"])

    def test_duplicate_is_not_sent_twice(self) -> None:
        self.service.handle(message())
        duplicate = self.service.handle(message())
        self.assertFalse(duplicate.accepted)
        self.assertEqual(duplicate.reason, "duplicate")
        self.assertEqual(len(self.provider.requests), 1)

    def test_group_requires_mention(self) -> None:
        result = self.service.handle(
            message(
                message_id="group-message",
                chat_id="group-1",
                chat_type=ChatType.GROUP,
                mentioned_bot=False,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "mention_required")

    def test_empty_allowlist_denies_user(self) -> None:
        service = BotService(
            provider=self.provider,
            conversations=InMemoryConversationStore(max_messages=20),
            policy=AccessPolicy(frozenset(), frozenset()),
            system_prompt="system",
        )
        result = service.handle(message())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "user_not_allowed")


if __name__ == "__main__":
    unittest.main()

