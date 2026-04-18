"""Claude CLI adapter — shells out to `claude -p` using host auth.

No API key needed. Streaming is not natively supported; stream() emits the
full response as one chunk after the subprocess completes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import AsyncIterator

from scottycore.services.ai_backends.schemas import AIProviderName
from scottycore.services.ai_backends.generation_schemas import (
    GenerationRequest,
    GenerationResult,
    _messages_from_request,
)

logger = logging.getLogger(__name__)

# Max characters of input sent over stdin. CLI has context limits too, but
# we guard here against runaway system + user prompts hitting a pipe limit.
_MAX_STDIN_CHARS = 200_000


def _build_stdin_prompt(request: GenerationRequest) -> str:
    """Flatten messages + optional system prompt into a single stdin string."""
    parts: list[str] = []
    if request.system:
        parts.append(f"<system>\n{request.system}\n</system>")
    for msg in _messages_from_request(request):
        parts.append(f"<{msg.role}>\n{msg.content}\n</{msg.role}>")
    combined = "\n\n".join(parts)
    if len(combined) > _MAX_STDIN_CHARS:
        combined = combined[:_MAX_STDIN_CHARS]
    return combined


class ClaudeCliAdapter:
    """Adapter for `claude -p` subprocess.

    Accepts an optional `_run_fn` callable for unit testing — it must have
    the same signature as asyncio.create_subprocess_exec and return a
    subprocess with (stdout, stderr) pipes.
    """

    provider_name: AIProviderName = AIProviderName.CLAUDE_CLI

    def __init__(
        self,
        model: str = "opus",
        timeout: int = 120,
        _run_fn: object | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._run_fn = _run_fn  # override for tests

    async def _exec(self, stdin_text: str) -> str:
        cmd = ["claude", "-p", "--model", self._model]
        t0 = time.monotonic()
        run_fn = self._run_fn or asyncio.create_subprocess_exec
        proc = await run_fn(  # type: ignore[operator]
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_text.encode("utf-8")),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude CLI timed out after {self._timeout}s")

        elapsed = time.monotonic() - t0
        rc = proc.returncode
        if rc != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            logger.warning("claude_cli failed (rc=%s) after %.2fs: %s", rc, elapsed, err)
            raise RuntimeError(f"claude CLI exited with code {rc}: {err}")

        text = stdout.decode("utf-8", errors="replace").strip()
        logger.info("claude_cli generate: model=%s len=%d elapsed=%.2fs", self._model, len(text), elapsed)
        return text

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        stdin_text = _build_stdin_prompt(request)
        text = await self._exec(stdin_text)
        return GenerationResult(
            text=text,
            finish_reason="end_turn",
            usage=None,
            provider_used=AIProviderName.CLAUDE_CLI,
            attempted=[],
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        # CLI has no streaming; emit the full response as one chunk
        stdin_text = _build_stdin_prompt(request)
        text = await self._exec(stdin_text)

        async def _single_chunk() -> AsyncIterator[str]:
            yield text

        return _single_chunk()

    async def health_check(self) -> tuple[bool, str]:
        available = shutil.which("claude") is not None
        return available, "claude binary found" if available else "claude binary not in PATH"
