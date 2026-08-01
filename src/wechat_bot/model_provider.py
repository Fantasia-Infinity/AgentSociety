from __future__ import annotations

import logging
from threading import BoundedSemaphore
from typing import Protocol

from .domain import ModelRequest, ModelResponse


logger = logging.getLogger(__name__)


class ModelProviderError(RuntimeError):
    """A model request failed or returned an invalid response."""


class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate one assistant response for a normalized request."""
        ...


def provider_health(provider: ModelProvider) -> dict[str, object]:
    health = getattr(provider, "health", None)
    if not callable(health):
        return {"status": "unknown"}
    try:
        result = health()
    except Exception as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}
    if not isinstance(result, dict):
        return {"status": "unknown"}
    return result


class ConcurrencyLimitedProvider:
    """Serialize or cap calls to a resource-constrained model runtime."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_concurrency: int,
        backend_name: str,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._provider = provider
        self._semaphore = BoundedSemaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._backend_name = backend_name

    def complete(self, request: ModelRequest) -> ModelResponse:
        with self._semaphore:
            return self._provider.complete(request)

    def health(self) -> dict[str, object]:
        status = provider_health(self._provider)
        return {
            **status,
            "backend": self._backend_name,
            "max_concurrency": self._max_concurrency,
        }


class FailoverProvider:
    """Use the fallback only for normalized provider failures.

    Selecting this provider is an explicit privacy decision because the fallback
    may be remote and therefore receive the same conversation content.
    """

    def __init__(
        self,
        primary: ModelProvider,
        fallback: ModelProvider,
        *,
        primary_name: str,
        fallback_name: str,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            return self._primary.complete(request)
        except ModelProviderError as exc:
            logger.warning(
                "model_provider_failover primary=%s fallback=%s error_type=%s",
                self._primary_name,
                self._fallback_name,
                type(exc).__name__,
            )
            return self._fallback.complete(request)

    def health(self) -> dict[str, object]:
        primary = provider_health(self._primary)
        fallback = provider_health(self._fallback)
        primary_status = str(primary.get("status", "unknown"))
        overall = "ready" if primary_status == "ready" else "degraded"
        return {
            "backend": "auto",
            "status": overall,
            "primary": primary,
            "fallback": fallback,
        }
