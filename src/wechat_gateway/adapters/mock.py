from __future__ import annotations

import json
import logging
import sys
from threading import Lock, Thread
from typing import TextIO

from ..adapter import EventCallback, WeChatAdapterError
from ..domain import GatewayAction, GatewayEvent


logger = logging.getLogger(__name__)


class MockWeChatAdapter:
    """Console-backed adapter for end-to-end development without Windows."""

    def __init__(
        self,
        *,
        account_id: str,
        interactive: bool = False,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._account_id = account_id
        self._interactive = interactive
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout
        self._on_event: EventCallback | None = None
        self._sent: list[GatewayAction] = []
        self._lock = Lock()

    @property
    def sent_actions(self) -> list[GatewayAction]:
        with self._lock:
            return list(self._sent)

    def start(self, on_event: EventCallback) -> None:
        self._on_event = on_event
        if self._interactive:
            logger.info(
                "mock_adapter_ready enter one JSON object per line, for example: "
                '{"chat_id":"test-user-id","content":"你好"}'
            )
            Thread(target=self._read_console, name="mock-console", daemon=True).start()

    def emit(self, event: GatewayEvent) -> None:
        if self._on_event is None:
            raise WeChatAdapterError("mock adapter has not been started")
        self._on_event(event)

    def send(self, action: GatewayAction) -> None:
        if action.content_type != "text":
            raise WeChatAdapterError(
                f"mock adapter cannot send content type: {action.content_type}"
            )
        with self._lock:
            self._sent.append(action)
            self._output.write(
                "BOT_REPLY "
                + json.dumps(
                    {
                        "action_id": action.action_id,
                        "chat_id": action.chat_id,
                        "content": action.content,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._output.flush()

    def stop(self) -> None:
        self._on_event = None

    def _read_console(self) -> None:
        for raw_line in self._input:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("input must be a JSON object")
                event = GatewayEvent.from_console_dict(
                    payload,
                    account_id=self._account_id,
                )
                self.emit(event)
            except (ValueError, json.JSONDecodeError, WeChatAdapterError) as exc:
                logger.error("mock_input_rejected error=%s", exc)
