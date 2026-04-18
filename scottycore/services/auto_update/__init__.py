"""Auto-update polling for the scottycore pip-pin."""

from __future__ import annotations

from scottycore.services.auto_update.service import (
    AutoUpdateError,
    AutoUpdateService,
    MODE_AUTO,
    MODE_NOTIFY,
    MODE_OFF,
    UpdateCheckResult,
)

__all__ = [
    "AutoUpdateError",
    "AutoUpdateService",
    "MODE_AUTO",
    "MODE_NOTIFY",
    "MODE_OFF",
    "UpdateCheckResult",
]
