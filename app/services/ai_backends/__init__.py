# scottycore-pattern: ai_backends.multi_provider
"""AI Backends — multi-provider AI connectivity with automatic fallback."""

from app.services.ai_backends.schemas import (
    AIBackendConfig,
    AIBackendStatus,
    AIProviderName,
    EndpointHealth,
)
from app.services.ai_backends.service import AIBackendsService

__all__ = [
    "AIBackendConfig",
    "AIBackendStatus",
    "AIBackendsService",
    "AIProviderName",
    "EndpointHealth",
]
