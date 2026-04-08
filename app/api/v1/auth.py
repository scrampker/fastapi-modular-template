"""Auth API routes — thin wrappers around AuthService + UsersService."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.dependencies import get_auth_service, get_users_service
from app.core.exceptions import AuthenticationError
from app.services.auth.schemas import LoginRequest, TokenResponse
from app.services.auth.service import AuthService
from app.services.users.service import UsersService

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    auth: AuthService = Depends(get_auth_service),
    users: UsersService = Depends(get_users_service),
) -> TokenResponse:
    """Authenticate with email + password, receive JWT access token."""
    password_hash = await users.get_password_hash(body.email)
    if not password_hash or not auth.verify_password(body.password, password_hash):
        raise AuthenticationError("Invalid email or password")

    user = await users.get_by_email(body.email)
    if not user or not user.is_active:
        raise AuthenticationError("Invalid email or password")

    access_token = auth.create_access_token(user.id)
    return auth.build_token_response(access_token)


@router.post("/setup", response_model=dict)
async def initial_setup(
    body: LoginRequest,
    users: UsersService = Depends(get_users_service),
) -> dict:
    """Create initial superadmin. Only works when no users exist."""
    count = await users.user_count()
    if count > 0:
        raise AuthenticationError("Setup already completed")
    user = await users.create_superadmin(
        email=body.email,
        password=body.password,
        display_name="Admin",
    )
    return {"message": "Superadmin created", "email": user.email}
