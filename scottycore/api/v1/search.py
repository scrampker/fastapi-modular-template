"""Global search API — powers the Ctrl+K command palette."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from scottycore.core.auth import require_auth
from scottycore.core.dependencies import get_search_service
from scottycore.services.auth.schemas import UserContext
from scottycore.services.search.schemas import SearchResults
from scottycore.services.search.service import SearchService

router = APIRouter()


@router.get("", response_model=SearchResults)
async def global_search(
    q: str = Query(min_length=1, max_length=200, description="Search query"),
    tenant: str | None = Query(default=None, description="Tenant slug to scope the search"),
    user: UserContext = Depends(require_auth),
    svc: SearchService = Depends(get_search_service),
) -> SearchResults:
    """Search across items, tenants, and settings.

    Results are scoped to the given tenant when ``tenant`` is provided.
    The ``tenants`` result group is only populated for superadmin users.
    """
    return await svc.search(
        q=q,
        tenant_slug=tenant,
        is_superadmin=user.is_superadmin,
    )
