"""Search service schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchQuery(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    tenant_slug: str | None = None


class SearchResultItem(BaseModel):
    id: str | None = None
    title: str
    subtitle: str | None = None
    url: str
    meta: dict | None = None


class SearchResults(BaseModel):
    query: str
    results: dict[str, list[SearchResultItem]]
    total: int
