from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
import uuid
from typing import Any

from wechat_core.domain import ChatType, ContentType, OutgoingAction
from wechat_core.persistence import SqliteActionOutbox


class ChannelCapabilityError(RuntimeError):
    pass


class SqliteChannelService:
    """Channel facade over the Core event archive and durable action outbox.

    The first adapter is WeChat, but no MCP method exposes wxauto or Windows
    implementation details. Additional adapters can implement the same data
    model without changing agent prompts or tools.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._lock = Lock()
        self._outbox = SqliteActionOutbox(path)
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS core_inbox (
                    message_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    received_at REAL NOT NULL,
                    completed_at REAL,
                    reason TEXT
                )
                """
            )

    def capabilities(self, channel: str) -> dict[str, bool]:
        self._require_channel(channel)
        return {
            "list_conversations": True,
            "read_messages": True,
            "send": True,
            "reply": True,
            "react": False,
            "download": False,
        }

    def list_conversations(
        self,
        *,
        channel: str = "wechat",
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._require_channel(channel)
        limit = min(max(limit, 1), 500)
        messages = self._messages(account_id=account_id, row_limit=5000)
        conversations: dict[tuple[str, str], dict[str, Any]] = {}
        for message in messages:
            key = (str(message["account_id"]), str(message["chat_id"]))
            if key in conversations:
                continue
            conversations[key] = {
                "channel": channel,
                "account_id": key[0],
                "conversation_id": key[1],
                "chat_type": message["chat_type"],
                "last_message_at": message["timestamp"],
                "last_message_preview": str(message["content"])[:200],
                "capabilities": self.capabilities(channel),
            }
            if len(conversations) >= limit:
                break
        return list(conversations.values())

    def read_messages(
        self,
        *,
        channel: str = "wechat",
        account_id: str,
        conversation_id: str,
        limit: int = 50,
        before_timestamp: int | None = None,
    ) -> list[dict[str, Any]]:
        self._require_channel(channel)
        limit = min(max(limit, 1), 500)
        result = []
        for message in self._messages(account_id=account_id, row_limit=5000):
            if str(message["chat_id"]) != conversation_id:
                continue
            if before_timestamp is not None and int(message["timestamp"]) >= before_timestamp:
                continue
            result.append({"channel": channel, **message})
            if len(result) >= limit:
                break
        return list(reversed(result))

    def send(
        self,
        *,
        channel: str = "wechat",
        account_id: str,
        conversation_id: str,
        content: str,
        chat_type: str = "direct",
        idempotency_key: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_channel(channel)
        content = content.strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > 50_000:
            raise ValueError("content exceeds 50000 characters")
        action_id = self._action_id(channel, idempotency_key)
        action = OutgoingAction(
            action_id=action_id,
            account_id=account_id,
            chat_id=conversation_id,
            chat_type=ChatType(chat_type),
            content_type=ContentType.TEXT,
            content=content,
            reply_to_message_id=reply_to_message_id,
        )
        self._outbox.push(action)
        return {
            "channel": channel,
            "action_id": action_id,
            "status": "queued",
            "account_id": account_id,
            "conversation_id": conversation_id,
            "reply_to_message_id": reply_to_message_id,
        }

    def reply(
        self,
        *,
        channel: str = "wechat",
        account_id: str,
        message_id: str,
        content: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        original = self._message(message_id, account_id)
        return self.send(
            channel=channel,
            account_id=account_id,
            conversation_id=str(original["chat_id"]),
            chat_type=str(original["chat_type"]),
            content=content,
            reply_to_message_id=message_id,
            idempotency_key=idempotency_key,
        )

    def react(self, **_: Any) -> dict[str, Any]:
        raise ChannelCapabilityError("wechat adapter does not support reactions yet")

    def download(self, **_: Any) -> dict[str, Any]:
        raise ChannelCapabilityError("wechat attachment download is not implemented yet")

    def close(self) -> None:
        self._outbox.close()
        with self._lock:
            self._connection.close()

    def _messages(
        self, *, account_id: str | None, row_limit: int
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM core_inbox ORDER BY received_at DESC LIMIT ?",
                (row_limit,),
            ).fetchall()
        messages = [json.loads(str(row[0])) for row in rows]
        if account_id:
            messages = [
                message
                for message in messages
                if str(message.get("account_id", "")) == account_id
            ]
        return messages

    def _message(self, message_id: str, account_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM core_inbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise LookupError("message not found")
        message = json.loads(str(row[0]))
        if str(message.get("account_id", "")) != account_id:
            raise LookupError("message not found")
        return message

    @staticmethod
    def _require_channel(channel: str) -> None:
        if channel != "wechat":
            raise ValueError("unsupported channel")

    @staticmethod
    def _action_id(channel: str, idempotency_key: str | None) -> str:
        if not idempotency_key:
            return f"channel:{channel}:{uuid.uuid4().hex}"
        digest = hashlib.sha256(
            f"{channel}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
        return f"channel:{channel}:{digest}"
