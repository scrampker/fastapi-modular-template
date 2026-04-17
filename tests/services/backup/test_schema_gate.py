"""Restore rejects bundles with a newer schema_version than we support."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scottycore.services.backup.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    BackupManifest,
    BackupScope,
)
from scottycore.services.backup.service import BackupService, UnsupportedBundleError


def _bundle_with_schema(version: int) -> bytes:
    manifest = BackupManifest(
        schema_version=version,
        scope=BackupScope.PLATFORM,
        timestamp=datetime.now(timezone.utc),
        app_name="test",
        app_version="0.0.0",
        contributors=[],
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = manifest.model_dump_json(indent=2).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_service() -> BackupService:
    svc = BackupService.__new__(BackupService)
    svc._session_factory = None
    svc._audit = AsyncMock()
    svc._audit.log = AsyncMock()
    svc._app_name = "test"
    svc._app_version = "0.0.0"
    svc._contributors = {}
    return svc


@pytest.mark.asyncio
async def test_newer_schema_version_rejected() -> None:
    svc = _make_service()
    future = _bundle_with_schema(SUPPORTED_SCHEMA_VERSION + 1)

    with pytest.raises(UnsupportedBundleError):
        await svc.restore_bundle(future, user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_current_schema_version_accepted() -> None:
    svc = _make_service()
    current = _bundle_with_schema(SUPPORTED_SCHEMA_VERSION)

    # No registered contributors → empty restore, but no exception.
    summary = await svc.restore_bundle(current, user_id=uuid4(), ip="127.0.0.1")
    assert summary.total_rows_upserted == 0


@pytest.mark.asyncio
async def test_older_schema_version_accepted() -> None:
    svc = _make_service()
    old = _bundle_with_schema(0)  # pretend v0 bundle — still accepted

    summary = await svc.restore_bundle(old, user_id=uuid4(), ip="127.0.0.1")
    assert summary.total_rows_upserted == 0
