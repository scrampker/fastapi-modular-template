"""OpenAI SDK adapter."""

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


class OpenAIAdapter:
    """Adapter for the OpenAI API using the openai Python SDK.

    Pass sdk_client for testing (must be openai.AsyncOpenAI-compatible).
    """

    provider_name: AIProviderName = AIProviderName.OPENAI

    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o",
        sdk_client: object | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._sdk_client = sdk_client

    def _get_client(self) -> object:
        if self._sdk_client is not None:
            return self._sdk_client
        try:
            from openai import AsyncOpenAI  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("openai package not installed") from exc
        kwargs: dict = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return AsyncOpenAI(**kwargs)

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
                "openai generate: model=%s tokens_out=%s elapsed=%.2fs",
                self._model,
                usage.get("completion_tokens") if usage else "?",
                elapsed,
            )
            return GenerationResult(
                text=text,
                finish_reason=finish_reason,
                usage=usage,
                provider_used=AIProviderName.OPENAI,
                attempted=[],
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning("openai generate failed after %.2fs: %s", elapsed, exc)
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
            logger.warning("openai stream failed: %s", exc)
            raise

    async def health_check(self) -> tuple[bool, str]:
        import os
        has_key = bool(self._api_key or os.environ.get("OPENAI_API_KEY", ""))
        return has_key, "API key configured" if has_key else "no API key"
