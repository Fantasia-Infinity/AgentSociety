from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil

from .config import _load_env_file


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalModelRuntimeSettings:
    executable: Path
    model_path: Path
    host: str
    port: int
    alias: str
    context_size: int
    parallel: int
    threads: int
    gpu_layers: int
    chat_template: str

    @classmethod
    def from_env(cls) -> "LocalModelRuntimeSettings":
        _load_env_file()
        executable_value = os.environ.get("LOCAL_LLM_EXECUTABLE", "llama-server").strip()
        resolved_executable = shutil.which(executable_value)
        executable = Path(resolved_executable or executable_value).expanduser()
        model_path = Path(
            os.environ.get("LOCAL_LLM_MODEL_PATH", "").strip()
        ).expanduser()
        settings = cls(
            executable=executable,
            model_path=model_path,
            host=os.environ.get("LOCAL_LLM_BIND_HOST", "127.0.0.1").strip(),
            port=int(os.environ.get("LOCAL_LLM_PORT", "18080")),
            alias=os.environ.get("LOCAL_LLM_MODEL", "rwkv-local").strip(),
            context_size=int(os.environ.get("LOCAL_LLM_CONTEXT_SIZE", "4096")),
            parallel=int(os.environ.get("LOCAL_LLM_MAX_CONCURRENCY", "1")),
            threads=int(os.environ.get("LOCAL_LLM_THREADS", "8")),
            gpu_layers=int(os.environ.get("LOCAL_LLM_GPU_LAYERS", "99")),
            chat_template=os.environ.get(
                "LOCAL_LLM_CHAT_TEMPLATE", "rwkv-world"
            ).strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("LOCAL_LLM_BIND_HOST must be a loopback address")
        if not 1 <= self.port <= 65535:
            raise ValueError("LOCAL_LLM_PORT must be between 1 and 65535")
        if not self.alias:
            raise ValueError("LOCAL_LLM_MODEL cannot be empty")
        if self.context_size < 128:
            raise ValueError("LOCAL_LLM_CONTEXT_SIZE must be at least 128")
        if self.parallel < 1:
            raise ValueError("LOCAL_LLM_MAX_CONCURRENCY must be at least 1")
        if self.threads < 1:
            raise ValueError("LOCAL_LLM_THREADS must be at least 1")
        if self.gpu_layers < 0:
            raise ValueError("LOCAL_LLM_GPU_LAYERS cannot be negative")
        if not self.chat_template:
            raise ValueError("LOCAL_LLM_CHAT_TEMPLATE cannot be empty")
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise ValueError("LOCAL_LLM_EXECUTABLE is missing or not executable")
        if not self.model_path.is_file():
            raise ValueError("LOCAL_LLM_MODEL_PATH must point to a GGUF model file")

    def command(self) -> list[str]:
        return [
            str(self.executable),
            "-m",
            str(self.model_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--alias",
            self.alias,
            "-c",
            str(self.context_size),
            "-np",
            str(self.parallel),
            "-t",
            str(self.threads),
            "-ngl",
            str(self.gpu_layers),
            "--chat-template",
            self.chat_template,
            "--cors-origins",
            "localhost",
            "--no-webui",
        ]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = LocalModelRuntimeSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Local model configuration error: {exc}") from exc

    logger.info(
        "local_model_starting host=%s port=%s alias=%s parallel=%s",
        settings.host,
        settings.port,
        settings.alias,
        settings.parallel,
    )
    os.execv(settings.executable, settings.command())


if __name__ == "__main__":
    main()
