# scottycore-pattern: ai_backends.multi_provider
"""AI Backends service — provider resolution, health checks, and endpoint management.

Extracted from ScottyScribe's battle-tested multi-backend AI system.
Provides a framework-agnostic service that any ScottyCore app can use
to route AI requests across multiple providers with automatic fallback.

The service does NOT own a database session — endpoint config is loaded
from environment variables and .env files. Apps that want persistent
per-tenant endpoint config can store it in the settings service and
pass it here at runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from typing import TYPE_CHECKING

from app.services.ai_backends.schemas import (
    AIBackendConfig,
    AIBackendStatus,
    AIProviderName,
    BASELINE_ENDPOINTS,
    EndpointConfig,
    EndpointHealth,
)

# Cache for endpoint health checks (avoids hammering endpoints)
_health_cache: dict[str, tuple[bool, float]] = {}
_HEALTH_TTL = 30  # seconds


def _check_url_reachable(url: str, path: str = "/api/tags", timeout: int = 3) -> tuple[bool, str]:
    """Check if an HTTP endpoint is reachable. Returns (ok, detail)."""
    cache_key = f"{url}{path}"
    now = time.time()
    cached = _health_cache.get(cache_key)
    if cached and (now - cached[1]) < _HEALTH_TTL:
        return cached[0], "cached"

    try:
        req = urllib.request.Request(f"{url.rstrip('/')}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            _health_cache[cache_key] = (True, now)
            try:
                parsed = json.loads(data)
                models = parsed.get("models", [])
                if models:
                    names = ", ".join(m.get("name", "?") for m in models[:3])
                    return True, f"models: {names}"
            except (json.JSONDecodeError, AttributeError):
                pass
            return True, "reachable"
    except Exception as e:
        _health_cache[cache_key] = (False, now)
        return False, str(e)[:100]


def _check_claude_cli() -> bool:
    """Check if the Claude CLI is available on the host."""
    return shutil.which("claude") is not None


def _detect_local_gpu() -> tuple[str | None, int | None]:
    """Detect local NVIDIA GPU via nvidia-smi. Returns (name, vram_mb) or (None, None)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=5, stderr=subprocess.DEVNULL,
        ).decode().strip()
        parts = [p.strip() for p in out.split(",")]
        if len(parts) >= 2:
            return parts[0], int(parts[1])
    except Exception:
        pass
    return None, None


class AIBackendsService:
    """Multi-provider AI backend management.

    This service is stateless — it reads config from the provided
    AIBackendConfig and checks endpoint health on demand. Apps wire
    it into their service registry and optionally persist config
    via the settings service.
    """

    def __init__(self, config: AIBackendConfig | None = None) -> None:
        self._config = config or self._build_default_config()

    @property
    def config(self) -> AIBackendConfig:
        return self._config

    def update_config(self, config: AIBackendConfig) -> None:
        """Replace the current config (e.g. after loading from settings store)."""
        self._config = config

    @staticmethod
    def _build_default_config() -> AIBackendConfig:
        """Build config from BASELINE_ENDPOINTS + environment overrides."""
        import os
        endpoints = dict(BASELINE_ENDPOINTS)

        # Environment overrides (same keys ScottyScribe uses)
        env_map = {
            "OLLAMA_URL": (AIProviderName.OLLAMA, "url"),
            "OLLAMA_MODEL_CHAT": (AIProviderName.OLLAMA, "model"),
            "DGX_OLLAMA_URL": (AIProviderName.DGX, "url"),
            "DGX_OLLAMA_MODEL": (AIProviderName.DGX, "model"),
            "OPENAI_API_KEY": (AIProviderName.OPENAI, "api_key"),
            "AZURE_OPENAI_ENDPOINT": (AIProviderName.AZURE_OPENAI, "url"),
            "AZURE_OPENAI_KEY": (AIProviderName.AZURE_OPENAI, "api_key"),
            "ANTHROPIC_API_KEY": (AIProviderName.CLAUDE_API, "api_key"),
        }

        for env_key, (provider, field) in env_map.items():
            value = os.environ.get(env_key, "")
            if value:
                ep = endpoints.get(provider)
                if ep:
                    endpoints[provider] = ep.model_copy(update={field: value})
                    # Auto-enable if a key/url was provided
                    if field in ("api_key", "url"):
                        endpoints[provider] = endpoints[provider].model_copy(update={"enabled": True})

        return AIBackendConfig(
            default_provider=AIProviderName.CLAUDE_CLI,
            fallback_chain=[
                AIProviderName.DGX,
                AIProviderName.OLLAMA,
                AIProviderName.CLAUDE_CLI,
            ],
            endpoints=endpoints,
        )

    def resolve_provider(
        self,
        requested: AIProviderName | str = "auto",
    ) -> AIProviderName:
        """Resolve the effective AI provider to use.

        Args:
            requested: Explicit provider name, or "auto" to use the fallback chain.

        Returns the first reachable provider, or claude_cli as last resort.
        """
        # Explicit request — use it if reachable, otherwise fall through chain
        if requested != "auto":
            provider = AIProviderName(requested) if isinstance(requested, str) else requested
            if provider == AIProviderName.CLAUDE_CLI:
                return provider
            ep = self._config.endpoints.get(provider)
            if ep and ep.enabled and ep.url:
                ok, _ = _check_url_reachable(ep.url)
                if ok:
                    return provider
            # Fall through to chain

        # Auto-resolve: walk the fallback chain
        for provider in self._config.fallback_chain:
            ep = self._config.endpoints.get(provider)
            if not ep or not ep.enabled:
                continue

            if provider == AIProviderName.CLAUDE_CLI:
                if _check_claude_cli():
                    return provider
                continue

            if ep.url:
                ok, _ = _check_url_reachable(ep.url)
                if ok:
                    return provider

        # Last resort
        return AIProviderName.CLAUDE_CLI

    def get_endpoint(self, provider: AIProviderName) -> EndpointConfig | None:
        """Get the endpoint config for a specific provider."""
        return self._config.endpoints.get(provider)

    async def check_all_health(self) -> AIBackendStatus:
        """Check health of all configured endpoints. Returns full status."""
        results: list[EndpointHealth] = []
        active = None

        for provider, ep in self._config.endpoints.items():
            if not ep.enabled:
                continue

            if provider == AIProviderName.CLAUDE_CLI:
                ok = _check_claude_cli()
                results.append(EndpointHealth(
                    provider=provider,
                    url="(local CLI)",
                    reachable=ok,
                    model=ep.model,
                    detail="claude binary found" if ok else "claude binary not in PATH",
                ))
                if ok and active is None:
                    active = provider
                continue

            if provider == AIProviderName.CLAUDE_API:
                # Can't health-check without making an API call; just report config
                has_key = bool(ep.api_key)
                results.append(EndpointHealth(
                    provider=provider,
                    url="api.anthropic.com",
                    reachable=has_key,
                    model=ep.model,
                    detail="API key configured" if has_key else "no API key",
                ))
                continue

            if not ep.url:
                continue

            ok, detail = _check_url_reachable(ep.url)

            # For Ollama/DGX, try to detect GPU
            gpu_name, gpu_vram = None, None
            if ok and provider in (AIProviderName.OLLAMA, AIProviderName.DGX):
                # Remote GPU detection via Ollama's /api/ps
                try:
                    req = urllib.request.Request(f"{ep.url.rstrip('/')}/api/ps")
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        ps_data = json.loads(resp.read())
                        models = ps_data.get("models", [])
                        if models:
                            m = models[0]
                            vram = m.get("size_vram", 0)
                            if vram:
                                gpu_vram = vram // (1024 * 1024)
                except Exception:
                    pass

            results.append(EndpointHealth(
                provider=provider,
                url=ep.url,
                reachable=ok,
                model=ep.model,
                detail=detail,
                gpu_name=gpu_name,
                gpu_vram_mb=gpu_vram,
            ))

            if ok and active is None:
                active = provider

        return AIBackendStatus(
            active_provider=active,
            endpoints=results,
            claude_cli_available=_check_claude_cli(),
        )
