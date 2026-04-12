"""Search service — cross-domain search across tenants, items, and settings."""

from __future__ import annotations

from uuid import UUID

from app.services.items.service import ItemsService
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
    """Composite service that searches across tenants, items, and settings.

    This service does NOT own a session_factory — it delegates all database
    access to the services it depends on (TenantsService, ItemsService).
    """

    def __init__(
        self,
        items: ItemsService,
        tenants: TenantsService,
    ) -> None:
        self._items = items
        self._tenants = tenants

    async def search(
        self,
        q: str,
        tenant_slug: str | None,
        is_superadmin: bool,
        tenant_id: UUID | None = None,
    ) -> SearchResults:
        """Run a cross-domain search and return grouped results."""
        like = f"%{q}%"

        tenants_list = (
            await self._search_tenants(like) if is_superadmin else []
        )
        settings = self._search_settings(q, tenant_slug or "default")
        items_list = (
            await self._search_items(tenant_id, q)
            if tenant_id is not None
            else []
        )

        results: dict[str, list[SearchResultItem]] = {}
        if tenants_list:
            results["tenants"] = tenants_list
        if settings:
            results["settings"] = settings
        if items_list:
            results["items"] = items_list

        total = sum(len(v) for v in results.values())
        return SearchResults(query=q, results=results, total=total, items=items_list)

    async def search_items(
        self,
        tenant_id: UUID,
        query: str,
        limit: int = _PER_CATEGORY,
    ) -> list[SearchResultItem]:
        """Search items by full-text search for a specific tenant."""
        return await self._search_items(tenant_id, query, limit)

    async def _search_items(
        self,
        tenant_id: UUID,
        q: str,
        limit: int = _PER_CATEGORY,
    ) -> list[SearchResultItem]:
        hits = await self._items.search_fts(tenant_id, q, limit)
        return [
            SearchResultItem(
                id=hit.item.id,
                title=hit.item.name,
                subtitle=hit.item.description,
                url=f"/c/{{slug}}/items/{hit.item.id}",
                highlight=hit.highlight,
            )
            for hit in hits
        ]

    async def _search_tenants(
        self,
        like: str,
    ) -> list[SearchResultItem]:
        tenants = await self._tenants.search_by_name(like, _PER_CATEGORY)
        return [
            SearchResultItem(
                id=r.id,
                title=r.name,
                subtitle=r.slug,
                url=f"/c/{r.slug}/dashboard",
                meta={"slug": r.slug, "is_active": r.is_active},
            )
            for r in tenants
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
