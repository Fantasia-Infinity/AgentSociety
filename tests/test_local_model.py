from __future__ import annotations

from pathlib import Path
import plistlib
import tempfile
import unittest

from wechat_bot.local_model import LocalModelRuntimeSettings


class LocalModelRuntimeTests(unittest.TestCase):
    def test_launchagent_template_is_persistent(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "deploy/macos/com.fantasia.wechat-bot-local-llm.plist.example"
        )
        with path.open("rb") as handle:
            template = plistlib.load(handle)

        self.assertTrue(template["RunAtLoad"])
        self.assertTrue(template["KeepAlive"])
        self.assertEqual(
            template["Label"],
            "com.fantasia.wechat-bot-local-llm",
        )

    def test_builds_loopback_llama_server_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "llama-server"
            executable.touch(mode=0o700)
            model = root / "rwkv.gguf"
            model.touch()
            settings = LocalModelRuntimeSettings(
                executable=executable,
                model_path=model,
                host="127.0.0.1",
                port=18080,
                alias="rwkv-local",
                context_size=4096,
                parallel=1,
                threads=8,
                gpu_layers=99,
                chat_template="rwkv-world",
            )

            settings.validate()

            self.assertEqual(
                settings.command(),
                [
                    str(executable),
                    "-m",
                    str(model),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18080",
                    "--alias",
                    "rwkv-local",
                    "-c",
                    "4096",
                    "-np",
                    "1",
                    "-t",
                    "8",
                    "-ngl",
                    "99",
                    "--chat-template",
                    "rwkv-world",
                    "--cors-origins",
                    "localhost",
                    "--no-webui",
                ],
            )

    def test_rejects_non_loopback_bind(self) -> None:
        settings = LocalModelRuntimeSettings(
            executable=Path("/missing"),
            model_path=Path("/missing"),
            host="0.0.0.0",
            port=18080,
            alias="rwkv-local",
            context_size=4096,
            parallel=1,
            threads=8,
            gpu_layers=99,
            chat_template="rwkv-world",
        )

        with self.assertRaisesRegex(ValueError, "loopback"):
            settings.validate()


if __name__ == "__main__":
    unittest.main()
