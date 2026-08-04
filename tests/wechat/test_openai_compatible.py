from __future__ import annotations

import unittest

from wechat_core.domain import ModelMessage, ModelRequest
from wechat_core.model_provider import ModelProviderError
from wechat_core.openai_compatible import OpenAICompatibleProvider


class FakeTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple] = []
        self.health_calls: list[tuple] = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        return self.response

    def get_json(self, url, headers, timeout):
        self.health_calls.append((url, headers, timeout))
        return {"status": "ok"}


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_builds_rwkv_completion_prompt_with_history(self) -> None:
        transport = FakeTransport(
            {
                "model": "rwkv-local",
                "choices": [{"text": " 本地回答 "}],
            }
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:18080/v1",
            api_key="",
            model="rwkv-local",
            request_format="rwkv_completion",
            transport=transport,
        )

        response = provider.complete(
            ModelRequest(
                conversation_id="conversation-1",
                messages=(
                    ModelMessage(role="system", content="系统提示"),
                    ModelMessage(role="user", content="上一问"),
                    ModelMessage(role="assistant", content="上一答"),
                    ModelMessage(role="user", content="这一问"),
                ),
            )
        )

        self.assertEqual(response.text, "本地回答")
        url, headers, payload, timeout = transport.calls[0]
        self.assertEqual(url, "http://127.0.0.1:18080/v1/completions")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(
            payload["prompt"],
            "System: 系统提示\n\nUser: 上一问\n\nAssistant: 上一答\n\n"
            "User: 这一问\n\nAssistant:",
        )
        self.assertEqual(payload["stop"], ["\n\nUser:", "\n\nSystem:"])

    def test_builds_chat_completion_request_and_parses_text(self) -> None:
        transport = FakeTransport(
            {
                "model": "remote-model-v1",
                "choices": [{"message": {"content": " 你好！ "}}],
                "usage": {"total_tokens": 12},
            }
        )
        provider = OpenAICompatibleProvider(
            base_url="https://provider.example/v1/",
            api_key="secret",
            model="chat-model",
            timeout_seconds=15,
            temperature=0.2,
            max_output_tokens=300,
            transport=transport,
        )

        response = provider.complete(
            ModelRequest(
                conversation_id="conversation-1",
                messages=(ModelMessage(role="user", content="你好"),),
            )
        )

        self.assertEqual(response.text, "你好！")
        self.assertEqual(response.model, "remote-model-v1")
        self.assertEqual(response.usage["total_tokens"], 12)
        url, headers, payload, timeout = transport.calls[0]
        self.assertEqual(url, "https://provider.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(payload["model"], "chat-model")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "你好"}])
        self.assertEqual(payload["max_tokens"], 300)
        self.assertEqual(timeout, 15)

    def test_rejects_missing_assistant_message(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.example/v1",
            api_key="",
            model="chat-model",
            transport=FakeTransport({"choices": []}),
        )
        with self.assertRaises(ModelProviderError):
            provider.complete(ModelRequest(messages=(), conversation_id="c"))

    def test_local_health_is_cached_and_does_not_require_key(self) -> None:
        transport = FakeTransport({"choices": []})
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:18080/v1",
            api_key="",
            model="rwkv-local",
            backend_name="local_rwkv",
            health_url="http://127.0.0.1:18080/health",
            health_cache_seconds=60,
            transport=transport,
        )

        first = provider.health()
        second = provider.health()

        self.assertEqual(first, {"backend": "local_rwkv", "status": "ready"})
        self.assertEqual(second, first)
        self.assertEqual(len(transport.health_calls), 1)
        self.assertNotIn("Authorization", transport.health_calls[0][1])


if __name__ == "__main__":
    unittest.main()
