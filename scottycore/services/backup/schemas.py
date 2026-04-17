"""Pydantic schemas for the backup / restore subsystem."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class BackupScope(str, Enum):
    """Determines what data set is included in a backup bundle."""

    PLATFORM = "platform"
    TENANT = "tenant"


class ContributorExport(BaseModel):
    """Payload returned by a single contributor during an export pass.

    ``rows`` holds serialised DB rows as plain dicts.  All UUIDs must be
    strings; all datetimes must be ISO-8601 strings.  Binary blobs that are
    too large for JSON should be placed in ``files`` instead.

    ``files`` is a list of ``(relative_path, raw_bytes)`` pairs.  The backup
    service will place each pair under ``files/<contributor_id>/`` inside the
    tarball.
    """

    rows: list[dict[str, Any]] = Field(default_factory=list)
    files: list[tuple[str, bytes]] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class ContributorInfo(BaseModel):
    """Public metadata about a registered contributor (used by the list endpoint)."""

    id: str
    scopes: list[BackupScope]
    description: str


class ManifestContributorEntry(BaseModel):
    """Per-contributor summary written into the tarball manifest."""

    id: str
    rows: int
    files: int


#: Highest manifest ``schema_version`` this build of scottycore can restore.
#: Bump when the tarball layout or manifest fields change in a backward-
#: incompatible way. Restores reject manifests whose version is higher than
#: this constant — older bundles continue to work (backward compatibility is
#: handled inside contributors).
SUPPORTED_SCHEMA_VERSION: int = 1


class BackupManifest(BaseModel):
    """Top-level metadata for a backup bundle (written as manifest.json)."""

    schema_version: int = SUPPORTED_SCHEMA_VERSION
    scope: BackupScope
    tenant_slug: str | None = None  # only set when scope == TENANT
    timestamp: datetime
    app_name: str
    app_version: str
    contributors: list[ManifestContributorEntry] = Field(default_factory=list)


class RestoreSummary(BaseModel):
    """Returned to the caller after a restore operation completes."""

    scope: BackupScope
    tenant_slug: str | None = None
    contributors_restored: list[str] = Field(default_factory=list)
    total_rows_upserted: int = 0
    warnings: list[str] = Field(default_factory=list)
