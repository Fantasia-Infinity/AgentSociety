from __future__ import annotations

import os
from unittest.mock import patch
import unittest

from wechat_bot.config import Settings


class SettingsTests(unittest.TestCase):
    def load(self, values: dict[str, str]) -> Settings:
        with (
            patch("wechat_bot.config._load_env_file"),
            patch.dict(os.environ, values, clear=True),
        ):
            return Settings.from_env()

    def test_remote_is_backward_compatible_default(self) -> None:
        settings = self.load(
            {
                "BOT_API_TOKEN": "test-token",
                "LLM_BASE_URL": "https://provider.example/v1",
                "LLM_MODEL": "remote-model",
            }
        )

        self.assertEqual(settings.llm_backend, "remote")
        self.assertEqual(settings.remote_llm.model, "remote-model")
        self.assertIsNone(settings.local_llm)

    def test_local_mode_does_not_require_remote_credentials(self) -> None:
        settings = self.load(
            {
                "BOT_API_TOKEN": "test-token",
                "LLM_BACKEND": "local_rwkv",
            }
        )

        self.assertIsNone(settings.remote_llm)
        self.assertEqual(settings.local_llm.model, "rwkv-local")
        self.assertEqual(
            settings.local_llm.base_url,
            "http://127.0.0.1:18080/v1",
        )
        self.assertEqual(
            settings.local_llm.health_url,
            "http://127.0.0.1:18080/health",
        )
        self.assertEqual(settings.local_llm.max_concurrency, 1)
        self.assertEqual(settings.local_llm.temperature, 1.0)
        self.assertEqual(settings.local_llm.top_p, 0.5)
        self.assertEqual(settings.local_llm.repeat_penalty, 1.2)

    def test_auto_requires_remote_settings(self) -> None:
        with self.assertRaisesRegex(ValueError, "LLM_BASE_URL"):
            self.load(
                {
                    "BOT_API_TOKEN": "test-token",
                    "LLM_BACKEND": "auto",
                }
            )

    def test_rejects_unknown_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "LLM_BACKEND"):
            self.load(
                {
                    "BOT_API_TOKEN": "test-token",
                    "LLM_BACKEND": "mystery",
                }
            )

    def test_rejects_invalid_local_concurrency(self) -> None:
        with self.assertRaisesRegex(ValueError, "LOCAL_LLM_MAX_CONCURRENCY"):
            self.load(
                {
                    "BOT_API_TOKEN": "test-token",
                    "LLM_BACKEND": "local_rwkv",
                    "LOCAL_LLM_MAX_CONCURRENCY": "0",
                }
            )

    def test_rejects_non_loopback_local_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "LOCAL_LLM_BASE_URL.*loopback"):
            self.load(
                {
                    "BOT_API_TOKEN": "test-token",
                    "LLM_BACKEND": "local_rwkv",
                    "LOCAL_LLM_BASE_URL": "https://provider.example/v1",
                }
            )

    def test_rejects_non_loopback_local_health_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "LOCAL_LLM_HEALTH_URL.*loopback"):
            self.load(
                {
                    "BOT_API_TOKEN": "test-token",
                    "LLM_BACKEND": "local_rwkv",
                    "LOCAL_LLM_HEALTH_URL": "https://provider.example/health",
                }
            )


if __name__ == "__main__":
    unittest.main()
