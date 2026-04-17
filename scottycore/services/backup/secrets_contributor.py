"""SecretsContributor — separate contributor for settings keys that look like secrets.

Why a separate contributor?
  * Operators can choose to *exclude* secrets from a backup bundle by
    filtering contributors at restore time without losing the rest of their
    settings (e.g. a bundle shared with a consultant).
  * Secrets are flagged in the contributor id so the UI can highlight the
    need for a passphrase.

Classification
--------------
A setting key is treated as a secret if its (case-insensitive) name matches
any of ``DEFAULT_SECRET_PATTERNS`` below, or any caller-supplied pattern.

Important
---------
This contributor does NOT perform its own encryption — it writes plaintext
values into the tarball exactly like :class:`SettingsContributor`. Bundle-
level GPG encryption is what protects the data at rest. When you export
without a passphrase, the audit log records the event with ``encrypted=False``
so downstream reviewers can spot an unsafe export.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.backup.contributors import _hydrate_row, _row_to_dict, _upsert_rows
from scottycore.services.backup.schemas import BackupScope, ContributorExport

DEFAULT_SECRET_PATTERNS: tuple[str, ...] = (
    r".*_secret$",
    r".*_password$",
    r".*_pwd$",
    r".*_token$",
    r".*_api_key$",
    r".*_apikey$",
    r".*_private_key$",
    r".*_passphrase$",
    r".*_webhook$",  # webhook URLs often carry signing secrets
)


class SecretsContributor:
    """Ships only the secret-looking settings rows."""

    contributor_id = "secrets"
    scopes: set[BackupScope] = {BackupScope.PLATFORM, BackupScope.TENANT}
    description = (
        "Settings whose keys match secret patterns (passwords, tokens, API keys). "
        "Export without a passphrase will include plaintext — use bundle encryption."
    )

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        patterns: tuple[str, ...] = DEFAULT_SECRET_PATTERNS,
    ) -> None:
        self._session_factory = session_factory
        self._regex = re.compile(
            "|".join(f"({p})" for p in patterns), re.IGNORECASE
        )

    def is_secret(self, key: str) -> bool:
        return bool(self._regex.fullmatch(key))

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        from scottycore.services.settings.models import Setting

        async with self._session_factory() as s:
            if scope == BackupScope.PLATFORM:
                rows = list(
                    (
                        await s.scalars(select(Setting).where(Setting.scope == "global"))
                    ).all()
                )
            else:
                rows = list(
                    (
                        await s.scalars(
                            select(Setting).where(
                                Setting.scope == "tenant",
                                Setting.scope_id == tenant_id,
                            )
                        )
                    ).all()
                )
        secret_rows = [r for r in rows if self.is_secret(r.key)]
        return ContributorExport(rows=[_row_to_dict(r) for r in secret_rows])

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        from scottycore.services.settings.models import Setting

        # Restore exactly as SettingsContributor would — the subset is already
        # filtered on export, but we re-check on restore so a tampered bundle
        # can't sneak non-secret settings in under this contributor id.
        safe_rows = [r for r in rows if self.is_secret(r.get("key", ""))]

        async with session_factory() as s:
            total = 0
            for row in safe_rows:
                instance = Setting(**_hydrate_row(Setting, row))
                await s.merge(instance)
                total += 1
            await s.commit()
        return total
