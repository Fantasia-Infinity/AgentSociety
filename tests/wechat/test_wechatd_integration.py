from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from threading import Thread
import unittest
import urllib.error
import urllib.request

from wechatd.adapters.mock import MockWeChatAdapter
from wechatd.domain import GatewayEvent
from wechatd.main import build_adapter
from wechatd.runtime import WechatdRuntime
from wechatd.server import WechatdHttpServer
from wechatd.state import SentActionStore, WechatdStore

from agent_channel.service import HttpChannelService


def _request_json(base_url: str, method: str, path: str, payload=None, token: str | None = None):
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url}{path}", data=body, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _make_event(
    account_id: str = "account-1",
    chat_id: str = "测试好友",
    content: str = "你好",
    message_id: str = "msg-1",
) -> GatewayEvent:
    return GatewayEvent(
        message_id=message_id,
        account_id=account_id,
        chat_id=chat_id,
        sender_id=chat_id,
        chat_type="direct",
        content_type="text",
        content=content,
    )


class WechatdIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        state_db = Path(self._tmp.name) / "wechatd-state.sqlite3"
        account_id = "account-1"
        adapter = MockWeChatAdapter(
            account_id=account_id,
            interactive=False,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
        )
        self._adapter = adapter
        self._runtime = WechatdRuntime(
            account_id=account_id,
            adapter=adapter,
            store=WechatdStore(state_db),
            sent_actions=SentActionStore(state_db),
            send_min_interval_seconds=0,
        )
        self._server = WechatdHttpServer(
            ("127.0.0.1", 0),
            self._runtime,
            "",
            65536,
        )
        self._port = self._server.server_address[1]
        self._base_url = f"http://127.0.0.1:{self._port}"
        self._runtime.start()
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._runtime.stop()
        self._tmp.cleanup()

    def test_health_requires_no_token(self) -> None:
        payload = _request_json(self._base_url, "GET", "/health")
        self.assertEqual(payload["status"], "ok")

    def test_event_archives_and_reads_back(self) -> None:
        self._adapter.emit(_make_event())
        payload = _request_json(
            self._base_url, "GET", f"/v1/messages?chat_id={urllib.parse.quote('测试好友')}"
        )
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["content"], "你好")
        self.assertEqual(payload["next_cursor"], "msg-1")

    def test_messages_support_after_message_id(self) -> None:
        self._adapter.emit(_make_event(message_id="msg-1", content="first"))
        self._adapter.emit(_make_event(message_id="msg-2", content="second"))
        payload = _request_json(
            self._base_url,
            "GET",
            f"/v1/messages?chat_id={urllib.parse.quote('测试好友')}&after_message_id=msg-1",
        )
        self.assertEqual([m["content"] for m in payload["messages"]], ["second"])

    def test_messages_support_before_timestamp(self) -> None:
        event = _make_event(message_id="msg-1", content="old")
        self._adapter.emit(event)
        future = event.timestamp + 100
        payload = _request_json(
            self._base_url,
            "GET",
            f"/v1/messages?chat_id={urllib.parse.quote('测试好友')}&before_timestamp={future}",
        )
        self.assertEqual(len(payload["messages"]), 1)
        self.assertIsNone(payload["next_cursor"])

    def test_chats_listing(self) -> None:
        self._adapter.emit(_make_event(chat_id="甲", content="from a", message_id="m-a"))
        self._adapter.emit(_make_event(chat_id="乙", content="from b", message_id="m-b"))
        payload = _request_json(self._base_url, "GET", "/v1/chats")
        chats = payload["chats"]
        self.assertEqual({c["chat_id"] for c in chats}, {"甲", "乙"})

    def test_agent_cursor_roundtrip(self) -> None:
        chat_id = urllib.parse.quote("测试好友")
        before = _request_json(self._base_url, "GET", f"/v1/agent_cursor?chat_id={chat_id}")
        self.assertIsNone(before["cursor"])
        result = _request_json(
            self._base_url,
            "PUT",
            "/v1/agent_cursor",
            {"chat_id": "测试好友", "cursor": "msg-1"},
        )
        self.assertEqual(result["cursor"], "msg-1")
        after = _request_json(self._base_url, "GET", f"/v1/agent_cursor?chat_id={chat_id}")
        self.assertEqual(after["cursor"], "msg-1")

    def test_send_and_idempotency(self) -> None:
        payload = {"chat_id": "测试好友", "content": "回复", "idempotency_key": "key-1"}
        first = _request_json(self._base_url, "POST", "/v1/send", payload)
        self.assertTrue(first["sent"])
        second = _request_json(self._base_url, "POST", "/v1/send", payload)
        self.assertFalse(second["sent"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self._adapter.sent_actions), 1)
        self.assertEqual(self._adapter.sent_actions[0].content, "回复")

    def test_send_requires_content(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request_json(
                self._base_url, "POST", "/v1/send", {"chat_id": "测试好友", "content": "  "}
            )
        self.assertEqual(ctx.exception.code, 400)

    def test_status_endpoint(self) -> None:
        payload = _request_json(self._base_url, "GET", "/v1/status")
        self.assertTrue(payload["started"])
        self.assertEqual(payload["adapter"]["driver"], "mock")


class WechatdTokenTests(unittest.TestCase):
    def test_token_rejects_unauthenticated_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_db = Path(tmp) / "state.sqlite3"
            adapter = MockWeChatAdapter(
                account_id="account-1", interactive=False,
                input_stream=io.StringIO(), output_stream=io.StringIO(),
            )
            runtime = WechatdRuntime(
                account_id="account-1",
                adapter=adapter,
                store=WechatdStore(state_db),
                sent_actions=SentActionStore(state_db),
                send_min_interval_seconds=0,
            )
            server = WechatdHttpServer(("127.0.0.1", 0), runtime, "secret", 65536)
            port = server.server_address[1]
            base_url = f"http://127.0.0.1:{port}"
            runtime.start()
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    _request_json(base_url, "GET", "/v1/status")
                self.assertEqual(ctx.exception.code, 401)
                payload = _request_json(base_url, "GET", "/v1/status", token="secret")
                self.assertTrue(payload["started"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                runtime.stop()


class HttpChannelServiceIntegrationTests(unittest.TestCase):
    """agent_channel MCP service talking to a live wechatd over HTTP."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        state_db = Path(self._tmp.name) / "wechatd-state.sqlite3"
        self._adapter = MockWeChatAdapter(
            account_id="account-1",
            interactive=False,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
        )
        self._runtime = WechatdRuntime(
            account_id="account-1",
            adapter=self._adapter,
            store=WechatdStore(state_db),
            sent_actions=SentActionStore(state_db),
            send_min_interval_seconds=0,
        )
        self._server = WechatdHttpServer(
            ("127.0.0.1", 0), self._runtime, "", 65536
        )
        self._port = self._server.server_address[1]
        self._runtime.start()
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._service = HttpChannelService(base_url=f"http://127.0.0.1:{self._port}")

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._runtime.stop()
        self._tmp.cleanup()

    def test_full_agent_flow(self) -> None:
        status = self._service.status()
        self.assertTrue(status["started"])

        self._adapter.emit(_make_event(message_id="in-1", content="在吗"))

        messages = self._service.read_messages(
            account_id="account-1", conversation_id="测试好友"
        )
        self.assertEqual([m["content"] for m in messages], ["在吗"])

        conversations = self._service.list_conversations(account_id="account-1")
        self.assertEqual([c["conversation_id"] for c in conversations], ["测试好友"])

        result = self._service.send(
            account_id="account-1", conversation_id="测试好友", content="我在"
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self._adapter.sent_actions[0].content, "我在")

        reply = self._service.reply(
            account_id="account-1", message_id="in-1", content="回复你"
        )
        self.assertEqual(reply["conversation_id"], "测试好友")
        self.assertEqual(self._adapter.sent_actions[-1].content, "回复你")

    def test_read_advances_cursor(self) -> None:
        self._adapter.emit(_make_event(message_id="in-1", content="第一条"))
        first = self._service.read_messages(
            account_id="account-1", conversation_id="测试好友"
        )
        self.assertEqual(len(first), 1)
        self._adapter.emit(_make_event(message_id="in-2", content="第二条"))
        second = self._service.read_messages(
            account_id="account-1", conversation_id="测试好友"
        )
        self.assertEqual([m["content"] for m in second], ["第二条"])

    def test_unavailable_service_raises(self) -> None:
        unreachable = HttpChannelService(base_url="http://127.0.0.1:1")
        with self.assertRaises(RuntimeError):
            unreachable.status()


if __name__ == "__main__":
    unittest.main()
