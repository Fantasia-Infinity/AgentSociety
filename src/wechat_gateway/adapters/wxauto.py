from __future__ import annotations

import hashlib
import importlib
import logging
from threading import RLock
import time
from typing import Any, Callable

from ..adapter import EventCallback, WeChatAdapterError
from ..domain import GatewayAction, GatewayEvent


logger = logging.getLogger(__name__)


_CONTENT_TYPE_MAP = {
    "text": "text",
    "quote": "text",
    "image": "image",
    "file": "file",
    "voice": "audio",
    "video": "video",
}


class WxAutoAdapter:
    """Optional Windows adapter for wxauto 4.x free or Plus packages."""

    def __init__(
        self,
        *,
        account_id: str,
        module_name: str,
        listen_chats: tuple[str, ...],
        bot_mention: str,
        wechat_factory: Callable[[], Any] | None = None,
    ) -> None:
        if module_name not in {"wxauto4", "wxautox4"}:
            raise ValueError("unsupported wxauto module")
        self._account_id = account_id
        self._module_name = module_name
        self._listen_chats = listen_chats
        self._bot_mention = bot_mention
        self._factory = wechat_factory
        self._wechat: Any | None = None
        self._on_event: EventCallback | None = None
        self._send_lock = RLock()

    def start(self, on_event: EventCallback) -> None:
        if self._factory is None:
            try:
                module = importlib.import_module(self._module_name)
                factory = module.WeChat
            except (ImportError, AttributeError) as exc:
                raise WeChatAdapterError(
                    f"Cannot load {self._module_name}; install it on Windows first"
                ) from exc
        else:
            factory = self._factory

        self._on_event = on_event
        try:
            self._wechat = factory()
            for nickname in self._listen_chats:
                result = self._wechat.AddListenChat(
                    nickname=nickname,
                    callback=self._handle_message,
                )
                if result is not None and not bool(result):
                    raise WeChatAdapterError(
                        f"wxauto could not listen to {nickname}: "
                        f"{_response_message(result)}"
                    )
        except WeChatAdapterError:
            raise
        except Exception as exc:
            raise WeChatAdapterError(f"wxauto startup failed: {exc}") from exc
        logger.info(
            "wxauto_adapter_ready driver=%s chats=%s",
            self._module_name,
            len(self._listen_chats),
        )

    def send(self, action: GatewayAction) -> None:
        if action.content_type != "text":
            raise WeChatAdapterError(
                f"wxauto adapter cannot send content type: {action.content_type}"
            )
        if self._wechat is None:
            raise WeChatAdapterError("wxauto adapter has not been started")
        try:
            with self._send_lock:
                result = self._wechat.SendMsg(
                    msg=action.content,
                    who=action.chat_id,
                    exact=True,
                )
        except Exception as exc:
            raise WeChatAdapterError(f"wxauto send failed: {exc}") from exc
        if result is not None and not bool(result):
            raise WeChatAdapterError(
                f"wxauto send failed: {_response_message(result)}"
            )

    def stop(self) -> None:
        wechat = self._wechat
        self._wechat = None
        self._on_event = None
        if wechat is None:
            return
        stop_listening = getattr(wechat, "StopListening", None)
        if callable(stop_listening):
            try:
                stop_listening()
            except Exception:
                logger.exception("wxauto_stop_failed")

    def _handle_message(self, message: Any, chat: Any) -> None:
        callback = self._on_event
        if callback is None:
            return
        try:
            attr = str(getattr(message, "attr", "other"))
            if attr not in {"friend", "self"}:
                return
            raw_type = str(getattr(message, "type", "other"))
            content_type = _CONTENT_TYPE_MAP.get(raw_type)
            if content_type is None:
                return

            chat_info = _chat_info(chat)
            chat_id = str(
                chat_info.get("chat_name") or getattr(chat, "who", "")
            ).strip()
            if not chat_id:
                raise ValueError("wxauto callback did not include a chat name")
            raw_chat_type = str(
                chat_info.get("chat_type") or getattr(chat, "chat_type", "friend")
            )
            chat_type = "group" if raw_chat_type == "group" else "direct"
            sender_id = str(getattr(message, "sender", "") or chat_id).strip()
            content = str(getattr(message, "content", ""))
            timestamp = int(time.time())
            callback(
                GatewayEvent(
                    message_id=_message_id(
                        self._account_id,
                        message,
                        chat_id,
                        sender_id,
                        content,
                        timestamp,
                    ),
                    account_id=self._account_id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    chat_type=chat_type,
                    content_type=content_type,
                    content=content,
                    timestamp=timestamp,
                    mentioned_bot=(
                        chat_type == "group"
                        and bool(self._bot_mention)
                        and self._bot_mention in content
                    ),
                    is_self=attr == "self",
                    metadata={
                        "driver": self._module_name,
                        "wxauto_attr": attr,
                        "wxauto_type": raw_type,
                        "wxauto_chat_type": raw_chat_type,
                    },
                )
            )
        except Exception:
            logger.exception("wxauto_message_normalization_failed")


def _chat_info(chat: Any) -> dict[str, Any]:
    method = getattr(chat, "ChatInfo", None)
    if not callable(method):
        return {}
    value = method()
    return value if isinstance(value, dict) else {}


def _message_id(
    account_id: str,
    message: Any,
    chat_id: str,
    sender_id: str,
    content: str,
    timestamp: int,
) -> str:
    for name in ("hash", "id"):
        value = getattr(message, name, None)
        if value is not None and not callable(value) and str(value).strip():
            return f"wxauto:{account_id}:{value}"
    raw = "\0".join((account_id, chat_id, sender_id, content, str(timestamp)))
    return "wxauto:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _response_message(response: Any) -> str:
    try:
        return str(response["message"])
    except (KeyError, TypeError):
        return str(response)
