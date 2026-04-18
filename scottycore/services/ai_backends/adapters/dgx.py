"""DGX adapter — same logic as Ollama, different default endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator

from scottycore.services.ai_backends.schemas import AIProviderName
from scottycore.services.ai_backends.generation_schemas import GenerationRequest, GenerationResult
from scottycore.services.ai_backends.adapters.ollama import OllamaAdapter


class DgxAdapter(OllamaAdapter):
    """Adapter for the DGX Spark / remote Ollama endpoint.

    Inherits all OllamaAdapter logic; the only difference is the default
    URL and model, matching BASELINE_ENDPOINTS for the dgx provider.
    """

    provider_name: AIProviderName = AIProviderName.DGX

    def __init__(
        self,
        url: str = "http://192.168.150.111:11434",
        model: str = "qwen3.5:35b-a3b",
        http_client: object | None = None,
        timeout: int = 180,
    ) -> None:
        super().__init__(url=url, model=model, http_client=http_client, timeout=timeout)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        result = await super().generate(request)
        # Re-stamp provider so callers see dgx, not ollama
        return result.model_copy(update={"provider_used": AIProviderName.DGX})

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        return await super().stream(request)
