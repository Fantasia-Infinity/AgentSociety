from __future__ import annotations

from .config import LlmEndpointSettings, Settings
from .model_provider import (
    ConcurrencyLimitedProvider,
    FailoverProvider,
    ModelProvider,
)
from .openai_compatible import OpenAICompatibleProvider


def _openai_provider(
    endpoint: LlmEndpointSettings,
    *,
    backend_name: str,
    request_format: str,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        model=endpoint.model,
        timeout_seconds=endpoint.timeout_seconds,
        temperature=endpoint.temperature,
        max_output_tokens=endpoint.max_output_tokens,
        top_p=endpoint.top_p,
        repeat_penalty=endpoint.repeat_penalty,
        request_format=request_format,
        backend_name=backend_name,
        health_url=endpoint.health_url,
        health_timeout_seconds=endpoint.health_timeout_seconds,
    )


def _limited_provider(
    endpoint: LlmEndpointSettings,
    *,
    backend_name: str,
    request_format: str = "chat",
) -> ModelProvider:
    provider = _openai_provider(
        endpoint,
        backend_name=backend_name,
        request_format=request_format,
    )
    return ConcurrencyLimitedProvider(
        provider,
        max_concurrency=endpoint.max_concurrency,
        backend_name=backend_name,
    )


def build_model_provider(settings: Settings) -> ModelProvider:
    if settings.llm_backend == "remote":
        if settings.remote_llm is None:
            raise ValueError("remote LLM settings are missing")
        return _limited_provider(settings.remote_llm, backend_name="remote")

    if settings.llm_backend == "local_rwkv":
        if settings.local_llm is None:
            raise ValueError("local LLM settings are missing")
        return _limited_provider(
            settings.local_llm,
            backend_name="local_rwkv",
            request_format="rwkv_completion",
        )

    if settings.llm_backend == "auto":
        if settings.local_llm is None or settings.remote_llm is None:
            raise ValueError("auto mode requires both local and remote LLM settings")
        return FailoverProvider(
            _limited_provider(
                settings.local_llm,
                backend_name="local_rwkv",
                request_format="rwkv_completion",
            ),
            _limited_provider(settings.remote_llm, backend_name="remote"),
            primary_name="local_rwkv",
            fallback_name="remote",
        )

    raise ValueError(f"unsupported LLM backend: {settings.llm_backend}")
