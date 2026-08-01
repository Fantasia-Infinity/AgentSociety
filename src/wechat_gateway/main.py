from __future__ import annotations

import logging
import time

from .adapter import WeChatAdapter
from .adapters.mock import MockWeChatAdapter
from .adapters.wxauto import WxAutoAdapter
from .config import GatewaySettings
from .core_client import GatewayCoreClient
from .runtime import GatewayRuntime
from .state import GatewayInboxStore, SentActionStore


logger = logging.getLogger(__name__)


def build_adapter(
    settings: GatewaySettings,
    inbox: GatewayInboxStore | None = None,
) -> WeChatAdapter:
    if settings.driver == "mock":
        return MockWeChatAdapter(account_id=settings.account_id, interactive=True)
    return WxAutoAdapter(
        account_id=settings.account_id,
        module_name=settings.driver,
        listen_chats=settings.listen_chats,
        bot_mention=settings.bot_mention,
        poll_interval_seconds=settings.wechat_poll_interval_seconds,
        cursor_getter=(
            None
            if inbox is None
            else lambda chat_id: inbox.get_cursor(settings.account_id, chat_id)
        ),
        cursor_setter=(
            None
            if inbox is None
            else lambda chat_id, cursor: inbox.set_cursor(
                settings.account_id, chat_id, cursor
            )
        ),
    )


def build_runtime(settings: GatewaySettings) -> GatewayRuntime:
    inbox = GatewayInboxStore(settings.state_db)
    return GatewayRuntime(
        adapter=build_adapter(settings, inbox),
        client=GatewayCoreClient(
            base_url=settings.core_url,
            api_token=settings.api_token,
            account_id=settings.account_id,
            timeout_seconds=settings.http_timeout_seconds,
        ),
        sent_actions=SentActionStore(settings.state_db),
        inbox=inbox,
        event_queue_size=settings.event_queue_size,
        poll_timeout_seconds=settings.poll_timeout_seconds,
        action_lease_seconds=settings.action_lease_seconds,
        retry_min_seconds=settings.retry_min_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = GatewaySettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    runtime = build_runtime(settings)
    try:
        runtime.start()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    logger.info(
        "wechat_gateway_started account_id=%s driver=%s core=%s",
        settings.account_id,
        settings.driver,
        settings.core_url,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
