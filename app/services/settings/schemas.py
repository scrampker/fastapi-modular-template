"""Settings service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Tier schemas ──────────────────────────────────────────────────────────────


SMTP_PASSWORD_MASKED = "*****"


class AIEndpointSetting(BaseModel):
    """Persisted config for a single AI endpoint (stored as JSON in settings KV)."""
    url: str = ""
    model: str = ""
    api_key: str = ""  # write-only; masked on GET
    enabled: bool = False
    context_window: int = 0
    priority: int = 0
    notes: str = ""


AI_API_KEY_MASKED = "*****"


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

    # ── AI Backends ────────────────────────────────────────────────────
    # Stored as JSON dicts in the KV store. The settings UI renders these
    # as editable endpoint cards. Defaults come from BASELINE_ENDPOINTS
    # in ai_backends/schemas.py; env vars override; DB settings override both.
    ai_default_provider: str = "claude_cli"
    ai_fallback_chain: list[str] = Field(
        default_factory=lambda: ["dgx", "ollama", "claude_cli"]
    )
    ai_endpoint_ollama: AIEndpointSetting = Field(default_factory=lambda: AIEndpointSetting(
        url="http://192.168.150.210:11434", model="qwen2.5:7b",
        enabled=True, context_window=32768, priority=20,
        notes="Local Ollama on CT 114 (GPU passthrough).",
    ))
    ai_endpoint_dgx: AIEndpointSetting = Field(default_factory=lambda: AIEndpointSetting(
        url="http://192.168.150.111:11434", model="qwen3.5:35b-a3b",
        enabled=True, context_window=131072, priority=10,
        notes="DGX Spark on homelab.",
    ))
    ai_endpoint_claude_cli: AIEndpointSetting = Field(default_factory=lambda: AIEndpointSetting(
        model="opus", enabled=True, context_window=200000, priority=30,
        notes="Uses host Claude Code auth. No API key needed.",
    ))
    ai_endpoint_claude_api: AIEndpointSetting = Field(default_factory=lambda: AIEndpointSetting(
        model="claude-sonnet-4-6", enabled=False, context_window=200000, priority=40,
        notes="Anthropic SDK direct. Needs API key or Claude Code OAuth token.",
    ))
    ai_endpoint_openai: AIEndpointSetting = Field(default_factory=lambda: AIEndpointSetting(
        url="https://api.openai.com/v1", model="gpt-4o",
        enabled=False, context_window=128000, priority=50,
        notes="OpenAI API. Needs OPENAI_API_KEY.",
    ))
    ai_endpoint_azure_openai: AIEndpointSetting = Field(default_factory=lambda: AIEndpointSetting(
        enabled=False, context_window=128000, priority=50,
        notes="Azure OpenAI Service. Needs endpoint + key.",
    ))


class TenantSettings(BaseModel):
    """Per-tenant overrides, managed by tenant admins."""

    retention_days_override: int | None = Field(None, ge=1, le=3650)
    # retention_days: explicit per-tenant data retention cap in days.
    # When set, items older than this value are flagged by the retention report
    # endpoint.  Falls back to GlobalSettings.retention_days_default when None.
    retention_days: int | None = Field(None, ge=1, le=3650)
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
