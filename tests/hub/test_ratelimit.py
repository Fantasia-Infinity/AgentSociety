from __future__ import annotations

import unittest

from agent_hub.ratelimit import AuthRateLimiter, SlidingWindowLimiter
from agent_hub.server import forwarded_client_ip


class SlidingWindowLimiterTests(unittest.TestCase):
    def test_allows_up_to_limit_then_denies(self) -> None:
        limiter = SlidingWindowLimiter(2, 60)
        self.assertTrue(limiter.allow("ip"))
        self.assertTrue(limiter.allow("ip"))
        self.assertFalse(limiter.allow("ip"))
        self.assertTrue(limiter.allow("other"))

    def test_window_expiry_releases_quota(self) -> None:
        now = [100.0]
        limiter = SlidingWindowLimiter(1, 60, clock=lambda: now[0])
        self.assertTrue(limiter.allow("ip"))
        self.assertFalse(limiter.allow("ip"))
        now[0] = 160.0
        self.assertTrue(limiter.allow("ip"))

    def test_rejects_non_positive_parameters(self) -> None:
        with self.assertRaises(ValueError):
            SlidingWindowLimiter(0, 60)
        with self.assertRaises(ValueError):
            SlidingWindowLimiter(1, 0)


class AuthRateLimiterTests(unittest.TestCase):
    def test_register_consumes_auth_quota(self) -> None:
        limiter = AuthRateLimiter(auth_per_minute=1, register_per_hour=10)
        self.assertTrue(limiter.allow_register("ip"))
        self.assertFalse(limiter.allow_auth("ip"))

    def test_disabled_always_allows(self) -> None:
        limiter = AuthRateLimiter(enabled=False)
        self.assertTrue(limiter.allow_auth("ip"))
        self.assertTrue(limiter.allow_register("ip"))


class ForwardedClientIpTests(unittest.TestCase):
    def test_prefers_first_forwarded_hop(self) -> None:
        self.assertEqual(
            forwarded_client_ip(
                {"X-Forwarded-For": "203.0.113.7, 10.0.0.2"},
                ("127.0.0.1", 1234),
            ),
            "203.0.113.7",
        )
        self.assertEqual(
            forwarded_client_ip({}, ("127.0.0.1", 1234)),
            "127.0.0.1",
        )
        self.assertEqual(forwarded_client_ip({}, None), "unknown")


if __name__ == "__main__":
    unittest.main()
