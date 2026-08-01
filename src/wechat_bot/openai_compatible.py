from __future__ import annotations

import json
import logging
import socket
from threading import Lock
import time
from typing import Any, Protocol
import urllib.error
import urllib.request

from .domain import ModelRequest, ModelResponse
from .model_provider import ModelProviderError


logger = logging.getLogger(__name__)


class JsonTransport(Protocol):
    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]: ...


class UrllibJsonTransport:
    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return self._send(request, timeout)

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        return self._send(request, timeout)

    @staticmethod
    def _send(
        request: urllib.request.Request,
        timeout: float,
    ) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Provider error bodies are intentionally not copied into logs or the
            # durable retry database because some runtimes echo request content.
            raise ModelProviderError(f"LLM HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise ModelProviderError(f"LLM network error: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelProviderError("LLM returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelProviderError("LLM response must be a JSON object")
        return parsed


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible chat or text completion contracts."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 90,
        temperature: float = 0.3,
        max_output_tokens: int = 1024,
        top_p: float | None = None,
        repeat_penalty: float | None = None,
        request_format: str = "chat",
        backend_name: str = "openai_compatible",
        health_url: str | None = None,
        health_timeout_seconds: float = 2,
        health_cache_seconds: float = 5,
        transport: JsonTransport | None = None,
    ) -> None:
        if request_format not in {"chat", "rwkv_completion"}:
            raise ValueError("request_format must be chat or rwkv_completion")
        endpoint = "chat/completions" if request_format == "chat" else "completions"
        self._url = f"{base_url.rstrip('/')}/{endpoint}"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._top_p = top_p
        self._repeat_penalty = repeat_penalty
        self._request_format = request_format
        self._backend_name = backend_name
        self._health_url = health_url
        self._health_timeout = health_timeout_seconds
        self._health_cache_seconds = max(health_cache_seconds, 0)
        self._transport = transport or UrllibJsonTransport()
        self._health_lock = Lock()
        self._health_checked_at = 0.0
        self._health_status: dict[str, object] | None = None

    def complete(self, request: ModelRequest) -> ModelResponse:
        started_at = time.monotonic()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        if self._request_format == "chat":
            payload["messages"] = [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ]
        else:
            payload["prompt"] = self._format_rwkv_prompt(request)
            payload["stop"] = ["\n\nUser:", "\n\nSystem:"]
        if self._top_p is not None:
            payload["top_p"] = self._top_p
        if self._repeat_penalty is not None:
            payload["repeat_penalty"] = self._repeat_penalty
        try:
            response = self._transport.post_json(
                self._url,
                headers,
                payload,
                self._timeout,
            )
            text = self._extract_text(response)
        except Exception as exc:
            logger.warning(
                "model_request_failed backend=%s error_type=%s duration_ms=%s",
                self._backend_name,
                type(exc).__name__,
                round((time.monotonic() - started_at) * 1000),
            )
            raise
        usage = response.get("usage")
        logger.info(
            "model_request_completed backend=%s duration_ms=%s",
            self._backend_name,
            round((time.monotonic() - started_at) * 1000),
        )
        return ModelResponse(
            text=text,
            model=str(response.get("model") or self._model),
            usage=usage if isinstance(usage, dict) else {},
        )

    def health(self) -> dict[str, object]:
        if self._health_url is None:
            return {"backend": self._backend_name, "status": "not_checked"}

        now = time.monotonic()
        with self._health_lock:
            if (
                self._health_status is not None
                and now - self._health_checked_at < self._health_cache_seconds
            ):
                return dict(self._health_status)

            headers = {"Accept": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            try:
                self._transport.get_json(
                    self._health_url,
                    headers,
                    self._health_timeout,
                )
                status: dict[str, object] = {
                    "backend": self._backend_name,
                    "status": "ready",
                }
            except Exception as exc:
                status = {
                    "backend": self._backend_name,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
            self._health_checked_at = now
            self._health_status = status
            return dict(status)

    def _extract_text(self, response: dict[str, Any]) -> str:
        if self._request_format == "rwkv_completion":
            try:
                content = response["choices"][0]["text"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ModelProviderError("LLM response has no completion text") from exc
            text = content.strip() if isinstance(content, str) else ""
            if not text:
                raise ModelProviderError("LLM returned an empty completion")
            return text

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError("LLM response has no assistant message") from exc

        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        else:
            text = ""
        if not text:
            raise ModelProviderError("LLM returned an empty assistant message")
        return text

    @staticmethod
    def _format_rwkv_prompt(request: ModelRequest) -> str:
        parts: list[str] = []
        for message in request.messages:
            content = message.content.strip()
            if not content:
                continue
            if message.role == "system":
                label = "System"
            elif message.role == "user":
                label = "User"
            elif message.role == "assistant":
                label = "Assistant"
            else:
                raise ModelProviderError(f"Unsupported model message role: {message.role}")
            parts.append(f"{label}: {content}")
        parts.append("Assistant:")
        return "\n\n".join(parts)
