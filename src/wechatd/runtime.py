from __future__ import annotations

import logging
from threading import Event, Lock
import time
from typing import Any

from .adapter import WeChatAdapter
from .domain import GatewayAction, GatewayEvent
from .state import SentActionStore, WechatdStore


logger = logging.getLogger(__name__)


class WechatdRuntime:
    """Local runtime that owns the WeChat adapter and the message archive.

    Inbound wxauto events are persisted to the store and remain available for
    agent-side readers. Outbound actions are sent immediately through the
    adapter with an idempotency check and a minimum send interval guard.
    """

    def __init__(
        self,
        *,
        account_id: str,
        adapter: WeChatAdapter,
        store: WechatdStore,
        sent_actions: SentActionStore,
        send_min_interval_seconds: float,
        startup_timeout_seconds: float = 60,
    ) -> None:
        self._account_id = account_id
        self._adapter = adapter
        self._store = store
        self._sent_actions = sent_actions
        self._send_min_interval = max(send_min_interval_seconds, 0)
        self._startup_timeout = startup_timeout_seconds
        self._adapter_ready = Event()
        self._startup_error: Exception | None = None
        self._stop = Event()
        self._send_lock = Lock()
        self._last_send_at = 0.0
        self._started = False

    @property
    def account_id(self) -> str:
        return self._account_id

    def start(self) -> None:
        if self._started:
            raise RuntimeError("wechatd runtime has already been started")
        self._started = True
        try:
            self._adapter.start(self._accept_event)
        except Exception as exc:
            self._startup_error = exc
            self._adapter_ready.set()
            self.stop()
            raise RuntimeError(f"WeChat adapter startup failed: {exc}") from exc
        self._adapter_ready.set()
        if not self._adapter_ready.is_set() or self._startup_error is not None:
            self.stop()
            raise RuntimeError(f"WeChat adapter startup failed: {self._startup_error}")

    def wait_ready(self) -> None:
        if not self._adapter_ready.wait(self._startup_timeout):
            self.stop()
            raise RuntimeError("WeChat adapter startup timed out")
        if self._startup_error is not None:
            self.stop()
            raise RuntimeError(f"WeChat adapter startup failed: {self._startup_error}")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._adapter.stop()
        except Exception:
            logger.exception("wechatd_adapter_stop_failed")
        finally:
            self._sent_actions.close()
            self._store.close()

    def status(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "archive_depth": self._store.archive_depth(),
            "adapter": self._adapter.status(),
        }

    def list_chats(self, *, limit: int = 100) -> list[dict[str, object]]:
        return self._store.list_chats(self._account_id, limit=limit)

    def read_messages(
        self,
        *,
        chat_id: str,
        after_message_id: str | None = None,
        before_timestamp: float | None = None,
        limit: int = 50,
    ) -> list[GatewayEvent]:
        return self._store.messages_after(
            self._account_id,
            chat_id,
            after_message_id=after_message_id,
            before_timestamp=before_timestamp,
            limit=limit,
        )

    def get_message(self, message_id: str) -> GatewayEvent | None:
        return self._store.get_message(self._account_id, message_id)

    def get_agent_cursor(self, chat_id: str) -> str | None:
        return self._store.get_agent_cursor(self._account_id, chat_id)

    def set_agent_cursor(self, chat_id: str, cursor: str) -> None:
        self._store.set_agent_cursor(self._account_id, chat_id, cursor)

    def send(self, action: GatewayAction) -> bool:
        if action.content_type != "text":
            raise ValueError("only text actions are supported")
        if not action.content.strip():
            raise ValueError("content is required")
        with self._send_lock:
            if self._sent_actions.was_sent(action.action_id):
                return False
            if self._send_min_interval > 0:
                elapsed = time.monotonic() - self._last_send_at
                if elapsed < self._send_min_interval:
                    self._stop.wait(self._send_min_interval - elapsed)
            self._adapter.send(action)
            self._sent_actions.mark_sent(action.action_id)
            self._last_send_at = time.monotonic()
            logger.info(
                "wechatd_action_sent action_id=%s chat_id=%s",
                action.action_id,
                action.chat_id,
            )
            return True

    def _accept_event(self, event: GatewayEvent) -> bool:
        if self._stop.is_set():
            return False
        try:
            inserted = self._store.store(event)
            source_key = str(event.metadata.get("source_key", "")).strip()
            if source_key:
                self._store.set_cursor(event.account_id, event.chat_id, source_key)
        except Exception:
            logger.exception("wechatd_event_persist_failed message_id=%s", event.message_id)
            return False
        if not inserted:
            logger.info("wechatd_event_duplicate message_id=%s", event.message_id)
        return True
