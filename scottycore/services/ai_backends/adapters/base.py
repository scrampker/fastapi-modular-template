"""ProviderAdapter Protocol — the contract every adapter must satisfy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from scottycore.services.ai_backends.schemas import AIProviderName
from scottycore.services.ai_backends.generation_schemas import GenerationRequest, GenerationResult


@runtime_checkable
class ProviderAdapter(Protocol):
    """Structural protocol for all AI provider adapters.

    Adapters are stateless (or lightly stateful for connection reuse) and must
    be safe for concurrent use. Each adapter wraps one provider's SDK or
    subprocess surface.
    """

    provider_name: AIProviderName

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Single-shot generation. Returns full result."""
        ...

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        """Streaming generation. Yields text chunks as they arrive.

        For adapters that don't support native streaming (e.g. claude_cli),
        this emits the full response as a single chunk.
        """
        ...

    async def health_check(self) -> tuple[bool, str]:
        """Optional override. Returns (reachable, detail_message).

        The default behaviour in the service is to use the URL reachability
        check from the health module. Adapters that have a cheaper or more
        accurate check (e.g. a /models endpoint) can override this.
        """
        ...
