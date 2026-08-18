from __future__ import annotations

import time
import unittest

from agent_hub.tunnel import TunnelRegistry


class TunnelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = TunnelRegistry(ticket_ttl_seconds=60)

    def test_ticket_is_one_time_and_scoped_to_node(self) -> None:
        ticket = self.registry.issue_ticket("node-a")
        self.assertEqual(self.registry.consume_ticket(ticket), "node-a")
        # Second consumption fails.
        self.assertIsNone(self.registry.consume_ticket(ticket))

    def test_ticket_expires(self) -> None:
        self.registry = TunnelRegistry(ticket_ttl_seconds=0)
        ticket = self.registry.issue_ticket("node-a")
        time.sleep(0.01)
        self.assertIsNone(self.registry.consume_ticket(ticket))

    def test_attach_detach_and_send(self) -> None:
        received: list[dict] = []
        close = lambda: None  # noqa: E731
        self.registry.attach("node-a", received.append, close)
        self.assertTrue(self.registry.is_online("node-a"))
        self.assertTrue(self.registry.send_to("node-a", {"type": "ping"}))
        self.assertEqual(received, [{"type": "ping"}])
        self.registry.detach("node-a", close)
        self.assertFalse(self.registry.is_online("node-a"))

    def test_send_to_offline_node_fails(self) -> None:
        self.assertFalse(self.registry.send_to("node-a", {"type": "ping"}))

    def test_reattach_closes_previous_tunnel(self) -> None:
        closed: list[bool] = []
        self.registry.attach("node-a", lambda message: None, lambda: closed.append(True))
        self.registry.attach("node-a", lambda message: None, lambda: None)
        self.assertEqual(closed, [True])

    def test_send_failure_reports_offline(self) -> None:
        def broken(message: dict) -> None:
            raise RuntimeError("send failed")

        self.registry.attach("node-a", broken, lambda: None)
        self.assertFalse(self.registry.send_to("node-a", {"type": "ping"}))


if __name__ == "__main__":
    unittest.main()
