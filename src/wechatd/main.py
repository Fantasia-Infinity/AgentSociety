from __future__ import annotations

import logging
import time

from .adapter import WeChatAdapter
from .adapters.mock import MockWeChatAdapter
from .adapters.wxauto import WxAutoAdapter
from .config import WechatdSettings
from .runtime import WechatdRuntime
from .server import WechatdHttpServer
from .state import SentActionStore, WechatdStore


logger = logging.getLogger(__name__)


def build_adapter(
    settings: WechatdSettings,
    store: WechatdStore | None = None,
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
            if store is None
            else lambda chat_id: store.get_cursor(settings.account_id, chat_id)
        ),
        cursor_setter=(
            None
            if store is None
            else lambda chat_id, cursor: store.set_cursor(
                settings.account_id, chat_id, cursor
            )
        ),
    )


def build_runtime(settings: WechatdSettings) -> WechatdRuntime:
    store = WechatdStore(settings.state_db)
    return WechatdRuntime(
        account_id=settings.account_id,
        adapter=build_adapter(settings, store),
        store=store,
        sent_actions=SentActionStore(settings.state_db),
        send_min_interval_seconds=settings.send_min_interval_seconds,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = WechatdSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    runtime = build_runtime(settings)
    server = WechatdHttpServer(
        (settings.http_host, settings.http_port),
        runtime,
        settings.http_token,
        settings.max_request_bytes,
    )
    try:
        runtime.start()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    logger.info(
        "wechatd_started account_id=%s driver=%s http=%s:%s",
        settings.account_id,
        settings.driver,
        settings.http_host,
        settings.http_port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        server.shutdown()
        runtime.stop()


if __name__ == "__main__":
    main()
