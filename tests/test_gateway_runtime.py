from __future__ import annotations

import io
import logging
from pathlib import Path
import tempfile
import time
import unittest

from wechat_gateway.adapters.mock import MockWeChatAdapter
from wechat_gateway.domain import GatewayAction, GatewayEvent
from wechat_gateway.runtime import GatewayRuntime
from wechat_gateway.state import SentActionStore


class FakeCoreClient:
    def __init__(self, action: GatewayAction) -> None:
        self.action = action
        self.events = []
        self.ack_calls = 0
        self.acked = False

    def submit_event(self, event):
        self.events.append(event)

    def poll_actions(self, *, timeout_seconds, lease_seconds):
        if self.acked:
            time.sleep(0.005)
            return []
        return [self.action]

    def ack_actions(self, action_ids):
        self.ack_calls += 1
        if self.ack_calls == 1:
            raise RuntimeError("simulated lost ACK")
        self.acked = True
        return 1


def wait_until(predicate, timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class GatewayRuntimeTests(unittest.TestCase):
    def test_uploads_events_and_does_not_resend_after_lost_ack(self) -> None:
        action = GatewayAction(
            action_id="action-1",
            account_id="account-1",
            chat_id="user-1",
            chat_type="direct",
            content_type="text",
            content="reply",
        )
        client = FakeCoreClient(action)
        adapter = MockWeChatAdapter(
            account_id="account-1",
            output_stream=io.StringIO(),
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime_logger = logging.getLogger("wechat_gateway.runtime")
            previous_disabled = runtime_logger.disabled
            runtime_logger.disabled = True
            runtime = GatewayRuntime(
                adapter=adapter,
                client=client,
                sent_actions=SentActionStore(Path(directory) / "state.sqlite3"),
                event_queue_size=10,
                poll_timeout_seconds=0.01,
                action_lease_seconds=5,
                retry_min_seconds=0.01,
                retry_max_seconds=0.02,
            )
            runtime.start()
            try:
                adapter.emit(
                    GatewayEvent(
                        message_id="message-1",
                        account_id="account-1",
                        chat_id="user-1",
                        sender_id="user-1",
                        chat_type="direct",
                        content_type="text",
                        content="hello",
                    )
                )
                self.assertTrue(wait_until(lambda: len(client.events) == 1))
                self.assertTrue(wait_until(lambda: client.acked))
                self.assertEqual(len(adapter.sent_actions), 1)
                self.assertGreaterEqual(client.ack_calls, 2)
            finally:
                runtime.stop()
                runtime_logger.disabled = previous_disabled

    def test_sent_action_store_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SentActionStore(path)
            store.mark_sent("action-1")
            store.close()
            reopened = SentActionStore(path)
            try:
                self.assertTrue(reopened.was_sent("action-1"))
            finally:
                reopened.close()
