from __future__ import annotations

from typing import Protocol

from .domain import ModelRequest, ModelResponse


class ModelProviderError(RuntimeError):
    """A model request failed or returned an invalid response."""


class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate one assistant response for a normalized request."""
        ...

