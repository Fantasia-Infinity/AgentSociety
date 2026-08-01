from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time
import unittest

from wechat_bot.domain import ModelRequest, ModelResponse
from wechat_bot.model_provider import (
    ConcurrencyLimitedProvider,
    FailoverProvider,
    ModelProviderError,
)


REQUEST = ModelRequest(messages=(), conversation_id="conversation-1")


class StubProvider:
    def __init__(self, *, text: str = "ok", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ModelResponse(text=self.text)

    def health(self):
        return {"status": "ready"}


class TrackingProvider:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = Lock()

    def complete(self, request):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return ModelResponse(text="ok")


class ModelProviderRoutingTests(unittest.TestCase):
    def test_failover_uses_remote_for_provider_error(self) -> None:
        primary = StubProvider(error=ModelProviderError("local unavailable"))
        fallback = StubProvider(text="remote reply")
        provider = FailoverProvider(
            primary,
            fallback,
            primary_name="local_rwkv",
            fallback_name="remote",
        )

        response = provider.complete(REQUEST)

        self.assertEqual(response.text, "remote reply")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_failover_does_not_hide_programming_errors(self) -> None:
        primary = StubProvider(error=ValueError("bug"))
        fallback = StubProvider(text="remote reply")
        provider = FailoverProvider(
            primary,
            fallback,
            primary_name="local_rwkv",
            fallback_name="remote",
        )

        with self.assertRaises(ValueError):
            provider.complete(REQUEST)
        self.assertEqual(fallback.calls, 0)

    def test_local_concurrency_is_bounded(self) -> None:
        inner = TrackingProvider()
        provider = ConcurrencyLimitedProvider(
            inner,
            max_concurrency=1,
            backend_name="local_rwkv",
        )

        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda _: provider.complete(REQUEST), range(4)))

        self.assertEqual([item.text for item in responses], ["ok"] * 4)
        self.assertEqual(inner.max_active, 1)


if __name__ == "__main__":
    unittest.main()
