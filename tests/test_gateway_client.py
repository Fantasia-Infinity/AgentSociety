from __future__ import annotations

import unittest
import urllib.parse

from wechat_gateway.core_client import GatewayCoreClient
from wechat_gateway.domain import GatewayEvent


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []
        self.responses = [
            {"accepted": True, "reason": "queued"},
            {
                "actions": [
                    {
                        "action_id": "action-1",
                        "account_id": "account-1",
                        "chat_id": "user-1",
                        "chat_type": "direct",
                        "content_type": "text",
                        "content": "hello",
                    }
                ]
            },
            {"acked": 1},
        ]

    def request_json(self, method, url, headers, payload, timeout):
        self.calls.append((method, url, headers, payload, timeout))
        return self.responses.pop(0)


class GatewayCoreClientTests(unittest.TestCase):
    def test_event_poll_and_ack_protocol(self) -> None:
        transport = FakeTransport()
        client = GatewayCoreClient(
            base_url="https://core.example/",
            api_token="secret",
            account_id="account-1",
            timeout_seconds=10,
            transport=transport,
        )
        event = GatewayEvent(
            message_id="message-1",
            account_id="account-1",
            chat_id="user-1",
            sender_id="user-1",
            chat_type="direct",
            content_type="text",
            content="hi",
        )

        client.submit_event(event)
        actions = client.poll_actions(timeout_seconds=20, lease_seconds=60)
        acked = client.ack_actions([actions[0].action_id])

        self.assertEqual(acked, 1)
        self.assertEqual(actions[0].content, "hello")
        self.assertEqual(transport.calls[0][0:2], (
            "POST",
            "https://core.example/v1/events/wechat",
        ))
        self.assertEqual(
            transport.calls[0][2]["Authorization"], "Bearer secret"
        )
        poll_url = urllib.parse.urlparse(transport.calls[1][1])
        query = urllib.parse.parse_qs(poll_url.query)
        self.assertEqual(query["account_id"], ["account-1"])
        self.assertEqual(query["lease_seconds"], ["60"])
        self.assertEqual(
            transport.calls[2][3],
            {"account_id": "account-1", "action_ids": ["action-1"]},
        )
