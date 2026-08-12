from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


logger = logging.getLogger(__name__)


class ChannelUnavailableError(RuntimeError):
    """Raised when the local wechatd service cannot be reached."""


class ChannelCapabilityError(RuntimeError):
    pass


class HttpChannelService:
    """Channel facade over the local wechatd HTTP API.

    The first adapter is WeChat, but no MCP method exposes wxauto or Windows
    implementation details. Agent-side reads are cursor based: the first read
    of a chat returns messages from the beginning of the archive, and every
    read advances the cursor so the next call only returns new messages.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

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
        payload = self._request("GET", f"/v1/chats?limit={limit}")
        conversations = []
        for chat in payload.get("chats", []):
            conversations.append(
                {
                    "channel": channel,
                    "account_id": account_id or "",
                    "conversation_id": str(chat.get("chat_id", "")),
                    "chat_type": chat.get("chat_type", "direct"),
                    "last_message_at": chat.get("last_message_at"),
                    "last_message_preview": str(chat.get("last_message_preview", "")),
                    "capabilities": self.capabilities(channel),
                }
            )
        return conversations

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
        if before_timestamp is not None:
            payload = self._request(
                "GET",
                f"/v1/messages?chat_id={self._quote(conversation_id)}"
                f"&before_timestamp={before_timestamp}&limit={limit}",
            )
            return [
                {"channel": channel, **message}
                for message in payload.get("messages", [])
            ]
        cursor = self._agent_cursor(conversation_id)
        suffix = f"&after_message_id={self._quote(cursor)}" if cursor else ""
        payload = self._request(
            "GET",
            f"/v1/messages?chat_id={self._quote(conversation_id)}"
            f"{suffix}&limit={limit}",
        )
        messages = [
            {"channel": channel, **message}
            for message in payload.get("messages", [])
        ]
        next_cursor = payload.get("next_cursor")
        if next_cursor:
            self._set_agent_cursor(conversation_id, str(next_cursor))
        return messages

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
        result = self._request(
            "POST",
            "/v1/send",
            {
                "chat_id": conversation_id,
                "content": content,
                "chat_type": chat_type,
                "idempotency_key": idempotency_key or "",
            },
        )
        return {
            "channel": channel,
            "action_id": result.get("action_id", ""),
            "status": "sent" if result.get("sent") else "duplicate",
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
        payload = self._request("GET", f"/v1/message?message_id={self._quote(message_id)}")
        message = payload.get("message")
        if not isinstance(message, dict) or not message.get("chat_id"):
            raise LookupError("message not found")
        return self.send(
            channel=channel,
            account_id=account_id,
            conversation_id=str(message["chat_id"]),
            chat_type=str(message.get("chat_type", "direct")),
            content=content,
            reply_to_message_id=message_id,
            idempotency_key=idempotency_key,
        )

    def react(self, **_: Any) -> dict[str, Any]:
        raise ChannelCapabilityError("wechat adapter does not support reactions yet")

    def download(self, **_: Any) -> dict[str, Any]:
        raise ChannelCapabilityError("wechat attachment download is not implemented yet")

    def _agent_cursor(self, conversation_id: str) -> str | None:
        payload = self._request(
            "GET", f"/v1/agent_cursor?chat_id={self._quote(conversation_id)}"
        )
        cursor = payload.get("cursor")
        return None if not cursor else str(cursor)

    def _set_agent_cursor(self, conversation_id: str, cursor: str) -> None:
        self._request("PUT", "/v1/agent_cursor", {"chat_id": conversation_id, "cursor": cursor})

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            message = self._error_message(exc)
            if exc.code == 401:
                raise ChannelUnavailableError("wechatd rejected the channel token") from exc
            if exc.code == 404:
                raise LookupError(message or "not found") from exc
            raise ChannelUnavailableError(f"wechatd returned HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise ChannelUnavailableError(
                f"cannot reach wechatd at {self._base_url}: {exc.reason}"
            ) from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChannelUnavailableError("wechatd returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ChannelUnavailableError("wechatd returned an unexpected response")
        return parsed

    @staticmethod
    def _error_message(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(payload, dict):
                return str(payload.get("message", ""))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return ""
        return ""

    @staticmethod
    def _quote(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    @staticmethod
    def _require_channel(channel: str) -> None:
        if channel != "wechat":
            raise ValueError("unsupported channel")
