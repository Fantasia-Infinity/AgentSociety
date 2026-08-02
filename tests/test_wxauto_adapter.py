from __future__ import annotations

from types import SimpleNamespace
from threading import Lock
import time
import unittest

from wechat_gateway.adapters.wxauto import (
    WxAutoAdapter,
    _install_navigation_alias,
)
from wechat_gateway.adapter import WeChatAdapterError
from wechat_gateway.domain import GatewayAction


class FakeWeChat:
    def __init__(self) -> None:
        self.callback = None
        self.listened = []
        self.sent = []
        self.stopped = False

    def AddListenChat(self, *, nickname, callback):
        self.listened.append(nickname)
        self.callback = callback
        return True

    def SendMsg(self, **kwargs):
        self.sent.append(kwargs)
        return True

    def StopListening(self):
        self.stopped = True


class FakeChat:
    who = "项目群"
    chat_type = "group"

    def ChatInfo(self):
        return {"chat_name": "项目群", "chat_type": "group"}


class FakePollingWeChat:
    def __init__(
        self,
        messages,
        *,
        empty_polls: int = 0,
        volatile_hashes: bool = False,
    ) -> None:
        self._messages = list(messages)
        self._empty_polls = empty_polls
        self._volatile_hashes = volatile_hashes
        self._session_message = None
        self._session_time = None
        self._lock = Lock()
        self.available = True
        self.current_chat = ""
        self.poll_calls = 0
        self.sent = []

    def ChatWith(self, who, exact=True):
        if not self.available:
            raise LookupError("stale WeChat controls")
        self.current_chat = who
        return True

    def ChatInfo(self):
        return {"chat_name": self.current_chat, "chat_type": "friend"}

    def GetAllMessage(self):
        with self._lock:
            self.poll_calls += 1
            if self._empty_polls:
                self._empty_polls -= 1
                return []
            if self._volatile_hashes:
                messages = []
                for message in self._messages:
                    values = vars(message).copy()
                    values["hash"] = f"{values.get('hash', '')}:{self.poll_calls}"
                    messages.append(SimpleNamespace(**values))
                return messages
            return list(self._messages)

    def GetSession(self):
        with self._lock:
            message = (
                self._session_message
                if self._session_message is not None
                else (self._messages[-1] if self._messages else None)
            )
            return [
                SimpleNamespace(
                    name="测试好友",
                    content=getattr(message, "content", ""),
                    time=(
                        self._session_time
                        if self._session_time is not None
                        else str(len(self._messages))
                    ),
                    new_count=0,
                    isnew=False,
                )
            ]

    def SendMsg(self, **kwargs):
        self.sent.append(kwargs)
        return True

    def append(self, message) -> None:
        with self._lock:
            self._messages.append(message)

    def set_session_preview(self, message, *, marker_time: str) -> None:
        with self._lock:
            self._session_message = message
            self._session_time = marker_time


def wait_until(predicate, timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class WxAutoAdapterTests(unittest.TestCase):
    def test_normalizes_callback_and_sends_text(self) -> None:
        wechat = FakeWeChat()
        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("项目群",),
            bot_mention="@机器人",
            wechat_factory=lambda: wechat,
        )
        adapter.start(events.append)

        wechat.callback(
            SimpleNamespace(
                attr="friend",
                type="text",
                content="@机器人 帮我总结",
                sender="张三",
                hash="hash-1",
            ),
            FakeChat(),
        )
        action = GatewayAction(
            action_id="action-1",
            account_id="account-1",
            chat_id="项目群",
            chat_type="group",
            content_type="text",
            content="总结完成",
        )
        adapter.send(action)
        adapter.stop()

        self.assertEqual(wechat.listened, ["项目群"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].message_id, "wxauto:account-1:hash-1")
        self.assertEqual(events[0].sender_id, "张三")
        self.assertTrue(events[0].mentioned_bot)
        self.assertEqual(
            wechat.sent,
            [{"msg": "总结完成", "who": "项目群", "exact": True}],
        )
        self.assertTrue(wechat.stopped)

    def test_free_package_polls_only_messages_added_after_startup(self) -> None:
        old_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="old",
            sender="测试好友",
            hash="old-hash",
        )
        wechat = FakePollingWeChat(
            [old_message],
            empty_polls=4,
            volatile_hashes=True,
        )
        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.01,
            poll_load_wait_seconds=0.01,
            poll_baseline_seconds=0.03,
            wechat_factory=lambda: wechat,
        )
        adapter.start(events.append)
        try:
            time.sleep(0.04)
            self.assertEqual(events, [])

            wechat.append(
                SimpleNamespace(
                    attr="friend",
                    type="text",
                    content="old",
                    sender="测试好友",
                    hash="new-hash",
                )
            )
            self.assertTrue(wait_until(lambda: len(events) == 1))
            time.sleep(0.04)
            self.assertEqual(len(events), 1)
            self.assertTrue(
                events[0].message_id.startswith("wxauto:account-1:new-hash")
            )
            self.assertEqual(events[0].chat_id, "测试好友")
            self.assertFalse(events[0].is_self)
        finally:
            adapter.stop()

        calls_after_stop = wechat.poll_calls
        time.sleep(0.04)
        self.assertEqual(wechat.poll_calls, calls_after_stop)

    def test_history_recovery_uses_persisted_cursor(self) -> None:
        old_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="old",
            sender="测试好友",
            hash="old-hash",
        )
        wechat = FakePollingWeChat([old_message])
        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.01,
            poll_load_wait_seconds=0.01,
            poll_baseline_seconds=0,
            wechat_factory=lambda: wechat,
            cursor_getter=lambda chat_id: "old-hash",
            cursor_setter=lambda chat_id, cursor: None,
        )
        adapter.start(events.append)
        try:
            time.sleep(0.04)
            self.assertEqual(events, [])
            wechat.append(
                SimpleNamespace(
                    attr="friend",
                    type="text",
                    content="new",
                    sender="测试好友",
                    hash="new-hash",
                )
            )
            self.assertTrue(
                wait_until(lambda: any(event.content == "new" for event in events))
            )
            self.assertEqual([event.content for event in events], ["new"])
            self.assertEqual(events[0].message_id, "wxauto:account-1:new-hash")
        finally:
            adapter.stop()

    def test_history_recovery_emits_visible_messages_without_cursor(self) -> None:
        wechat = FakePollingWeChat(
            [
                SimpleNamespace(
                    attr="friend",
                    type="text",
                    content="offline message",
                    sender="测试好友",
                    hash="offline-hash",
                )
            ]
        )
        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.05,
            poll_load_wait_seconds=0.01,
            wechat_factory=lambda: wechat,
            cursor_getter=lambda chat_id: None,
            cursor_setter=lambda chat_id, cursor: None,
        )
        adapter.start(events.append)
        try:
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].content, "offline message")
        finally:
            adapter.stop()

    def test_navigation_alias_matches_wechat_brand_label(self) -> None:
        class FakeControlClass:
            def __init__(self, search_properties=None) -> None:
                self.searchProperties = search_properties or {}

            def _CompareFunction(self, control, depth):
                del depth
                return all(
                    getattr(control, name) == value
                    for name, value in self.searchProperties.items()
                )

        fake_uiautomation = SimpleNamespace(Control=FakeControlClass)
        _install_navigation_alias(fake_uiautomation)
        _install_navigation_alias(fake_uiautomation)

        search = FakeControlClass(
            {"ClassName": "mmui::XTabBarItem", "Name": "微信"}
        )
        actual = SimpleNamespace(
            ClassName="mmui::XTabBarItem",
            Name="WeChat",
        )
        self.assertTrue(search._CompareFunction(actual, 1))

        unrelated = FakeControlClass(
            {"ClassName": "Button", "Name": "微信"}
        )
        self.assertFalse(unrelated._CompareFunction(actual, 1))

    def test_polling_defers_preview_marker_until_hidden_snapshot_recovers(self) -> None:
        old_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="old",
            sender="测试好友",
            hash="old-hash",
        )
        new_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="new while minimized",
            sender="测试好友",
            hash="new-hash",
        )
        wechat = FakePollingWeChat([old_message])
        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.01,
            poll_load_wait_seconds=0.01,
            poll_baseline_seconds=0.03,
            wechat_factory=lambda: wechat,
        )
        adapter.start(events.append)
        try:
            calls_before_preview = wechat.poll_calls
            wechat.set_session_preview(new_message, marker_time="new")
            self.assertTrue(
                wait_until(lambda: wechat.poll_calls > calls_before_preview)
            )
            self.assertEqual(events, [])

            wechat.append(new_message)
            self.assertTrue(wait_until(lambda: len(events) == 1))
            time.sleep(0.04)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].content, "new while minimized")
        finally:
            adapter.stop()

    def test_polling_defers_seen_commit_until_preview_recovers(self) -> None:
        old_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="old",
            sender="测试好友",
            hash="old-hash",
        )
        new_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="new controls before preview",
            sender="测试好友",
            hash="new-hash",
        )
        wechat = FakePollingWeChat([old_message])
        wechat.set_session_preview(old_message, marker_time="old")
        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.01,
            poll_load_wait_seconds=0.01,
            poll_baseline_seconds=0.03,
            wechat_factory=lambda: wechat,
        )
        adapter.start(events.append)
        try:
            calls_before_snapshot = wechat.poll_calls
            wechat.append(new_message)
            self.assertTrue(
                wait_until(lambda: wechat.poll_calls > calls_before_snapshot)
            )
            self.assertEqual(events, [])

            wechat.set_session_preview(new_message, marker_time="new")
            self.assertTrue(wait_until(lambda: len(events) == 1))
            time.sleep(0.04)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].content, "new controls before preview")
        finally:
            adapter.stop()

    def test_polling_rejects_an_unexpected_chat(self) -> None:
        wechat = FakePollingWeChat([])
        wechat.ChatInfo = lambda: {
            "chat_name": "其他好友",
            "chat_type": "friend",
        }
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.01,
            poll_load_wait_seconds=0.01,
            poll_baseline_seconds=0,
            wechat_factory=lambda: wechat,
        )

        with self.assertRaisesRegex(
            WeChatAdapterError,
            "wxauto opened unexpected chat",
        ):
            adapter.start(lambda event: None)
        adapter.stop()

    def test_polling_reconnects_and_rebaselines_after_client_restart(self) -> None:
        old_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="old",
            sender="测试好友",
            hash="old-hash",
        )
        offline_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="arrived while client was offline",
            sender="测试好友",
            hash="offline-hash",
        )
        live_message = SimpleNamespace(
            attr="friend",
            type="text",
            content="after reconnect",
            sender="测试好友",
            hash="live-hash",
        )
        first = FakePollingWeChat([old_message])
        second = FakePollingWeChat([old_message, offline_message])
        clients = iter((first, second))
        factory_calls = []

        def factory():
            factory_calls.append(time.monotonic())
            return next(clients)

        events = []
        adapter = WxAutoAdapter(
            account_id="account-1",
            module_name="wxauto4",
            listen_chats=("测试好友",),
            bot_mention="",
            poll_interval_seconds=0.01,
            poll_load_wait_seconds=0.01,
            poll_baseline_seconds=0.03,
            poll_reconnect_min_seconds=0.01,
            poll_reconnect_max_seconds=0.04,
            wechat_factory=factory,
        )
        adapter.start(events.append)
        try:
            first.available = False
            self.assertTrue(wait_until(lambda: len(factory_calls) == 2))
            self.assertTrue(wait_until(lambda: second.poll_calls >= 3))
            self.assertEqual(events, [])

            second.append(live_message)
            self.assertTrue(wait_until(lambda: len(events) == 1))
            self.assertEqual(events[0].content, "after reconnect")

            action = GatewayAction(
                action_id="action-after-reconnect",
                account_id="account-1",
                chat_id="测试好友",
                chat_type="direct",
                content_type="text",
                content="reconnected reply",
            )
            adapter.send(action)
            self.assertEqual(
                second.sent,
                [
                    {
                        "msg": "reconnected reply",
                        "who": "测试好友",
                        "exact": True,
                    }
                ],
            )
        finally:
            adapter.stop()
