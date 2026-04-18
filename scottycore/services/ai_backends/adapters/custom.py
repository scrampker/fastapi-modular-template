"""Custom / generic OpenAI-compatible adapter (vLLM, TGI, LiteLLM, etc.)."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from scottycore.services.ai_backends.schemas import AIProviderName
from scottycore.services.ai_backends.generation_schemas import (
    GenerationRequest,
    GenerationResult,
    _messages_from_request,
)

logger = logging.getLogger(__name__)


class CustomAdapter:
    """Adapter for any OpenAI-compatible API endpoint.

    Uses AsyncOpenAI with a base_url override. Useful for vLLM, TGI,
    LiteLLM proxy, and any other OpenAI-spec server.
    Pass sdk_client for testing.
    """

    provider_name: AIProviderName = AIProviderName.CUSTOM

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "not-required",
        model: str = "",
        sdk_client: object | None = None,
        timeout: int = 120,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._sdk_client = sdk_client
        self._timeout = timeout

    def _get_client(self) -> object:
        if self._sdk_client is not None:
            return self._sdk_client
        try:
            from openai import AsyncOpenAI  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("openai package not installed") from exc
        if not self._base_url:
            raise RuntimeError("custom adapter requires a base_url")
        return AsyncOpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
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
                "custom generate: url=%s model=%s elapsed=%.2fs",
                self._base_url,
                self._model,
                elapsed,
            )
            return GenerationResult(
                text=text,
                finish_reason=finish_reason,
                usage=usage,
                provider_used=AIProviderName.CUSTOM,
                attempted=[],
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning("custom generate failed after %.2fs: %s", elapsed, exc)
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
            logger.warning("custom stream failed: %s", exc)
            raise

    async def health_check(self) -> tuple[bool, str]:
        if not self._base_url:
            return False, "no base_url configured"
        return True, f"configured: {self._base_url[:60]}"
