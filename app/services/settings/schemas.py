"""Settings service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Tier schemas ──────────────────────────────────────────────────────────────


SMTP_PASSWORD_MASKED = "*****"


class GlobalSettings(BaseModel):
    """Platform-wide settings, managed by superadmin only."""

    auth_providers: list[str] = ["local"]  # local, cloudflare, azure
    session_timeout_minutes: int = Field(15, ge=5, le=1440)
    password_min_length: int = Field(8, ge=6, le=128)
    retention_days_default: int = Field(90, ge=1, le=3650)
    branding_app_name: str = Field("MyApp", max_length=80)
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)
    smtp_from: str | None = None
    smtp_password: str | None = None  # write-only; masked to "*****" on GET


class TenantSettings(BaseModel):
    """Per-tenant overrides, managed by tenant admins."""

    retention_days_override: int | None = Field(None, ge=1, le=3650)
    notification_email: str | None = None
    custom_field_1: str | None = None
    custom_field_2: str | None = None


class UserSettings(BaseModel):
    """Per-user preferences, managed by the user themselves."""

    theme: str = Field("system", pattern="^(dark|light|system)$")
    timezone: str = "UTC"
    default_tenant: str | None = None
    page_size: int = Field(25, ge=10, le=200)
    notifications_enabled: bool = True


# ── Wire schemas ──────────────────────────────────────────────────────────────


class SettingRead(BaseModel):
    """Raw KV row read response."""

    id: UUID
    scope: str
    scope_id: UUID | None
    key: str
    value_json: str
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SettingWrite(BaseModel):
    """Body for writing a raw KV entry (superadmin escape hatch)."""

    value_json: str = Field(min_length=1)


class EffectiveSettings(BaseModel):
    """Merged view: user -> tenant -> global -> defaults."""

    global_settings: GlobalSettings
    tenant_settings: TenantSettings | None
    user_settings: UserSettings
    resolved: dict
