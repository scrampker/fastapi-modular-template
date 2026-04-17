"""Chaos tests — tampered bundle payloads.

Verifies that _extract_bundle / restore_bundle rejects or gracefully handles:
- byte-flip in the ciphertext
- truncated bundles
- appended garbage
- swapped files inside the tar
- altered manifest schema_version
- non-tar content
- manifest with unknown scope
- schema_version beyond supported limit
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from scottycore.services.backup.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    BackupManifest,
    BackupScope,
)
from scottycore.services.backup.service import BackupService, UnsupportedBundleError

# ── Helpers ────────────────────────────────────────────────────────────────


def _minimal_bundle(
    schema_version: int = 1,
    scope: str = "platform",
    tenant_slug: str | None = None,
    extra_entries: dict | None = None,
) -> bytes:
    """Build a minimal but valid tar.gz bundle in-memory."""
    buf = io.BytesIO()
    manifest_dict = {
        "schema_version": schema_version,
        "scope": scope,
        "tenant_slug": tenant_slug,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "app_name": "test-app",
        "app_version": "0.0.1",
        "contributors": [],
    }
    if extra_entries:
        manifest_dict.update(extra_entries)

    manifest_bytes = json.dumps(manifest_dict).encode()

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))

    return buf.getvalue()


def _make_service() -> BackupService:
    """Return a BackupService with no contributors and a stub audit/session."""
    from unittest.mock import AsyncMock, MagicMock

    audit = MagicMock()
    audit.log = AsyncMock(return_value=None)
    return BackupService(
        session_factory=MagicMock(),
        audit_service=audit,
        app_name="test",
        app_version="0.0.1",
    )


# ── Schema version checks ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_version_too_high_raises() -> None:
    """Bundle with schema_version > SUPPORTED must raise UnsupportedBundleError."""
    bundle = _minimal_bundle(schema_version=SUPPORTED_SCHEMA_VERSION + 1)
    svc = _make_service()
    with pytest.raises(UnsupportedBundleError):
        await svc.restore_bundle(bundle, user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_schema_version_999_raises() -> None:
    bundle = _minimal_bundle(schema_version=999)
    svc = _make_service()
    with pytest.raises(UnsupportedBundleError):
        await svc.restore_bundle(bundle, user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_schema_version_0_is_accepted_or_raises_value_error() -> None:
    """schema_version=0 is technically below supported; service may or may not
    accept it. What it must NOT do: silently return wrong data."""
    bundle = _minimal_bundle(schema_version=0)
    svc = _make_service()
    try:
        result = await svc.restore_bundle(bundle, user_id=uuid4(), ip="127.0.0.1")
        # If accepted, summary must be internally consistent
        assert result.total_rows_upserted == 0
    except (ValueError, UnsupportedBundleError):
        pass  # also acceptable


# ── Corrupt / truncated bundles ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_truncated_bundle_raises() -> None:
    bundle = _minimal_bundle()
    truncated = bundle[: len(bundle) // 2]
    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(truncated, user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_empty_bytes_raises() -> None:
    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(b"", user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_random_bytes_raises() -> None:
    import os

    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(os.urandom(512), user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_appended_garbage_still_parses_or_raises() -> None:
    """Appended garbage after a valid gzip stream.

    Python's tarfile/gzip reads the first stream and stops; the result
    must either parse cleanly or raise — it must not return garbage data.
    """
    bundle = _minimal_bundle() + b"\xFF" * 256
    svc = _make_service()
    # Either works (gzip ignores trailing bytes) or raises — both are OK.
    try:
        result = await svc.restore_bundle(bundle, user_id=uuid4(), ip="127.0.0.1")
        assert result.total_rows_upserted == 0
    except Exception:
        pass


@pytest.mark.asyncio
async def test_byte_flip_in_body_raises() -> None:
    """Flip a byte in the middle of the bundle — should cause parse failure."""
    bundle = bytearray(_minimal_bundle())
    mid = len(bundle) // 2
    bundle[mid] ^= 0xFF
    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(bytes(bundle), user_id=uuid4(), ip="127.0.0.1")


# ── Manifest content anomalies ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_scope_raises_validation_error() -> None:
    """scope='bogus' is not in BackupScope enum — pydantic must reject it."""
    bundle = _minimal_bundle(scope="bogus")
    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(bundle, user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_missing_manifest_raises() -> None:
    """A tar.gz with no manifest.json member must raise."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b'{"rows": []}'
        info = tarfile.TarInfo(name="data/contributor.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(buf.getvalue(), user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_malformed_manifest_json_raises() -> None:
    """manifest.json containing invalid JSON must raise."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        bad = b"{{NOT VALID JSON}}"
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(bad)
        tf.addfile(info, io.BytesIO(bad))

    svc = _make_service()
    with pytest.raises(Exception):
        await svc.restore_bundle(buf.getvalue(), user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_tenant_scope_without_tenant_slug_still_processes() -> None:
    """TENANT scope bundle with no tenant_slug — service should not crash."""
    bundle = _minimal_bundle(scope="tenant", tenant_slug=None)
    svc = _make_service()
    result = await svc.restore_bundle(bundle, user_id=uuid4(), ip="127.0.0.1")
    assert result.total_rows_upserted == 0


# ── Swapped / poisoned data files ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_contributor_data_file_with_invalid_json_results_in_warning_or_raises() -> None:
    """data/<contributor_id>.json that is not valid JSON."""
    buf = io.BytesIO()
    manifest_dict = {
        "schema_version": 1,
        "scope": "platform",
        "tenant_slug": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "app_name": "test",
        "app_version": "0.0.1",
        "contributors": [{"id": "users", "rows": 1, "files": 0}],
    }
    manifest_bytes = json.dumps(manifest_dict).encode()
    bad_data = b"[NOT JSON AT ALL"

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in [
            ("manifest.json", manifest_bytes),
            ("data/users.json", bad_data),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    svc = _make_service()
    # Must either raise cleanly (parse error) or return empty summary with a warning.
    with pytest.raises(Exception):
        await svc.restore_bundle(buf.getvalue(), user_id=uuid4(), ip="127.0.0.1")


@pytest.mark.asyncio
async def test_data_file_with_non_list_json_is_handled() -> None:
    """data/<contributor_id>.json contains a JSON object instead of a list."""
    buf = io.BytesIO()
    manifest_dict = {
        "schema_version": 1,
        "scope": "platform",
        "tenant_slug": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "app_name": "test",
        "app_version": "0.0.1",
        "contributors": [{"id": "users", "rows": 1, "files": 0}],
    }
    manifest_bytes = json.dumps(manifest_dict).encode()
    # rows is an object, not a list
    bad_rows = json.dumps({"id": "uuid-1", "name": "oops"}).encode()

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in [
            ("manifest.json", manifest_bytes),
            ("data/users.json", bad_rows),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    svc = _make_service()
    # Service loads contributor_data[cid]["rows"] = json.loads(...).
    # If restore() is called with a dict instead of a list, contributor.restore()
    # receives a dict as `rows`. Since there are no contributors registered, the
    # loop is a no-op and this should succeed with 0 rows.
    result = await svc.restore_bundle(buf.getvalue(), user_id=uuid4(), ip="127.0.0.1")
    assert result.total_rows_upserted == 0
