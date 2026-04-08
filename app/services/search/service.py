"""Search service — cross-domain search across tenants, items, and settings."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.tenants.models import Tenant
from app.services.tenants.service import TenantsService
from app.services.search.schemas import SearchResultItem, SearchResults

_PER_CATEGORY = 5

# Registry of searchable settings destinations for the Ctrl+K command palette.
_SETTINGS_REGISTRY: list[dict] = [
    # Admin / platform pages
    {"key": "tenants", "description": "Manage tenants", "url": "/admin/tenants"},
    {"key": "users", "description": "Manage user accounts and roles", "url": "/admin/users"},
    {"key": "audit", "description": "View audit log", "url": "/admin/audit"},
    # Platform settings sections
    {"key": "platform settings", "description": "Platform authentication providers", "url": "/admin/settings#platform"},
    {"key": "security settings", "description": "Session timeout and password policy", "url": "/admin/settings#security"},
    {"key": "branding settings", "description": "Application name and branding", "url": "/admin/settings#branding"},
    {"key": "operations settings", "description": "Data retention policy", "url": "/admin/settings#operations"},
    {"key": "communication settings smtp", "description": "SMTP email configuration", "url": "/admin/settings#communication"},
    # Tenant settings sections
    {"key": "tenant general settings retention notification", "description": "Tenant retention and notification email", "url": "/c/{slug}/settings#general"},
    # User settings sections
    {"key": "user display settings theme timezone", "description": "Display theme and timezone preferences", "url": "/settings#display"},
    {"key": "user preferences settings page size default tenant", "description": "Default workspace and pagination settings", "url": "/settings#preferences"},
    {"key": "user notification settings email", "description": "Email notification preferences", "url": "/settings#notifications"},
    # Tenant-scoped data views
    {"key": "items", "description": "View items", "url": "/c/{slug}/items"},
    {"key": "dashboard", "description": "View tenant dashboard", "url": "/c/{slug}/dashboard"},
]


class SearchService:
    """Composite service that searches across tenants, items, and settings."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenants: TenantsService,
    ) -> None:
        self._session_factory = session_factory
        self._tenants = tenants

    async def search(
        self,
        q: str,
        tenant_slug: str | None,
        is_superadmin: bool,
    ) -> SearchResults:
        """Run a cross-domain search and return grouped results."""
        like = f"%{q}%"

        async with self._session_factory() as session:
            tenants_list = (
                await self._search_tenants(session, like) if is_superadmin else []
            )
            settings = self._search_settings(q, tenant_slug or "default")

        results: dict[str, list[SearchResultItem]] = {}
        if tenants_list:
            results["tenants"] = tenants_list
        if settings:
            results["settings"] = settings

        total = sum(len(v) for v in results.values())
        return SearchResults(query=q, results=results, total=total)

    async def _search_tenants(
        self,
        session: AsyncSession,
        like: str,
    ) -> list[SearchResultItem]:
        rows = (
            await session.scalars(
                select(Tenant)
                .where(Tenant.name.ilike(like))
                .limit(_PER_CATEGORY)
            )
        ).all()
        return [
            SearchResultItem(
                id=r.id,
                title=r.name,
                subtitle=r.slug,
                url=f"/c/{r.slug}/dashboard",
                meta={"slug": r.slug, "is_active": r.is_active},
            )
            for r in rows
        ]

    def _search_settings(self, q: str, slug: str) -> list[SearchResultItem]:
        q_lower = q.lower()
        matches = [
            s for s in _SETTINGS_REGISTRY
            if q_lower in s["key"].lower() or q_lower in s["description"].lower()
        ][:_PER_CATEGORY]
        return [
            SearchResultItem(
                title=s["description"],
                subtitle=s["key"],
                url=s["url"].replace("{slug}", slug),
            )
            for s in matches
        ]
