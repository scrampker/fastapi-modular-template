"""User service — public interface for user management and role assignment."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from scottycore.core.exceptions import ConflictError, NotFoundError
from scottycore.core.schemas import AuditContext, PaginatedResponse, RoleName
from scottycore.services.audit.schemas import AuditLogCreate
from scottycore.services.audit.service import AuditService
from scottycore.services.auth.schemas import UserContext
from scottycore.services.auth.service import AuthService
from scottycore.services.users.repository import UserRepository

if TYPE_CHECKING:
    from scottycore.services.tenants.service import TenantsService
from scottycore.services.users.schemas import (
    UserCreate,
    UserFilter,
    UserRead,
    UserUpdate,
    UserWithRole,
)

_ROLE_MAP = {1: RoleName.VIEWER, 2: RoleName.ANALYST, 3: RoleName.ADMIN, 4: RoleName.SUPERADMIN}


class UsersService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit

    async def create_for_tenant(
        self, tenant_id: UUID, data: UserCreate, ctx: AuditContext
    ) -> UserWithRole:
        async with self._session_factory() as session:
            repo = UserRepository(session)
            existing = await repo.get_by_email(data.email)
            if existing:
                raise ConflictError(f"User with email '{data.email}' already exists")

            password_hash = AuthService.hash_password(data.password)
            user = await repo.create(
                email=data.email,
                password_hash=password_hash,
                display_name=data.display_name,
            )
            role = await repo.get_role_by_name(data.role.value)
            if not role:
                raise NotFoundError("Role", data.role.value)
            await repo.assign_role(
                user_id=user.id,
                tenant_id=str(tenant_id),
                role_id=role.id,
                assigned_by=str(ctx.user_id),
            )
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=tenant_id,
                action="user.create",
                target_type="user",
                target_id=str(user.id),
                detail={"email": data.email, "role": data.role.value},
                ip_address=ctx.ip_address,
            ))
            return UserWithRole(
                **UserRead.model_validate(user).model_dump(),
                role=data.role,
            )

    async def create_superadmin(self, email: str, password: str, display_name: str) -> UserRead:
        """Create initial superadmin. Used during first-run setup only."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            existing = await repo.get_by_email(email)
            if existing:
                raise ConflictError(f"User with email '{email}' already exists")
            password_hash = AuthService.hash_password(password)
            user = await repo.create(
                email=email,
                password_hash=password_hash,
                display_name=display_name,
                is_superadmin=True,
            )
            await session.commit()
            return UserRead.model_validate(user)

    async def list_for_tenant(
        self, tenant_id: UUID, filters: UserFilter
    ) -> PaginatedResponse[UserWithRole]:
        async with self._session_factory() as session:
            repo = UserRepository(session)
            rows, total = await repo.list_for_tenant(str(tenant_id), filters)
            items = []
            for user, utr in rows:
                role_name = _ROLE_MAP.get(utr.role_id, RoleName.VIEWER)
                items.append(UserWithRole(
                    **UserRead.model_validate(user).model_dump(),
                    role=role_name,
                ))
            return PaginatedResponse[UserWithRole](
                items=items,
                total=total,
                page=filters.page,
                per_page=filters.per_page,
            )

    async def get_by_email(self, email: str) -> UserRead | None:
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_email(email)
            if not user:
                return None
            return UserRead.model_validate(user)

    async def get_password_hash(self, email: str) -> str | None:
        """Get password hash for login verification. Separate from get_by_email for security."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_email(email)
            if not user:
                return None
            return user.password_hash

    async def get_user_tenant_roles(self, user_id: UUID) -> dict[str, RoleName]:
        """Returns {tenant_id: role_name} for all tenants this user has access to."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            utrs = await repo.get_user_roles(str(user_id))
            return {utr.tenant_id: _ROLE_MAP.get(utr.role_id, RoleName.VIEWER) for utr in utrs}

    async def update_for_tenant(
        self,
        tenant_id: UUID,
        user_id: UUID,
        data: UserUpdate,
        ctx: AuditContext,
    ) -> UserWithRole:
        """Update a user's role or active status within a tenant."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                raise NotFoundError("User", str(user_id))

            changes: dict = {}
            if data.display_name is not None:
                changes["display_name"] = data.display_name
            if data.is_active is not None:
                changes["is_active"] = data.is_active
                # Deactivating a user invalidates all their active JWT tokens.
                if not data.is_active:
                    changes["session_version"] = (user_obj.session_version or 0) + 1
            if changes:
                await repo.update(user_obj, **changes)

            # Update role assignment if requested
            if data.role is not None:
                from sqlalchemy import select as sa_select
                from scottycore.services.users.models import UserTenantRole
                utr_result = await session.scalars(
                    sa_select(UserTenantRole).where(
                        UserTenantRole.user_id == str(user_id),
                        UserTenantRole.tenant_id == str(tenant_id),
                    )
                )
                utr = utr_result.first()
                role = await repo.get_role_by_name(data.role.value)
                if not role:
                    raise NotFoundError("Role", data.role.value)
                if utr:
                    utr.role_id = role.id
                    await session.flush()
                else:
                    await repo.assign_role(
                        user_id=str(user_id),
                        tenant_id=str(tenant_id),
                        role_id=role.id,
                        assigned_by=str(ctx.user_id),
                    )

            await session.commit()
            await session.refresh(user_obj)

            from sqlalchemy import select as sa_select2
            from scottycore.services.users.models import UserTenantRole as UTR2
            utr2_result = await session.scalars(
                sa_select2(UTR2).where(
                    UTR2.user_id == str(user_id),
                    UTR2.tenant_id == str(tenant_id),
                )
            )
            utr2 = utr2_result.first()
            current_role = _ROLE_MAP.get(utr2.role_id, RoleName.VIEWER) if utr2 else RoleName.VIEWER

            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=tenant_id,
                action="user.update",
                target_type="user",
                target_id=str(user_id),
                detail={k: str(v) for k, v in changes.items()} | ({"role": data.role.value} if data.role else {}),
                ip_address=ctx.ip_address,
            ))
            return UserWithRole(
                **UserRead.model_validate(user_obj).model_dump(),
                role=current_role,
            )

    async def deactivate_for_tenant(
        self,
        tenant_id: UUID,
        user_id: UUID,
        ctx: AuditContext,
    ) -> UserWithRole:
        """Deactivate a user within a tenant context."""
        return await self.update_for_tenant(
            tenant_id,
            user_id,
            UserUpdate(is_active=False),
            ctx,
        )

    async def get_by_id(self, user_id: UUID) -> UserRead | None:
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                return None
            return UserRead.model_validate(user_obj)

    async def update_profile(
        self,
        user_id: UUID,
        display_name: str,
        ctx: AuditContext,
    ) -> UserRead:
        """Update the authenticated user's own display name."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                raise NotFoundError("User", str(user_id))
            await repo.update(user_obj, display_name=display_name)
            await session.commit()
            await session.refresh(user_obj)
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                action="user.update_profile",
                target_type="user",
                target_id=str(user_id),
                detail={"display_name": display_name},
                ip_address=ctx.ip_address,
            ))
            return UserRead.model_validate(user_obj)

    async def change_password(
        self,
        user_id: UUID,
        current_password: str,
        new_password: str,
        ctx: AuditContext,
    ) -> None:
        """Change the authenticated user's password after verifying the current one."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                raise NotFoundError("User", str(user_id))
            if not AuthService.verify_password(current_password, user_obj.password_hash):
                from scottycore.core.exceptions import AuthenticationError
                raise AuthenticationError("Current password is incorrect")
            new_hash = AuthService.hash_password(new_password)
            # Increment session_version to invalidate all previously issued tokens.
            new_session_version = (user_obj.session_version or 0) + 1
            await repo.update(user_obj, password_hash=new_hash, session_version=new_session_version)
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                action="user.change_password",
                target_type="user",
                target_id=str(user_id),
                detail={},
                ip_address=ctx.ip_address,
            ))

    async def user_count(self) -> int:
        async with self._session_factory() as session:
            repo = UserRepository(session)
            return await repo.count_all()

    # ── Methods used by core/auth.py (service-layer boundary) ─────────

    _EXTERNAL_AUTH_SENTINEL = "_external_auth_no_password_"

    async def create_external_user(self, email: str, display_name: str) -> UserRead:
        """Auto-provision a user from an external identity provider.

        The user gets a sentinel password hash (not a real password) and is
        created as a regular (non-superadmin) active user.  An admin must
        assign them to a tenant before they can access anything.
        """
        async with self._session_factory() as session:
            repo = UserRepository(session)
            existing = await repo.get_by_email(email)
            if existing:
                return UserRead.model_validate(existing)
            password_hash = AuthService.hash_password(self._EXTERNAL_AUTH_SENTINEL)
            user = await repo.create(
                email=email,
                password_hash=password_hash,
                display_name=display_name,
            )
            await session.commit()
            return UserRead.model_validate(user)

    async def promote_to_superadmin(self, email: str) -> None:
        """Promote an existing user to superadmin. No-op if already superadmin."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_email(email)
            if user_obj and not user_obj.is_superadmin:
                await repo.update(user_obj, is_superadmin=True)
                await session.commit()

    async def has_local_password_superadmin(self) -> bool:
        """Return True if at least one active superadmin has a real local password.

        Used before disabling the 'local' auth provider to prevent full lockout.
        An account is considered external-only when its password_hash was set to
        the bcrypt-hashed sentinel value.  We use verify_password() for the check
        because the sentinel is stored hashed (not as a raw string).
        """
        async with self._session_factory() as session:
            repo = UserRepository(session)
            superadmins = await repo.list_active_superadmins()
            for user in superadmins:
                if not user.password_hash:
                    continue
                # External-only accounts have the sentinel hashed via bcrypt.
                # A real local password will NOT match the sentinel.
                if AuthService.verify_password(self._EXTERNAL_AUTH_SENTINEL, user.password_hash):
                    continue  # external-only — skip
                return True
        return False

    # ── TOTP data access (called by AuthService) ───────────────────────

    async def set_totp(
        self, user_id: UUID, secret: str, backup_codes_json: str
    ) -> None:
        """Enable TOTP for a user and store the secret + hashed backup codes."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                raise NotFoundError("User", str(user_id))
            user_obj.totp_secret = secret
            user_obj.totp_enabled = True
            user_obj.backup_codes = backup_codes_json
            await session.commit()

    async def clear_totp(self, user_id: UUID) -> None:
        """Disable TOTP and clear all TOTP fields for a user."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                raise NotFoundError("User", str(user_id))
            user_obj.totp_secret = None
            user_obj.totp_enabled = False
            user_obj.backup_codes = None
            await session.commit()

    async def get_totp_fields(
        self, user_id: UUID
    ) -> tuple[str | None, bool, str | None]:
        """Return (totp_secret, totp_enabled, backup_codes) for a user.

        Returns (None, False, None) if user not found.
        """
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if not user_obj:
                return None, False, None
            return user_obj.totp_secret, user_obj.totp_enabled, user_obj.backup_codes

    async def consume_backup_code(
        self, user_id: UUID, remaining_codes_json: str
    ) -> None:
        """Update the stored backup codes after one has been consumed."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user_obj = await repo.get_by_id(str(user_id))
            if user_obj:
                user_obj.backup_codes = remaining_codes_json
                await session.commit()

    async def build_user_context(
        self, user_read: UserRead, tenants_service: TenantsService
    ) -> UserContext:
        """Build a UserContext with slug-keyed tenant roles.

        Args:
            user_read: The user's public schema data.
            tenants_service: A TenantsService instance for resolving tenant slugs.
        """
        tenant_roles = await self.get_user_tenant_roles(user_read.id)
        slug_roles: dict[str, RoleName] = {}
        for tid, role in tenant_roles.items():
            try:
                tenant = await tenants_service.get_by_id(UUID(tid))
                slug_roles[tenant.slug] = role
            except NotFoundError:
                pass  # tenant deleted; skip stale role

        return UserContext(
            user_id=user_read.id,
            email=user_read.email,
            display_name=user_read.display_name,
            is_superadmin=user_read.is_superadmin,
            tenant_roles=slug_roles,
        )
