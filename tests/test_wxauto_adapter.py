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
        self._lock = Lock()
        self.current_chat = ""
        self.poll_calls = 0
        self.sent = []

    def ChatWith(self, who, exact=True):
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
            message = self._messages[-1] if self._messages else None
            return [
                SimpleNamespace(
                    name="测试好友",
                    content=getattr(message, "content", ""),
                    time=str(len(self._messages)),
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
