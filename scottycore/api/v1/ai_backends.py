"""AI Backends API — health checks, provider resolution, and configuration.

All AI backend settings are managed through the global settings store.
Admins configure endpoints via the settings UI; this API provides
health-check and resolution endpoints, plus a dedicated save/reload path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from scottycore.core.auth import require_auth, require_superadmin
from scottycore.core.dependencies import get_ai_backends_service, get_settings_service
from scottycore.services.ai_backends.schemas import AIBackendStatus, AIProviderName
from scottycore.services.ai_backends.service import AIBackendsService
from scottycore.services.auth.schemas import UserContext
from scottycore.services.settings.schemas import AI_API_KEY_MASKED
from scottycore.services.settings.service import SettingsService

router = APIRouter()


@router.get("/health", response_model=AIBackendStatus)
async def ai_health(
    user: UserContext = Depends(require_auth),
    svc: AIBackendsService = Depends(get_ai_backends_service),
) -> AIBackendStatus:
    """Check health of all configured AI backends.

    Reloads config from the settings store before checking, so changes
    made via the settings UI are immediately reflected.
    """
    await svc.load_from_settings()
    return await svc.check_all_health()


@router.get("/resolve")
async def ai_resolve(
    provider: str = "auto",
    user: UserContext = Depends(require_auth),
    svc: AIBackendsService = Depends(get_ai_backends_service),
) -> dict:
    """Resolve which AI provider would be used for a request.

    Pass `provider=auto` (default) to get the highest-priority reachable
    backend, or pass a specific provider name to test its availability.
    """
    await svc.load_from_settings()
    resolved = svc.resolve_provider(provider)
    endpoint = svc.get_endpoint(resolved)
    return {
        "requested": provider,
        "resolved": resolved.value,
        "model": endpoint.model if endpoint else "",
        "url": endpoint.url if endpoint else "",
    }


@router.get("/config")
async def ai_config(
    user: UserContext = Depends(require_superadmin),
    svc: AIBackendsService = Depends(get_ai_backends_service),
) -> dict:
    """Get the current AI backend configuration (admin only).

    API keys are masked in the response. The full config is returned
    so the settings UI can render editable endpoint cards.
    """
    await svc.load_from_settings()
    config = svc.config

    endpoints = {}
    for provider, ep in config.endpoints.items():
        ep_dict = ep.model_dump()
        if ep_dict.get("api_key"):
            ep_dict["api_key"] = AI_API_KEY_MASKED
        endpoints[provider.value] = ep_dict

    return {
        "default_provider": config.default_provider.value,
        "fallback_chain": [p.value for p in config.fallback_chain],
        "endpoints": endpoints,
    }


@router.patch("/config")
async def ai_config_save(
    request: Request,
    user: UserContext = Depends(require_superadmin),
    settings_svc: SettingsService = Depends(get_settings_service),
    ai_svc: AIBackendsService = Depends(get_ai_backends_service),
) -> dict:
    """Update AI backend configuration via the settings store (admin only).

    Accepts a partial update — only include the fields you want to change.
    API keys set to the masked sentinel value are ignored (preserving the
    existing key).

    Example body:
    ```json
    {
        "ai_default_provider": "dgx",
        "ai_endpoint_dgx": {"url": "http://192.168.150.111:11434", "model": "qwen3.5:35b-a3b", "enabled": true}
    }
    ```
    """
    body = await request.json()

    # Strip masked API key sentinels so we don't overwrite real keys
    for key, value in body.items():
        if isinstance(value, dict) and value.get("api_key") == AI_API_KEY_MASKED:
            del value["api_key"]

    await settings_svc.set_global(body, user.user_id)
    await ai_svc.load_from_settings()

    return {"ok": True, "message": "AI backend configuration saved."}
