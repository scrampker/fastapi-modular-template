"""Azure OpenAI adapter."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator

from scottycore.services.ai_backends.schemas import AIProviderName
from scottycore.services.ai_backends.generation_schemas import (
    GenerationRequest,
    GenerationResult,
    _messages_from_request,
)

logger = logging.getLogger(__name__)


class AzureOpenAIAdapter:
    """Adapter for Azure OpenAI Service using AsyncAzureOpenAI.

    Requires endpoint URL and API key. The deployment name is used as the model.
    Pass sdk_client for testing.
    """

    provider_name: AIProviderName = AIProviderName.AZURE_OPENAI

    def __init__(
        self,
        endpoint: str = "",
        api_key: str = "",
        model: str = "gpt-4o",
        api_version: str = "2024-02-01",
        sdk_client: object | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._api_version = api_version
        self._sdk_client = sdk_client

    def _get_client(self) -> object:
        if self._sdk_client is not None:
            return self._sdk_client
        try:
            from openai import AsyncAzureOpenAI  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("openai package not installed") from exc

        endpoint = self._endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        api_key = self._api_key or os.environ.get("AZURE_OPENAI_KEY", "")
        if not endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT not configured")
        return AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=self._api_version,
        )

    def _build_messages(self, request: GenerationRequest) -> list[dict]:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in _messages_from_request(request):
            messages.append({"role": msg.role, "content": msg.content})
        return messages

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        client = self._get_client()
        t0 = time.monotonic()
        try:
            response = await client.chat.completions.create(  # type: ignore[union-attr]
                model=self._model,
                messages=self._build_messages(request),
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
            elapsed = time.monotonic() - t0
            choice = response.choices[0]
            text = choice.message.content or ""
            finish_reason = choice.finish_reason or "stop"
            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                }
            logger.info(
                "azure_openai generate: model=%s tokens_out=%s elapsed=%.2fs",
                self._model,
                usage.get("completion_tokens") if usage else "?",
                elapsed,
            )
            return GenerationResult(
                text=text,
                finish_reason=finish_reason,
                usage=usage,
                provider_used=AIProviderName.AZURE_OPENAI,
                attempted=[],
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning("azure_openai generate failed after %.2fs: %s", elapsed, exc)
            raise

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        client = self._get_client()
        try:
            stream = await client.chat.completions.create(  # type: ignore[union-attr]
                model=self._model,
                messages=self._build_messages(request),
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield delta.content
        except Exception as exc:
            logger.warning("azure_openai stream failed: %s", exc)
            raise

    async def health_check(self) -> tuple[bool, str]:
        endpoint = self._endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        api_key = self._api_key or os.environ.get("AZURE_OPENAI_KEY", "")
        if not endpoint or not api_key:
            return False, "endpoint or API key not configured"
        return True, f"endpoint configured: {endpoint[:40]}"
