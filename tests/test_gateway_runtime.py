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
from wechat_gateway.state import GatewayInboxStore, SentActionStore


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
    def test_inbox_persists_events_and_upload_status(self) -> None:
        event = GatewayEvent(
            message_id="durable-message-1",
            account_id="account-1",
            chat_id="user-1",
            sender_id="user-1",
            chat_type="direct",
            content_type="text",
            content="hello",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            inbox = GatewayInboxStore(path)
            self.assertTrue(inbox.insert(event))
            self.assertFalse(inbox.insert(event))
            self.assertEqual(inbox.status(event.message_id), "pending")
            claimed = inbox.claim_pending()
            self.assertEqual(claimed, [event])
            inbox.mark_uploaded(event.message_id)
            inbox.close()

            reopened = GatewayInboxStore(path)
            try:
                self.assertEqual(reopened.status(event.message_id), "uploaded")
                self.assertEqual(reopened.pending_count(), 0)
            finally:
                reopened.close()

    def test_durable_uploader_retries_after_core_reconnects(self) -> None:
        class FlakyCoreClient(FakeCoreClient):
            def __init__(self, action):
                super().__init__(action)
                self.failures = 2

            def submit_event(self, event):
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("core offline")
                super().submit_event(event)

        action = GatewayAction(
            action_id="action-durable-1",
            account_id="account-1",
            chat_id="user-1",
            chat_type="direct",
            content_type="text",
            content="reply",
        )
        client = FlakyCoreClient(action)
        adapter = MockWeChatAdapter(account_id="account-1", output_stream=io.StringIO())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            inbox = GatewayInboxStore(path)
            runtime = GatewayRuntime(
                adapter=adapter,
                client=client,
                sent_actions=SentActionStore(path),
                inbox=inbox,
                event_queue_size=10,
                poll_timeout_seconds=0.01,
                action_lease_seconds=5,
                retry_min_seconds=0.01,
                retry_max_seconds=0.02,
            )
            runtime.start()
            try:
                event = GatewayEvent(
                    message_id="durable-message-2",
                    account_id="account-1",
                    chat_id="user-1",
                    sender_id="user-1",
                    chat_type="direct",
                    content_type="text",
                    content="hello",
                )
                adapter.emit(event)
                self.assertTrue(wait_until(lambda: len(client.events) == 1))
                self.assertEqual(inbox.status(event.message_id), "uploaded")
            finally:
                runtime.stop()

    def test_durable_ingest_advances_chat_cursor(self) -> None:
        action = GatewayAction(
            action_id="action-cursor-1",
            account_id="account-1",
            chat_id="user-1",
            chat_type="direct",
            content_type="text",
            content="reply",
        )
        client = FakeCoreClient(action)
        adapter = MockWeChatAdapter(account_id="account-1", output_stream=io.StringIO())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            inbox = GatewayInboxStore(path)
            runtime = GatewayRuntime(
                adapter=adapter,
                client=client,
                sent_actions=SentActionStore(path),
                inbox=inbox,
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
                        message_id="cursor-message-1",
                        account_id="account-1",
                        chat_id="user-1",
                        sender_id="user-1",
                        chat_type="direct",
                        content_type="text",
                        content="hello",
                        metadata={"source_key": "source-1"},
                    )
                )
                self.assertTrue(
                    wait_until(lambda: inbox.get_cursor("account-1", "user-1") == "source-1")
                )
            finally:
                runtime.stop()

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
