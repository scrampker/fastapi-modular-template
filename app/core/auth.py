"""Unified authentication resolver.
# scottycore-pattern: auth.unified_resolver
# scottycore-pattern: auth.idle_timeout

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

    # ── 6. Dev bypass token (headless Chrome / Claude Code access) ───
    # Enabled via DEV_BYPASS_TOKEN env var. NEVER set in production.
    # Accepts: ?_dev_token=<token> query param or X-Dev-Token header.
    if settings.dev_bypass_token:
        dev_token = (
            request.query_params.get("_dev_token")
            or request.headers.get("X-Dev-Token")
        )
        if dev_token and dev_token == settings.dev_bypass_token:
            return await _resolve_dev_bypass_user(registry, settings)

    # ── 7. No credentials ────────────────────────────────────────────
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


async def check_lockout_risk(registry) -> bool:
    """Return True if at least one active superadmin has a real local password.

    Used before disabling the 'local' auth provider to prevent full lockout.
    Delegates to UsersService — no direct model/repository access.
    """
    return await registry.users.has_local_password_superadmin()


# ── Internal helpers ──────────────────────────────────────────────────────


async def _resolve_external_user(
    registry, settings: Settings, email: str, provider: str, request: Request
) -> UserContext:
    """Resolve an externally-authenticated email to a UserContext.

    Auto-provisions unknown users. Promotes ADMIN_EMAIL to superadmin.
    All user operations go through UsersService — no direct model imports.
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
            # Create as regular user — admin must assign to a tenant
            user_read = await registry.users.create_external_user(
                email=email,
                display_name=email.split("@")[0].title(),
            )

        await registry.audit.log(AuditLogCreate(
            user_id=user_read.id,
            action="auth.external_provision",
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
        await registry.users.promote_to_superadmin(email)

    return await registry.users.build_user_context(user_read, registry.tenants)


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

    # Fetch user via service layer — no direct ORM access
    user_read = await registry.users.get_by_id(UUID(user_id))
    if not user_read or not user_read.is_active:
        raise AuthenticationError("User not found or inactive")

    # ── Session version check ─────────────────────────────────────────
    token_sv = payload.get("sv", 0)
    if token_sv < user_read.session_version:
        raise AuthenticationError("Session invalidated — please log in again")

    return await registry.users.build_user_context(user_read, registry.tenants)


async def _resolve_api_key_user(registry, api_key: str) -> UserContext:
    """Resolve an API key to a UserContext scoped to that tenant.

    Delegates the prefix-based O(1) lookup + bcrypt verification to
    TenantsService — no direct repository or model imports.
    """
    tenant = await registry.tenants.verify_api_key(api_key)
    if tenant is None:
        raise AuthenticationError("Invalid API key")

    return UserContext(
        user_id=UUID("00000000-0000-0000-0000-000000000000"),
        email=f"api@{tenant.slug}",
        display_name=f"API ({tenant.name})",
        is_superadmin=False,
        tenant_roles={tenant.slug: RoleName.ADMIN},
    )


async def _resolve_dev_bypass_user(registry, settings) -> UserContext:
    """Create a synthetic superadmin UserContext for dev/headless access.

    This is gated by DEV_BYPASS_TOKEN in the caller — only reachable when
    the env var is set AND the request supplies a matching token.
    """
    return UserContext(
        user_id=UUID("00000000-0000-0000-0000-ffffffffffff"),
        email="dev-bypass@localhost",
        display_name="Dev Bypass (Claude Code)",
        is_superadmin=True,
        tenant_roles={},
    )
