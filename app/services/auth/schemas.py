"""Auth service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.schemas import RoleName


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=1000)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


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
