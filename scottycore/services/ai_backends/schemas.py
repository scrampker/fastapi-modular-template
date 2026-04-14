"""AI Backends service contract — schemas only.

Defines the configuration and status schemas for multi-provider AI
connectivity. Every ScottyCore app should be able to talk to any of
these backends with zero code changes — just configuration.

Supported providers:
  - claude_cli:   Claude Code CLI (`claude -p`), uses host auth, no API key needed
  - claude_api:   Anthropic SDK direct, needs API key or Claude Code OAuth token
  - ollama:       Local Ollama instance (GPU node), self-hosted models
  - dgx:          Remote DGX/Ollama endpoint, larger models, higher throughput
  - openai:       OpenAI API (GPT-4o, o1, etc.)
  - azure_openai: Azure OpenAI Service (enterprise, region-specific)
  - custom:       Any OpenAI-compatible API (vLLM, TGI, LiteLLM, etc.)
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AIProviderName(str, Enum):
    """Supported AI backend providers."""
    CLAUDE_CLI = "claude_cli"
    CLAUDE_API = "claude_api"
    OLLAMA = "ollama"
    DGX = "dgx"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    CUSTOM = "custom"


class EndpointConfig(BaseModel):
    """Configuration for a single AI endpoint."""
    provider: AIProviderName
    url: str = ""
    model: str = ""
    api_key: str = ""  # write-only in API responses, masked in GET
    enabled: bool = True
    context_window: int = Field(0, description="Max context in tokens. 0 = use provider default.")
    priority: int = Field(0, description="Lower = higher priority in auto-resolve. 0 = default.")
    notes: str = ""


class EndpointHealth(BaseModel):
    """Health check result for a single endpoint."""
    provider: AIProviderName
    url: str
    reachable: bool
    model: str = ""
    detail: str = ""
    gpu_name: str | None = None
    gpu_vram_mb: int | None = None


class AIBackendConfig(BaseModel):
    """Full AI backend configuration for an app.

    The `default_provider` is used when a caller doesn't specify a preference.
    The `fallback_chain` defines the resolution order when the preferred
    provider is unreachable: try each in order, use first that responds.
    """
    default_provider: AIProviderName = AIProviderName.CLAUDE_CLI
    fallback_chain: list[AIProviderName] = Field(
        default_factory=lambda: [
            AIProviderName.DGX,
            AIProviderName.OLLAMA,
            AIProviderName.CLAUDE_CLI,
        ],
        description="Resolution order: try each until one responds.",
    )
    endpoints: dict[AIProviderName, EndpointConfig] = Field(default_factory=dict)


class AIBackendStatus(BaseModel):
    """Current status of all configured AI backends."""
    active_provider: AIProviderName | None = None
    endpoints: list[EndpointHealth] = Field(default_factory=list)
    claude_cli_available: bool = False


# ── Baseline defaults for new app deployments ─────────────────────────────

BASELINE_ENDPOINTS: dict[AIProviderName, EndpointConfig] = {
    AIProviderName.CLAUDE_CLI: EndpointConfig(
        provider=AIProviderName.CLAUDE_CLI,
        model="opus",
        enabled=True,
        context_window=200_000,
        priority=30,
        notes="Uses host Claude Code auth. No API key needed. Always available.",
    ),
    AIProviderName.DGX: EndpointConfig(
        provider=AIProviderName.DGX,
        url="http://192.168.150.111:11434",
        model="qwen3.5:35b-a3b",
        enabled=True,
        context_window=131_072,
        priority=10,
        notes="DGX Spark on homelab. MoE model: 3B active params, ~55-70 tok/s.",
    ),
    AIProviderName.OLLAMA: EndpointConfig(
        provider=AIProviderName.OLLAMA,
        url="http://192.168.150.210:11434",
        model="qwen2.5:7b",
        enabled=True,
        context_window=32_768,
        priority=20,
        notes="Local Ollama on CT 114 (GPU passthrough).",
    ),
    AIProviderName.CLAUDE_API: EndpointConfig(
        provider=AIProviderName.CLAUDE_API,
        model="claude-sonnet-4-6",
        enabled=False,
        context_window=200_000,
        priority=40,
        notes="Anthropic SDK direct. Needs API key or Claude Code OAuth token.",
    ),
    AIProviderName.OPENAI: EndpointConfig(
        provider=AIProviderName.OPENAI,
        url="https://api.openai.com/v1",
        model="gpt-4o",
        enabled=False,
        context_window=128_000,
        priority=50,
        notes="OpenAI API. Needs OPENAI_API_KEY.",
    ),
    AIProviderName.AZURE_OPENAI: EndpointConfig(
        provider=AIProviderName.AZURE_OPENAI,
        enabled=False,
        context_window=128_000,
        priority=50,
        notes="Azure OpenAI Service. Needs AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_KEY.",
    ),
}
