"""PRIVATE: User database operations."""

from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.users.models import User, UserTenantRole, Role
from app.services.users.schemas import UserFilter


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, email: str, password_hash: str, display_name: str, is_superadmin: bool = False
    ) -> User:
        user = User(
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            is_superadmin=is_superadmin,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_id(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.scalars(
            select(User).where(User.email == email)
        )
        return result.first()

    async def list_for_tenant(
        self, tenant_id: str, filters: UserFilter
    ) -> tuple[list[tuple[User, UserTenantRole]], int]:
        query = (
            select(User, UserTenantRole)
            .join(UserTenantRole, User.id == UserTenantRole.user_id)
            .where(UserTenantRole.tenant_id == tenant_id)
        )
        count_query = (
            select(func.count(UserTenantRole.id))
            .where(UserTenantRole.tenant_id == tenant_id)
        )

        if filters.is_active is not None:
            query = query.where(User.is_active == filters.is_active)
            count_query = count_query.join(
                User, User.id == UserTenantRole.user_id
            ).where(User.is_active == filters.is_active)
        if filters.search:
            like = f"%{filters.search}%"
            query = query.where(User.email.ilike(like) | User.display_name.ilike(like))

        total = (await self._session.scalar(count_query)) or 0
        query = query.order_by(User.email).offset(filters.offset).limit(filters.per_page)
        result = await self._session.execute(query)
        return list(result.all()), total

    async def count_all(self) -> int:
        result = await self._session.scalar(select(func.count(User.id)))
        return result or 0

    async def assign_role(
        self, user_id: str, tenant_id: str, role_id: int, assigned_by: str | None = None
    ) -> UserTenantRole:
        utr = UserTenantRole(
            user_id=user_id,
            tenant_id=tenant_id,
            role_id=role_id,
            assigned_by=assigned_by,
        )
        self._session.add(utr)
        await self._session.flush()
        return utr

    async def get_user_roles(self, user_id: str) -> list[UserTenantRole]:
        result = await self._session.scalars(
            select(UserTenantRole).where(UserTenantRole.user_id == user_id)
        )
        return list(result.all())

    async def get_role_by_name(self, name: str) -> Role | None:
        result = await self._session.scalars(
            select(Role).where(Role.name == name)
        )
        return result.first()

    async def update(self, user: User, **kwargs: object) -> User:
        for key, value in kwargs.items():
            if value is not None:
                setattr(user, key, value)
        await self._session.flush()
        return user

    async def list_active_superadmins(self) -> list[User]:
        """Return all active superadmin users."""
        result = await self._session.scalars(
            select(User).where(
                User.is_superadmin == True,  # noqa: E712
                User.is_active == True,  # noqa: E712
            )
        )
        return list(result.all())
