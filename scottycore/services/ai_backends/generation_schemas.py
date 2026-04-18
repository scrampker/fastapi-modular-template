"""Generation-time schemas — extend the core ai_backends schemas.

These are kept separate from schemas.py so the base config/status schemas
(imported by the settings service and health check paths) stay lightweight
and SDK-free.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from scottycore.services.ai_backends.schemas import AIProviderName


class Message(BaseModel):
    """A single chat message.

    MVP: content is a plain string.
    Future: content may become list[ContentBlock] for multi-modal support.
    """

    role: Literal["user", "assistant", "system"]
    content: str


class GenerationRequest(BaseModel):
    """Unified generation request passed to every adapter.

    Either `prompt` (shorthand for a single user message) or `messages` must
    be provided.  Both can be set; `prompt` is then appended as a final user
    message after any messages in the list.
    """

    prompt: str | None = Field(None, description="Shorthand for messages=[user: prompt]")
    messages: list[Message] = Field(default_factory=list)
    system: str | None = None
    max_tokens: int = Field(4096, gt=0)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    stream: bool = False
    provider: AIProviderName | Literal["auto"] = "auto"
    tenant_id: str | None = None

    @model_validator(mode="after")
    def _require_content(self) -> "GenerationRequest":
        if not self.prompt and not self.messages:
            raise ValueError("Either 'prompt' or 'messages' must be provided")
        return self


class GenerationResult(BaseModel):
    """Returned by every adapter after a successful generation."""

    text: str
    finish_reason: str = "end_turn"
    usage: dict | None = None
    provider_used: AIProviderName
    # Chain visibility: each element is (provider_tried, failure_reason)
    attempted: list[tuple[AIProviderName, str]] = Field(default_factory=list)


class AIBackendsError(Exception):
    """Raised when all providers in the fallback chain have been exhausted.

    `.attempts` contains the (provider, reason) pairs from each attempt.
    """

    def __init__(self, message: str = "All AI providers failed", attempts: list | None = None) -> None:
        super().__init__(message)
        self.attempts: list[tuple[AIProviderName, str]] = attempts or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.attempts:
            detail = "; ".join(f"{p.value}: {r}" for p, r in self.attempts)
            return f"{base} — [{detail}]"
        return base


def _messages_from_request(request: GenerationRequest) -> list[Message]:
    """Flatten prompt + messages into a canonical message list.

    The `prompt` shorthand is appended as a final user message after any
    explicit messages, so callers can mix history + a new prompt cleanly.
    """
    msgs = list(request.messages)
    if request.prompt:
        msgs.append(Message(role="user", content=request.prompt))
    return msgs
