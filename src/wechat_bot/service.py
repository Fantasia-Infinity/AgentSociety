from __future__ import annotations

from dataclasses import dataclass

from .conversations import InMemoryConversationStore, MessageDeduplicator
from .domain import (
    ChatType,
    ContentType,
    HandleResult,
    IncomingMessage,
    ModelMessage,
    ModelRequest,
    OutgoingAction,
)
from .model_provider import ModelProvider


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    allowed_users: frozenset[str]
    allowed_groups: frozenset[str]
    group_require_mention: bool = True

    def allows(self, message: IncomingMessage) -> tuple[bool, str]:
        if message.is_self:
            return False, "self_message"
        if message.content_type is not ContentType.TEXT:
            return False, "unsupported_content_type"
        if not message.content.strip():
            return False, "empty_message"

        if message.chat_type is ChatType.DIRECT:
            allowed = "*" in self.allowed_users or message.sender_id in self.allowed_users
            return (allowed, "allowed" if allowed else "user_not_allowed")

        allowed = "*" in self.allowed_groups or message.chat_id in self.allowed_groups
        if not allowed:
            return False, "group_not_allowed"
        if self.group_require_mention and not message.mentioned_bot:
            return False, "mention_required"
        return True, "allowed"


class BotService:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        conversations: InMemoryConversationStore,
        policy: AccessPolicy,
        system_prompt: str,
        deduplicator: MessageDeduplicator | None = None,
    ) -> None:
        self._provider = provider
        self._conversations = conversations
        self._policy = policy
        self._system_prompt = system_prompt
        self._deduplicator = deduplicator or MessageDeduplicator()

    def handle(self, message: IncomingMessage) -> HandleResult:
        allowed, reason = self._policy.allows(message)
        if not allowed:
            return HandleResult(accepted=False, reason=reason)
        if not self._deduplicator.acquire(message.message_id):
            return HandleResult(accepted=False, reason="duplicate")

        try:
            with self._conversations.conversation_lock(message.conversation_id):
                history = self._conversations.get(message.conversation_id)
                request = ModelRequest(
                    conversation_id=message.conversation_id,
                    messages=(
                        ModelMessage(role="system", content=self._system_prompt),
                        *history,
                        ModelMessage(role="user", content=message.content.strip()),
                    ),
                )
                response = self._provider.complete(request)
                self._conversations.append_exchange(
                    message.conversation_id,
                    message.content.strip(),
                    response.text,
                )
                action = OutgoingAction(
                    account_id=message.account_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    content_type=ContentType.TEXT,
                    content=response.text,
                    reply_to_message_id=message.message_id,
                )
            self._deduplicator.complete(message.message_id)
            return HandleResult(accepted=True, reason="completed", action=action)
        except Exception:
            self._deduplicator.release(message.message_id)
            raise

