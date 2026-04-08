"""Unified authentication resolver.

Priority order:
  1. Cloudflare Zero Trust header  (Cf-Access-Authenticated-User-Email)
  2. Azure AD / Entra ID header    (X-MS-CLIENT-PRINCIPAL-NAME)
  3. Session cookie                 (session=<jwt>)
  4. JWT Bearer token               (Authorization: Bearer <token>)
  5. API key                        (X-API-Key: <key>)
  6. No auth -> None

External identity providers (Cloudflare, Azure) auto-provision users on first
login. Unknown emails get a "pending" role; the configured ADMIN_EMAIL is
auto-promoted to superadmin.

Session security:
- JWT tokens carry a ``sv`` (session_version) claim.  If the value in the
  token is less than the user's current ``session_version`` column value the
  token is rejected — this enables instant forced re-auth when an admin
  deactivates a user or resets a password.
- The ``iat`` (issued-at) claim is checked against the ``session_timeout_minutes``
  global setting.  Tokens older than the idle timeout are rejected even if
  their ``exp`` hasn't been reached yet.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, ForbiddenError
from app.core.schemas import RoleName, has_minimum_role
from app.services.auth.schemas import UserContext


async def get_current_user(request: Request) -> UserContext | None:
    """Extract the authenticated user from the request.

    Returns UserContext if authenticated, None if no credentials provided.
    Raises AuthenticationError if credentials are present but invalid.
    """
    settings = get_settings()
    registry = request.app.state.registry

    # ── 1. Cloudflare Zero Trust ──────────────────────────────────────
    if "cloudflare" in settings.trusted_providers:
        cf_email = request.headers.get("Cf-Access-Authenticated-User-Email")
        if cf_email:
            return await _resolve_external_user(
                registry, settings, cf_email.strip().lower(), "cloudflare", request
            )

    # ── 2. Azure AD / Entra ID ────────────────────────────────────────
    if "azure" in settings.trusted_providers:
        azure_email = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
        if azure_email:
            return await _resolve_external_user(
                registry, settings, azure_email.strip().lower(), "azure", request
            )

    # ── 3. Session cookie (preferred for browser clients) ─────────────
    session_cookie = request.cookies.get("session")
    if session_cookie:
        return await _resolve_jwt_user(registry, session_cookie)

    # ── 4. JWT Bearer token ───────────────────────────────────────────
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        return await _resolve_jwt_user(registry, token)

    # ── 5. API key ────────────────────────────────────────────────────
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return await _resolve_api_key_user(registry, api_key)

    # ── 6. No credentials ────────────────────────────────────────────
    return None


async def require_auth(user: UserContext | None = Depends(get_current_user)) -> UserContext:
    """Dependency that REQUIRES authentication. Use on protected endpoints."""
    if user is None:
        raise AuthenticationError("Authentication required")
    return user


async def require_superadmin(user: UserContext = Depends(require_auth)) -> UserContext:
    """Dependency that requires superadmin role."""
    if not user.is_superadmin:
        raise ForbiddenError("Superadmin access required")
    return user


def require_role(minimum: RoleName):
    """Factory: returns a dependency that requires minimum role for a tenant."""
    async def _check(request: Request, user: UserContext = Depends(require_auth)) -> UserContext:
        if user.is_superadmin:
            return user
        slug = request.path_params.get("slug")
        if not slug:
            raise ForbiddenError("No tenant context")
        user_role = user.tenant_roles.get(slug)
        if not user_role or not has_minimum_role(user_role, minimum):
            raise ForbiddenError(f"Requires {minimum.value} role for tenant '{slug}'")
        return user
    return _check


# ── Lockout risk check ────────────────────────────────────────────────────

_EXTERNAL_AUTH_SENTINEL = "_external_auth_no_password_"


async def check_lockout_risk(registry) -> bool:
    """Return True if at least one active superadmin has a real local password.

    Used before disabling the 'local' auth provider to prevent full lockout.
    An account is considered locally-accessible when its password_hash was NOT
    set to the external-auth sentinel value.
    """
    from sqlalchemy import select
    from app.services.users.models import User

    async with registry.users._session_factory() as session:
        result = await session.scalars(
            select(User).where(
                User.is_superadmin == True,  # noqa: E712
                User.is_active == True,  # noqa: E712
            )
        )
        superadmins = result.all()
        for user in superadmins:
            if user.password_hash and user.password_hash != _EXTERNAL_AUTH_SENTINEL:
                return True
    return False


# ── Internal helpers ──────────────────────────────────────────────────────


async def _resolve_external_user(
    registry, settings: Settings, email: str, provider: str, request: Request
) -> UserContext:
    """Resolve an externally-authenticated email to a UserContext.

    Auto-provisions unknown users. Promotes ADMIN_EMAIL to superadmin.
    """
    from app.services.audit.schemas import AuditLogCreate

    user_read = await registry.users.get_by_email(email)

    if user_read is None:
        # Auto-provision: create user with no password (external-only auth)
        is_admin = settings.admin_email and email == settings.admin_email.strip().lower()
        if is_admin:
            user_read = await registry.users.create_superadmin(
                email=email,
                password="_external_auth_no_password_",
                display_name=email.split("@")[0].title(),
            )
        else:
            # Create as inactive/pending — admin must assign to a tenant
            async with registry.users._session_factory() as session:
                from app.services.users.repository import UserRepository
                from app.services.auth.service import AuthService
                repo = UserRepository(session)
                user = await repo.create(
                    email=email,
                    password_hash=AuthService.hash_password("_external_auth_no_password_"),
                    display_name=email.split("@")[0].title(),
                )
                await session.commit()
                user_read = await registry.users.get_by_email(email)

        await registry.audit.log(AuditLogCreate(
            user_id=user_read.id,
            action=f"auth.external_provision",
            target_type="user",
            target_id=str(user_read.id),
            detail={"provider": provider, "email": email},
            ip_address=request.client.host if request.client else "unknown",
        ))

    # Promote ADMIN_EMAIL to superadmin if they aren't already
    if (
        settings.admin_email
        and email == settings.admin_email.strip().lower()
        and not user_read.is_superadmin
    ):
        async with registry.users._session_factory() as session:
            from app.services.users.repository import UserRepository
            repo = UserRepository(session)
            user_obj = await repo.get_by_email(email)
            if user_obj:
                await repo.update(user_obj, is_superadmin=True)
                await session.commit()

    # Build tenant roles map
    tenant_roles = await registry.users.get_user_tenant_roles(user_read.id)
    # Map tenant_id -> slug for the context
    slug_roles: dict[str, RoleName] = {}
    for tid, role in tenant_roles.items():
        try:
            tenant = await registry.tenants.get_by_id(UUID(tid))
            slug_roles[tenant.slug] = role
        except Exception:
            pass

    return UserContext(
        user_id=user_read.id,
        email=user_read.email,
        display_name=user_read.display_name,
        is_superadmin=user_read.is_superadmin,
        tenant_roles=slug_roles,
    )


async def _resolve_jwt_user(registry, token: str) -> UserContext:
    """Resolve a JWT token to a UserContext.

    Raises ``ForbiddenError`` if the token carries a ``totp_pending`` claim,
    meaning the user authenticated with password but has not yet completed
    TOTP verification.

    Also enforces:
    - ``session_version`` claim: token is rejected if its version is less than
      the user's current ``session_version`` (forced re-auth path).
    - Idle timeout: token's ``iat`` must be within ``session_timeout_minutes``
      of now even if the JWT ``exp`` hasn't elapsed yet.
    """
    payload = registry.auth.decode_token(token)
    if payload.get("totp_pending"):
        raise ForbiddenError("TOTP verification required")
    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("Invalid token: no subject")

    # ── Idle timeout check ────────────────────────────────────────────
    iat = payload.get("iat")
    if iat is not None:
        try:
            settings = get_settings()
            # Resolve effective session_timeout from global settings store;
            # fall back to the JWT expiry config if the settings store is unavailable.
            timeout_minutes: int = settings.jwt_access_token_expire_minutes
            try:
                resolved_timeout = await registry.settings.resolve("session_timeout_minutes")
                if resolved_timeout is not None:
                    timeout_minutes = int(resolved_timeout)
            except Exception:
                pass

            issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
            if datetime.now(timezone.utc) - issued_at > timedelta(minutes=timeout_minutes):
                raise AuthenticationError("Session expired — please log in again")
        except AuthenticationError:
            raise
        except Exception:
            pass  # Never let timeout check crash auth for malformed iat

    user_read = None
    user_session_version = 0
    async with registry.users._session_factory() as session:
        from app.services.users.repository import UserRepository
        repo = UserRepository(session)
        user_obj = await repo.get_by_id(user_id)
        if not user_obj or not user_obj.is_active:
            raise AuthenticationError("User not found or inactive")
        user_session_version = getattr(user_obj, "session_version", 0)
        from app.services.users.schemas import UserRead
        user_read = UserRead.model_validate(user_obj)

    # ── Session version check ─────────────────────────────────────────
    token_sv = payload.get("sv", 0)
    if token_sv < user_session_version:
        raise AuthenticationError("Session invalidated — please log in again")

    tenant_roles = await registry.users.get_user_tenant_roles(user_read.id)
    slug_roles: dict[str, RoleName] = {}
    for tid, role in tenant_roles.items():
        try:
            tenant = await registry.tenants.get_by_id(UUID(tid))
            slug_roles[tenant.slug] = role
        except Exception:
            pass

    return UserContext(
        user_id=user_read.id,
        email=user_read.email,
        display_name=user_read.display_name,
        is_superadmin=user_read.is_superadmin,
        tenant_roles=slug_roles,
    )


async def _resolve_api_key_user(registry, api_key: str) -> UserContext:
    """Resolve an API key to a UserContext scoped to that tenant.

    Uses a SHA-256 prefix index for O(1) lookup before bcrypt verification,
    avoiding the previous O(n*bcrypt) scan across all tenants.
    """
    from app.services.auth.service import AuthService
    from app.services.tenants.repository import TenantRepository

    prefix = hashlib.sha256(api_key.encode()).hexdigest()[:16]

    async with registry.tenants._session_factory() as session:
        repo = TenantRepository(session)
        tenant_obj = await repo.get_by_api_key_prefix(prefix)
        if tenant_obj and tenant_obj.api_key_hash:
            if AuthService.verify_api_key(api_key, tenant_obj.api_key_hash):
                return UserContext(
                    user_id=UUID("00000000-0000-0000-0000-000000000000"),
                    email=f"api@{tenant_obj.slug}",
                    display_name=f"API ({tenant_obj.name})",
                    is_superadmin=False,
                    tenant_roles={tenant_obj.slug: RoleName.ADMIN},
                )

    raise AuthenticationError("Invalid API key")
