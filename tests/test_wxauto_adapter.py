from __future__ import annotations

from types import SimpleNamespace
import unittest

from wechat_gateway.adapters.wxauto import WxAutoAdapter
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
