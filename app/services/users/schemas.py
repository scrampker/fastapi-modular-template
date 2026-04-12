"""User service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.schemas import PaginationParams, RoleName


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=200)
    role: RoleName = RoleName.VIEWER


class UserUpdate(BaseModel):
    display_name: str | None = None
    is_active: bool | None = None
    role: RoleName | None = None


class UserRead(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_active: bool
    is_superadmin: bool
    totp_enabled: bool = False
    must_change_password: bool = False
    session_version: int = 0
    created_at: datetime
    last_login: datetime | None

    model_config = {"from_attributes": True}


class UserWithRole(UserRead):
    """User as seen within a specific tenant context."""
    role: RoleName


class UserFilter(PaginationParams):
    is_active: bool | None = None
    search: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class ProfileUpdate(BaseModel):
    display_name: str | None = None
