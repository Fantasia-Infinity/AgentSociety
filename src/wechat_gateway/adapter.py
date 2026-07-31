from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .domain import GatewayAction, GatewayEvent


EventCallback = Callable[[GatewayEvent], None]


class WeChatAdapterError(RuntimeError):
    pass


class WeChatAdapter(Protocol):
    def start(self, on_event: EventCallback) -> None: ...

    def send(self, action: GatewayAction) -> None: ...

    def stop(self) -> None: ...
