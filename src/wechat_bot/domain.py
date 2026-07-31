from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import time
import uuid
from typing import Any


class ChatType(StrEnum):
    DIRECT = "direct"
    GROUP = "group"


class ContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: str
    account_id: str
    chat_id: str
    sender_id: str
    chat_type: ChatType
    content_type: ContentType
    content: str
    timestamp: int
    mentioned_bot: bool = False
    is_self: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncomingMessage":
        required = (
            "message_id",
            "account_id",
            "chat_id",
            "sender_id",
            "chat_type",
            "content_type",
            "content",
            "timestamp",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError(f"Missing message fields: {', '.join(missing)}")

        values = {name: str(data[name]).strip() for name in required[:4]}
        if any(not value for value in values.values()):
            raise ValueError("Message identifiers cannot be empty")

        return cls(
            **values,
            chat_type=ChatType(str(data["chat_type"])),
            content_type=ContentType(str(data["content_type"])),
            content=str(data["content"]),
            timestamp=int(data["timestamp"]),
            mentioned_bot=bool(data.get("mentioned_bot", False)),
            is_self=bool(data.get("is_self", False)),
            metadata=dict(data.get("metadata", {})),
        )

    @property
    def conversation_id(self) -> str:
        return f"{self.account_id}:{self.chat_type}:{self.chat_id}"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    conversation_id: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutgoingAction:
    account_id: str
    chat_id: str
    chat_type: ChatType
    content_type: ContentType
    content: str
    reply_to_message_id: str | None = None
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["chat_type"] = self.chat_type.value
        payload["content_type"] = self.content_type.value
        return payload


@dataclass(frozen=True, slots=True)
class HandleResult:
    accepted: bool
    reason: str
    action: OutgoingAction | None = None

