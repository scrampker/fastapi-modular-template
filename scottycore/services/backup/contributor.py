"""BackupContributor Protocol — the interface every contributor must implement.

Any object that satisfies this protocol can be registered with
``BackupService.register()``.  Use ``@runtime_checkable`` so callers can
do ``isinstance(obj, BackupContributor)`` if they need to.

Contributor contract
--------------------
``contributor_id``   — stable, slug-style identifier (e.g. "users", "accounts").
                       Must be unique across all registered contributors.
``scopes``           — the set of BackupScope values this contributor handles.
                       A contributor registered for both scopes will be called
                       once per export, with scope passed as an argument.
``description``      — one-line human-readable summary shown by the list endpoint.

``export(scope, tenant_id)``
    Called during a backup run.  ``tenant_id`` is None for PLATFORM scope and
    the target tenant's UUID string for TENANT scope.  Must return a
    ``ContributorExport`` with all data for this contributor's slice.

``restore(scope, tenant_id, rows, files, session_factory)``
    Called during a restore run.  Should upsert each row (insert-or-update by
    primary key).  Returns the count of rows actually written.  "replace" mode
    (delete-then-insert) is a future extension — note it in subclasses.

    ``session_factory`` is provided so contributors that need DB access can
    open their own sessions.  File-only contributors may ignore it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.backup.schemas import BackupScope, ContributorExport


@runtime_checkable
class BackupContributor(Protocol):
    contributor_id: str
    scopes: set[BackupScope]
    description: str

    async def export(
        self,
        scope: BackupScope,
        tenant_id: str | None,
    ) -> ContributorExport:
        """Produce the export payload for this contributor.

        Parameters
        ----------
        scope:
            The backup scope in progress.
        tenant_id:
            The target tenant's UUID string when ``scope == TENANT``,
            or ``None`` when ``scope == PLATFORM``.
        """
        ...

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        """Restore (upsert) rows and files for this contributor.

        Returns the number of rows upserted.  File-only contributors
        should return 0.

        Future extension: accept a ``mode`` parameter ("merge" vs "replace").
        For now only "merge" (insert-or-update) is implemented.
        """
        ...

    async def export_delta(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        since: datetime | None,
        exclude_origins: list[str],
    ) -> ContributorExport:
        """Produce a delta export payload (rows updated after ``since``).

        This method is OPTIONAL — callers use ``hasattr(contributor, "export_delta")``
        before calling it; contributors that don't implement it fall back to
        ``export()`` followed by an in-Python ``updated_at > since`` filter in
        the engine.

        ``since``            — if None, behaves identically to ``export()``.
        ``exclude_origins``  — rows whose origin node ID (looked up in
                               ``sync_row_origin``) matches any entry here should
                               be excluded to prevent echo.  Contributors may
                               skip this filter if they have no origin tracking;
                               the engine provides a fallback filter pass.
        """
        ...

    async def get_row(self, contributor_id: str, row_id: str) -> dict[str, Any] | None:
        """Fetch a single row as a plain dict for LWW comparison.

        This method is OPTIONAL — callers use ``hasattr(contributor, "get_row")``
        before calling it.  If not implemented the engine treats the row as
        absent (remote always wins).

        ``contributor_id``  — the contributor's stable ID string (passed for
                              convenience in multi-table contributors).
        ``row_id``          — the primary key value as a string.

        Returns the row dict (matching the format produced by ``export()``) or
        None if the row does not exist locally.
        """
        ...
