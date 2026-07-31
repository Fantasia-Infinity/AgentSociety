from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
import uuid
from typing import Any


CHAT_TYPES = frozenset({"direct", "group"})
CONTENT_TYPES = frozenset({"text", "image", "file", "audio", "video"})


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    message_id: str
    account_id: str
    chat_id: str
    sender_id: str
    chat_type: str
    content_type: str
    content: str
    timestamp: int = field(default_factory=lambda: int(time.time()))
    mentioned_bot: bool = False
    is_self: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_console_dict(
        cls,
        data: dict[str, Any],
        *,
        account_id: str,
    ) -> "GatewayEvent":
        chat_id = str(data.get("chat_id", "")).strip()
        content = str(data.get("content", ""))
        if not chat_id:
            raise ValueError("chat_id is required")
        if not content:
            raise ValueError("content is required")
        chat_type = str(data.get("chat_type", "direct"))
        content_type = str(data.get("content_type", "text"))
        if chat_type not in CHAT_TYPES:
            raise ValueError(f"unsupported chat_type: {chat_type}")
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"unsupported content_type: {content_type}")
        return cls(
            message_id=str(data.get("message_id") or uuid.uuid4()),
            account_id=account_id,
            chat_id=chat_id,
            sender_id=str(data.get("sender_id") or chat_id),
            chat_type=chat_type,
            content_type=content_type,
            content=content,
            timestamp=int(data.get("timestamp") or time.time()),
            mentioned_bot=bool(data.get("mentioned_bot", False)),
            is_self=bool(data.get("is_self", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class GatewayAction:
    action_id: str
    account_id: str
    chat_id: str
    chat_type: str
    content_type: str
    content: str
    reply_to_message_id: str | None = None
    created_at: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GatewayAction":
        required = (
            "action_id",
            "account_id",
            "chat_id",
            "chat_type",
            "content_type",
            "content",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError(f"Missing action fields: {', '.join(missing)}")
        identifiers = {
            name: str(data[name]).strip()
            for name in ("action_id", "account_id", "chat_id")
        }
        if any(not value for value in identifiers.values()):
            raise ValueError("Action identifiers cannot be empty")
        chat_type = str(data["chat_type"])
        content_type = str(data["content_type"])
        if chat_type not in CHAT_TYPES:
            raise ValueError(f"unsupported action chat_type: {chat_type}")
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"unsupported action content_type: {content_type}")
        reply_to = data.get("reply_to_message_id")
        created_at = data.get("created_at")
        return cls(
            **identifiers,
            chat_type=chat_type,
            content_type=content_type,
            content=str(data["content"]),
            reply_to_message_id=None if reply_to is None else str(reply_to),
            created_at=None if created_at is None else int(created_at),
        )
