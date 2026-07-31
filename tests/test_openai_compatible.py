from __future__ import annotations

import unittest

from wechat_bot.domain import ModelMessage, ModelRequest
from wechat_bot.model_provider import ModelProviderError
from wechat_bot.openai_compatible import OpenAICompatibleProvider


class FakeTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple] = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        return self.response


class OpenAICompatibleProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

