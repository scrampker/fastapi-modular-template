"""Auth service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from scottycore.core.schemas import RoleName


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=1000)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LoginResponse(BaseModel):
    """Returned by POST /auth/login.

    When TOTP is enabled the full access token is withheld and a short-lived
    ``partial_token`` is issued instead so the client can complete verification.
    """

    access_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    requires_totp: bool = False
    partial_token: str | None = None


class UserContext(BaseModel):
    """The resolved user identity available to all routes after auth."""
    user_id: UUID
    email: str
    display_name: str
    is_superadmin: bool
    tenant_roles: dict[str, RoleName]  # {tenant_slug: role}


class RefreshTokenCreate(BaseModel):
    user_id: UUID
    token_hash: str
    expires_at: datetime


class InitialSetupRequest(BaseModel):
    email: str
    password: str
    display_name: str = "Admin"


# ── TOTP schemas ──────────────────────────────────────────────────────────────

class TOTPSetupResponse(BaseModel):
    """Returned when generating a new TOTP secret for enrollment."""
    secret: str
    otpauth_uri: str
    qr_code_base64: str  # data:image/png;base64,<...>


class TOTPVerifyRequest(BaseModel):
    """Body for verifying a TOTP or backup code."""
    code: str = Field(min_length=6, max_length=8)


class TOTPEnableRequest(BaseModel):
    """Body for confirming TOTP setup: the secret from /totp/setup + first code."""
    secret: str = Field(min_length=16, max_length=64)
    code: str = Field(min_length=6, max_length=8)


class BackupCodesResponse(BaseModel):
    """Returned once after enabling TOTP — plaintext codes shown exactly once."""
    codes: list[str]
