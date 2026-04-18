"""Anthropic SDK adapter — direct API or Claude Code OAuth token."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

from scottycore.services.ai_backends.schemas import AIProviderName
from scottycore.services.ai_backends.generation_schemas import (
    GenerationRequest,
    GenerationResult,
    _messages_from_request,
)

logger = logging.getLogger(__name__)

_CLAUDE_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"


def _read_claude_code_token() -> str | None:
    """Read and optionally refresh the OAuth token from Claude Code's credentials file."""
    try:
        if not _CLAUDE_CREDS_PATH.exists():
            return None
        data = json.loads(_CLAUDE_CREDS_PATH.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken", "")
        if not token or not token.startswith("sk-ant-"):
            return None

        expires_at = oauth.get("expiresAt", 0)
        now_ms = time.time() * 1000
        if expires_at and now_ms > expires_at - 120_000:
            refreshed = _try_refresh_token(data)
            if refreshed:
                return refreshed
        return token
    except Exception:
        return None


def _try_refresh_token(creds_data: dict) -> str | None:
    """Attempt OAuth token refresh. Returns new token on success, None on failure."""
    import urllib.request

    oauth = creds_data.get("claudeAiOauth", {})
    refresh_token = oauth.get("refreshToken", "")
    if not refresh_token:
        return None

    try:
        req_data = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://console.anthropic.com/v1/oauth/token",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        new_access = result.get("access_token", "")
        if not new_access or not new_access.startswith("sk-ant-"):
            return None

        oauth["accessToken"] = new_access
        oauth["refreshToken"] = result.get("refresh_token", refresh_token)
        oauth["expiresAt"] = int(time.time() * 1000) + result.get("expires_in", 3600) * 1000
        creds_data["claudeAiOauth"] = oauth
        _CLAUDE_CREDS_PATH.write_text(json.dumps(creds_data), encoding="utf-8")
        return new_access
    except Exception as exc:
        logger.warning("Claude Code token refresh failed: %s", exc)
        return None


class ClaudeApiAdapter:
    """Adapter for the Anthropic Python SDK (claude_api provider).

    Key resolution order:
      1. api_key argument (from EndpointConfig)
      2. ANTHROPIC_API_KEY env var
      3. Claude Code OAuth token from ~/.claude/.credentials.json
    """

    provider_name: AIProviderName = AIProviderName.CLAUDE_API

    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-sonnet-4-6",
        sdk_client: object | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # Injected for testing; None = build from resolved key at call time
        self._sdk_client = sdk_client

    def _resolve_key(self) -> str | None:
        import os
        if self._api_key:
            return self._api_key
        env = os.environ.get("ANTHROPIC_API_KEY", "")
        if env:
            return env
        return _read_claude_code_token()

    def _get_client(self) -> object:
        if self._sdk_client is not None:
            return self._sdk_client
        # Lazy import so missing anthropic package doesn't break module load
        try:
            from anthropic import AsyncAnthropic  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("anthropic package not installed") from exc

        key = self._resolve_key()
        # If no key found, let AsyncAnthropic try ANTHROPIC_API_KEY from environment itself
        kwargs: dict = {}
        if key:
            kwargs["api_key"] = key
        return AsyncAnthropic(**kwargs)

    def _build_messages(self, request: GenerationRequest) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in _messages_from_request(request)]

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        import time as _time
        client = self._get_client()
        model = self._model
        messages = self._build_messages(request)
        kwargs: dict = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.system:
            kwargs["system"] = request.system
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        t0 = _time.monotonic()
        try:
            response = await client.messages.create(**kwargs)  # type: ignore[union-attr]
            elapsed = _time.monotonic() - t0
            text = response.content[0].text if response.content else ""
            usage = None
            if hasattr(response, "usage") and response.usage:
                usage = {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }
            logger.info(
                "claude_api generate: model=%s tokens_out=%s elapsed=%.2fs",
                model,
                usage.get("output_tokens") if usage else "?",
                elapsed,
            )
            return GenerationResult(
                text=text,
                finish_reason=response.stop_reason or "end_turn",
                usage=usage,
                provider_used=AIProviderName.CLAUDE_API,
                attempted=[],
            )
        except Exception as exc:
            elapsed = _time.monotonic() - t0
            logger.warning("claude_api generate failed after %.2fs: %s", elapsed, exc)
            raise

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        client = self._get_client()
        model = self._model
        messages = self._build_messages(request)
        kwargs: dict = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.system:
            kwargs["system"] = request.system
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            async with client.messages.stream(**kwargs) as stream:  # type: ignore[union-attr]
                async for chunk in stream.text_stream:
                    yield chunk
        except Exception as exc:
            logger.warning("claude_api stream failed: %s", exc)
            raise

    async def health_check(self) -> tuple[bool, str]:
        key = self._resolve_key()
        if not key:
            return False, "no API key available"
        return True, "API key configured"
