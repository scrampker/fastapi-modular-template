"""Auth API routes — thin wrappers around AuthService + UsersService."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.auth import require_auth
from app.core.dependencies import get_auth_service, get_users_service
from app.core.exceptions import AuthenticationError
from app.services.audit.schemas import AuditLogCreate
from app.services.auth.schemas import (
    BackupCodesResponse,
    LoginRequest,
    LoginResponse,
    TOTPEnableRequest,
    TOTPSetupResponse,
    TOTPVerifyRequest,
    TokenResponse,
    UserContext,
)
from app.services.auth.service import AuthService
from app.services.users.service import UsersService

router = APIRouter()

_GENERIC_LOGIN_ERROR = "Invalid email or password"


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    auth: AuthService = Depends(get_auth_service),
    users: UsersService = Depends(get_users_service),
) -> LoginResponse:
    """Authenticate with email + password.

    If the user has TOTP enabled a short-lived *partial token* is returned and
    ``requires_totp`` is set to ``True``.  The client must call
    ``POST /auth/totp/verify`` with that token to receive a full access token.

    After 5 consecutive failures within 15 minutes the account is locked for
    30 minutes.  A generic error message is returned regardless of the failure
    reason to avoid revealing whether the email address exists.
    """
    ip = request.client.host if request.client else "unknown"
    email_lower = body.email.strip().lower()

    # Check lockout before attempting credential verification
    if await auth.check_lockout(email_lower):
        # Record this blocked attempt so the lockout window stays fresh
        await auth.record_login_attempt(email_lower, success=False, ip_address=ip)
        raise AuthenticationError(_GENERIC_LOGIN_ERROR)

    password_hash = await users.get_password_hash(email_lower)
    if not password_hash or not auth.verify_password(body.password, password_hash):
        await auth.record_login_attempt(email_lower, success=False, ip_address=ip)
        raise AuthenticationError(_GENERIC_LOGIN_ERROR)

    user = await users.get_by_email(email_lower)
    if not user or not user.is_active:
        await auth.record_login_attempt(email_lower, success=False, ip_address=ip)
        raise AuthenticationError(_GENERIC_LOGIN_ERROR)

    # Fetch ORM object for TOTP and session_version fields
    user_obj = await _get_user_orm(request, user.id)
    session_version = getattr(user_obj, "session_version", 0) if user_obj else 0

    await auth.record_login_attempt(email_lower, success=True, ip_address=ip)
    await auth._audit.log(AuditLogCreate(
        user_id=user.id,
        action="auth.login",
        target_type="user",
        target_id=str(user.id),
        detail={"email": email_lower},
        ip_address=ip,
    ))

    if user_obj is not None and getattr(user_obj, "totp_enabled", False):
        partial_token = auth.create_access_token(
            user.id, totp_pending=True, session_version=session_version
        )
        return LoginResponse(requires_totp=True, partial_token=partial_token)

    access_token = auth.create_access_token(user.id, session_version=session_version)
    return LoginResponse(
        access_token=access_token,
        expires_in=auth._settings.jwt_access_token_expire_minutes * 60,
    )


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


# ── TOTP endpoints ────────────────────────────────────────────────────────────

@router.post("/totp/setup", response_model=TOTPSetupResponse)
async def totp_setup(
    current_user: UserContext = Depends(require_auth),
    auth: AuthService = Depends(get_auth_service),
) -> TOTPSetupResponse:
    """Generate a new TOTP secret and QR code for the authenticated user.

    The returned *secret* must be confirmed by calling ``POST /auth/totp/enable``
    with a valid code from the authenticator app.  The secret is **not** saved
    until that confirmation step succeeds.
    """
    return auth.generate_totp_secret(current_user.email)


@router.post("/totp/enable", response_model=BackupCodesResponse)
async def totp_enable(
    body: TOTPEnableRequest,
    current_user: UserContext = Depends(require_auth),
    auth: AuthService = Depends(get_auth_service),
) -> BackupCodesResponse:
    """Confirm TOTP setup by validating the first code.

    Persists the secret, marks the user's TOTP as enabled, and returns a set of
    one-time backup codes (shown **exactly once** — store them securely).

    Pass the ``secret`` returned by ``POST /auth/totp/setup`` together with the
    ``code`` from the authenticator app.
    """
    return await auth.enable_totp(current_user.user_id, body.secret, body.code)


@router.post("/totp/verify", response_model=TokenResponse)
async def totp_verify(
    body: TOTPVerifyRequest,
    request: Request,
    auth: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Exchange a partial token + TOTP code for a full access token.

    The ``Authorization: Bearer <partial_token>`` header must carry the
    short-lived token that was issued during the login step when
    ``requires_totp`` was ``True``.
    """
    # Extract the partial token from Authorization header (cookie may not be set yet)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise AuthenticationError("Partial token required in Authorization header")
    partial_token = auth_header[7:]

    # Decode without going through the full resolver (which blocks totp_pending)
    try:
        payload = auth.decode_token(partial_token)
    except AuthenticationError:
        raise AuthenticationError("Invalid or expired partial token")

    if not payload.get("totp_pending"):
        raise AuthenticationError("Token is not a TOTP partial token")

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise AuthenticationError("Invalid token: no subject")

    user_id = UUID(user_id_str)
    verified = await auth.verify_totp(user_id, body.code)
    if not verified:
        raise AuthenticationError("Invalid verification code")

    access_token = auth.create_access_token(user_id)
    return auth.build_token_response(access_token)


@router.post("/totp/disable", response_model=dict)
async def totp_disable(
    body: TOTPVerifyRequest,
    current_user: UserContext = Depends(require_auth),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    """Disable TOTP for the authenticated user after verifying the current code."""
    verified = await auth.verify_totp(current_user.user_id, body.code)
    if not verified:
        raise AuthenticationError("Invalid verification code")
    await auth.disable_totp(current_user.user_id)
    return {"message": "Two-factor authentication has been disabled"}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user_orm(request: Request, user_id: UUID):
    """Fetch the raw ORM User object to read TOTP fields not in UserRead."""
    from app.services.users.models import User

    registry = request.app.state.registry
    async with registry.users._session_factory() as session:
        return await session.get(User, str(user_id))
