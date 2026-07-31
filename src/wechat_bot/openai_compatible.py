from __future__ import annotations

import json
import socket
from typing import Any, Protocol
import urllib.error
import urllib.request

from .domain import ModelRequest, ModelResponse
from .model_provider import ModelProviderError


class JsonTransport(Protocol):
    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
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
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ModelProviderError(f"LLM HTTP {exc.code}: {detail}") from exc
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
    """Remote provider using the OpenAI-compatible chat completions contract."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 90,
        temperature: float = 0.3,
        max_output_tokens: int = 1024,
        transport: JsonTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._transport = transport or UrllibJsonTransport()

    def complete(self, request: ModelRequest) -> ModelResponse:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        response = self._transport.post_json(
            self._url,
            headers,
            payload,
            self._timeout,
        )
        text = self._extract_text(response)
        usage = response.get("usage")
        return ModelResponse(
            text=text,
            model=str(response.get("model") or self._model),
            usage=usage if isinstance(usage, dict) else {},
        )

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
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

