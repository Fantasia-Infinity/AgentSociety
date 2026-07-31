from __future__ import annotations

import unittest
import urllib.parse

from wechat_bot.api import parse_action_ack_payload, parse_action_poll_query


class BotApiTests(unittest.TestCase):
    def test_action_poll_accepts_lease(self) -> None:
        query = urllib.parse.urlencode(
            {"account_id": "account-1", "timeout": 0, "lease_seconds": 45}
        )
        self.assertEqual(
            parse_action_poll_query(query),
            ("account-1", 0.0, 45.0),
        )

    def test_action_ack_endpoint(self) -> None:
        self.assertEqual(
            parse_action_ack_payload(
                {"account_id": "account-1", "action_ids": ["a-1", "a-2"]}
            ),
            ("account-1", ["a-1", "a-2"]),
        )
