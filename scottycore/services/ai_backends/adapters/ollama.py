"""Ollama adapter — POST /api/chat with /api/generate fallback."""

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


def _build_chat_payload(request: GenerationRequest, model: str) -> dict:
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for msg in _messages_from_request(request):
        messages.append({"role": msg.role, "content": msg.content})
    return {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "num_predict": request.max_tokens,
            "temperature": request.temperature,
        },
    }


def _build_generate_payload(request: GenerationRequest, model: str) -> dict:
    """Fallback for older Ollama versions that don't have /api/chat."""
    parts = []
    if request.system:
        parts.append(request.system)
    for msg in _messages_from_request(request):
        parts.append(f"{msg.role}: {msg.content}")
    return {
        "model": model,
        "prompt": "\n\n".join(parts),
        "stream": False,
        "options": {
            "num_predict": request.max_tokens,
            "temperature": request.temperature,
        },
    }


class OllamaAdapter:
    """Adapter for a local or remote Ollama instance.

    Tries /api/chat first (preferred, supports structured messages).
    Falls back to /api/generate if /api/chat returns 404.

    Pass http_client for testing (must be httpx.AsyncClient-compatible).
    """

    provider_name: AIProviderName = AIProviderName.OLLAMA

    def __init__(
        self,
        url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        http_client: object | None = None,
        timeout: int = 120,
    ) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._http_client = http_client
        self._timeout = timeout

    def _get_client(self) -> object:
        if self._http_client is not None:
            return self._http_client
        try:
            import httpx  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("httpx package not installed") from exc
        return httpx.AsyncClient(timeout=self._timeout)

    async def _post(self, client: object, path: str, payload: dict) -> dict:
        resp = await client.post(f"{self._url}{path}", json=payload)  # type: ignore[union-attr]
        resp.raise_for_status()
        return resp.json()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        own_client = self._http_client is None
        client = self._get_client()
        t0 = time.monotonic()
        text = ""
        finish_reason = "stop"
        usage: dict | None = None

        try:
            payload = _build_chat_payload(request, self._model)
            try:
                data = await self._post(client, "/api/chat", payload)
                msg = data.get("message", {})
                text = msg.get("content", "")
                # Thinking models (qwen3.x etc.) sometimes put the answer in
                # message.thinking when content is empty or whitespace-only.
                # Prefer content when it has substantive text; fall back to
                # thinking only when content is blank/whitespace.
                if not text.strip() and msg.get("thinking"):
                    text = msg["thinking"]
                finish_reason = data.get("done_reason", "stop")
                usage = {
                    "prompt_eval_count": data.get("prompt_eval_count"),
                    "eval_count": data.get("eval_count"),
                }
            except Exception as chat_err:
                # Try /api/generate as fallback
                logger.debug("ollama /api/chat failed (%s), trying /api/generate", chat_err)
                gen_payload = _build_generate_payload(request, self._model)
                data = await self._post(client, "/api/generate", gen_payload)
                text = data.get("response", "")
                finish_reason = data.get("done_reason", "stop")

            elapsed = time.monotonic() - t0
            logger.info("ollama generate: model=%s len=%d elapsed=%.2fs", self._model, len(text), elapsed)
            return GenerationResult(
                text=text,
                finish_reason=finish_reason,
                usage=usage,
                provider_used=AIProviderName.OLLAMA,
                attempted=[],
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning("ollama generate failed after %.2fs: %s", elapsed, exc)
            raise
        finally:
            if own_client and hasattr(client, "aclose"):
                await client.aclose()  # type: ignore[union-attr]

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        # Full response as one chunk — Ollama streaming requires ndjson parsing
        # which adds complexity; deferred to a future iteration.
        result = await self.generate(request)

        async def _single() -> AsyncIterator[str]:
            yield result.text

        return _single()

    async def health_check(self) -> tuple[bool, str]:
        client = self._get_client()
        own = self._http_client is None
        try:
            resp = await client.get(f"{self._url}/api/tags")  # type: ignore[union-attr]
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", [])
            names = ", ".join(m.get("name", "?") for m in models[:3])
            return True, f"models: {names}" if names else "reachable"
        except Exception as exc:
            return False, str(exc)[:100]
        finally:
            if own and hasattr(client, "aclose"):
                await client.aclose()  # type: ignore[union-attr]
