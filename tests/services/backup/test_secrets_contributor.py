"""SecretsContributor — classification + export/restore scoping."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scottycore.core.database import Base
from scottycore.services.backup.schemas import BackupScope
from scottycore.services.backup.secrets_contributor import SecretsContributor
from scottycore.services.settings.models import Setting


@pytest_asyncio.fixture
async def factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_classifies_secret_keys(factory) -> None:
    c = SecretsContributor(factory)
    assert c.is_secret("openai_api_key")
    assert c.is_secret("slack_webhook")
    assert c.is_secret("db_password")
    assert c.is_secret("oauth_token")
    assert not c.is_secret("app_name")
    assert not c.is_secret("feature_flag_enabled")


@pytest.mark.asyncio
async def test_export_only_returns_secret_rows_at_platform_scope(factory) -> None:
    async with factory() as s:
        s.add_all(
            [
                Setting(scope="global", key="app_name", value_json='"demo"'),
                Setting(scope="global", key="openai_api_key", value_json='"sk-x"'),
                Setting(scope="global", key="smtp_password", value_json='"pw"'),
                Setting(
                    scope="tenant",
                    scope_id="t1",
                    key="tenant_api_key",
                    value_json='"tk"',
                ),
            ]
        )
        await s.commit()

    c = SecretsContributor(factory)
    export = await c.export(BackupScope.PLATFORM, tenant_id=None)
    keys = {r["key"] for r in export.rows}
    assert keys == {"openai_api_key", "smtp_password"}


@pytest.mark.asyncio
async def test_export_tenant_scope_filters_by_tenant(factory) -> None:
    async with factory() as s:
        s.add_all(
            [
                Setting(
                    scope="tenant",
                    scope_id="t1",
                    key="openai_api_key",
                    value_json='"one"',
                ),
                Setting(
                    scope="tenant",
                    scope_id="t2",
                    key="openai_api_key",
                    value_json='"two"',
                ),
                Setting(
                    scope="tenant",
                    scope_id="t1",
                    key="team_size",
                    value_json="10",
                ),
            ]
        )
        await s.commit()

    c = SecretsContributor(factory)
    export = await c.export(BackupScope.TENANT, tenant_id="t1")
    assert len(export.rows) == 1
    assert export.rows[0]["value_json"] == '"one"'


@pytest.mark.asyncio
async def test_restore_re_filters_to_secret_rows_only(factory) -> None:
    c = SecretsContributor(factory)
    upserted = await c.restore(
        scope=BackupScope.PLATFORM,
        tenant_id=None,
        rows=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "scope": "global",
                "scope_id": None,
                "key": "openai_api_key",
                "value_json": '"sk-xyz"',
            },
            {
                # Trojan: a non-secret key smuggled in as this contributor.
                "id": "22222222-2222-2222-2222-222222222222",
                "scope": "global",
                "scope_id": None,
                "key": "app_name",
                "value_json": '"evil-overwrite"',
            },
        ],
        files=[],
        session_factory=factory,
    )
    assert upserted == 1  # only the API key, the trojan was filtered

    async with factory() as s:
        rows = list(
            (await s.execute(Setting.__table__.select())).mappings()
        )
    keys = {r["key"] for r in rows}
    assert keys == {"openai_api_key"}


@pytest.mark.asyncio
async def test_custom_patterns_override_defaults(factory) -> None:
    c = SecretsContributor(factory, patterns=(r"^gpg_.*",))
    assert c.is_secret("gpg_passphrase") is True
    assert c.is_secret("openai_api_key") is False
