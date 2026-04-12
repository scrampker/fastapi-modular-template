# scottycore-pattern: ai_backends.multi_provider
"""AI Backends API — health checks, provider resolution, and configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import require_auth, require_superadmin
from app.core.dependencies import get_ai_backends_service
from app.services.ai_backends.schemas import AIBackendStatus, AIProviderName
from app.services.ai_backends.service import AIBackendsService
from app.services.auth.schemas import UserContext

router = APIRouter()


@router.get("/health", response_model=AIBackendStatus)
async def ai_health(
    user: UserContext = Depends(require_auth),
    svc: AIBackendsService = Depends(get_ai_backends_service),
) -> AIBackendStatus:
    """Check health of all configured AI backends."""
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
    resolved = svc.resolve_provider(provider)
    endpoint = svc.get_endpoint(resolved)
    return {
        "requested": provider,
        "resolved": resolved.value,
        "model": endpoint.model if endpoint else "",
        "url": endpoint.url if endpoint else "",
    }
