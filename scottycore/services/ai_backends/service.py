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

from scottycore.services.ai_backends.schemas import (
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

    Config resolution order (highest priority wins):
      1. Settings store (GlobalSettings.ai_endpoint_* fields) — managed via UI
      2. Environment variables (.env file)
      3. BASELINE_ENDPOINTS hardcoded defaults

    The settings_service is optional. When provided, `load_from_settings()`
    reads persisted config from the DB. When not provided (or the settings
    table is empty), env vars and baseline defaults are used.
    """

    def __init__(
        self,
        config: AIBackendConfig | None = None,
        settings_service: object | None = None,
    ) -> None:
        self._config = config or self._build_default_config()
        self._settings_service = settings_service

    @property
    def config(self) -> AIBackendConfig:
        return self._config

    def update_config(self, config: AIBackendConfig) -> None:
        """Replace the current config (e.g. after loading from settings store)."""
        self._config = config

    async def load_from_settings(self) -> None:
        """Reload config from the settings store, layered on top of env/baseline.

        Called at startup and whenever settings are changed via the UI.
        """
        if self._settings_service is None:
            return

        try:
            settings = await self._settings_service.get_global()  # type: ignore[union-attr]
        except Exception:
            return  # Settings store not available yet (e.g. first run)

        # Start with env/baseline defaults
        config = self._build_default_config()

        # Override with persisted settings
        config = config.model_copy(update={
            "default_provider": AIProviderName(settings.ai_default_provider),
            "fallback_chain": [AIProviderName(p) for p in settings.ai_fallback_chain],
        })

        # Map settings schema fields to provider names
        setting_map = {
            AIProviderName.OLLAMA: settings.ai_endpoint_ollama,
            AIProviderName.DGX: settings.ai_endpoint_dgx,
            AIProviderName.CLAUDE_CLI: settings.ai_endpoint_claude_cli,
            AIProviderName.CLAUDE_API: settings.ai_endpoint_claude_api,
            AIProviderName.OPENAI: settings.ai_endpoint_openai,
            AIProviderName.AZURE_OPENAI: settings.ai_endpoint_azure_openai,
        }

        for provider, setting in setting_map.items():
            if setting is None:
                continue
            # Only override fields that the user has actually set (non-default)
            ep = config.endpoints.get(provider)
            if ep is None:
                continue
            updates: dict = {}
            if setting.url:
                updates["url"] = setting.url
            if setting.model:
                updates["model"] = setting.model
            if setting.api_key:
                updates["api_key"] = setting.api_key
            updates["enabled"] = setting.enabled
            if setting.context_window:
                updates["context_window"] = setting.context_window
            if setting.priority:
                updates["priority"] = setting.priority
            if setting.notes:
                updates["notes"] = setting.notes
            config.endpoints[provider] = ep.model_copy(update=updates)

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
