"""Chaos tests — path traversal in LocalDiskSink.

Attempts to escape the sink root via:
- "../../../etc/passwd" style locators
- absolute paths
- null bytes in locators
- URL-encoded traversal sequences
- deep relative paths that resolve outside root
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scottycore.services.backup.sinks.base import BackupBlob, SinkNotFoundError
from scottycore.services.backup.sinks.local_disk import LocalDiskSink


def _blob(data: bytes = b"payload") -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256="deadbeef",
        size=len(data),
        app_slug="test",
        scope="platform",
        kind="full",
        created_at=datetime.now(timezone.utc),
        encrypted=False,
    )


@pytest.fixture()
def sink(tmp_path: Path) -> LocalDiskSink:
    root = tmp_path / "sink_root"
    root.mkdir()
    return LocalDiskSink(root)


# ── get() traversal attempts ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_with_dotdot_locator_raises(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.get("../../etc/passwd")


@pytest.mark.asyncio
async def test_get_with_absolute_locator_raises(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.get("/etc/passwd")


@pytest.mark.asyncio
async def test_get_with_deep_traversal_raises(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.get("a/b/../../../../../../../../etc/shadow")


@pytest.mark.asyncio
async def test_get_with_null_byte_locator_raises(sink: LocalDiskSink) -> None:
    """Null bytes in path should not silently truncate and land elsewhere."""
    with pytest.raises((SinkNotFoundError, ValueError, OSError)):
        await sink.get("valid_dir\x00/../../../etc/passwd")


@pytest.mark.asyncio
async def test_get_with_url_encoded_traversal_raises(sink: LocalDiskSink) -> None:
    """URL-encoded %2e%2e sequences must not be decoded into path components."""
    with pytest.raises(SinkNotFoundError):
        await sink.get("%2e%2e/%2e%2e/etc/passwd")


@pytest.mark.asyncio
async def test_get_with_tilde_expansion_does_not_escape(
    sink: LocalDiskSink, tmp_path: Path
) -> None:
    """~/something should resolve relative to root, not the home directory."""
    # Create a file under the sink root named "~" just in case
    with pytest.raises((SinkNotFoundError, FileNotFoundError, ValueError)):
        await sink.get("~/secret_file")


# ── delete() traversal attempts ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_with_dotdot_locator_raises(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.delete("../../etc/passwd")


@pytest.mark.asyncio
async def test_delete_with_absolute_path_raises(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.delete("/tmp/legit_file")


# ── verify() traversal attempts ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_with_dotdot_raises(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.verify("../../etc/hostname", "deadbeef")


# ── put() locator field does not control written path ─────────────────────


@pytest.mark.asyncio
async def test_put_uses_default_filename_not_user_supplied_path(
    sink: LocalDiskSink, tmp_path: Path
) -> None:
    """put() derives the filename from blob metadata (default_filename),
    not from any user-supplied locator. The written file must be under sink.root.
    """
    result = await sink.put(_blob())
    written = (sink.root / result.locator).resolve()
    assert str(written).startswith(str(sink.root))


# ── Symlink race (best-effort check) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_symlink_outside_root_is_rejected_on_get(
    sink: LocalDiskSink, tmp_path: Path
) -> None:
    """Even if someone pre-places a symlink under sink.root pointing outside,
    _resolve() must detect it (because .resolve() follows symlinks).
    """
    # Create a file outside the sink
    secret = tmp_path / "outside.txt"
    secret.write_bytes(b"outside content")

    # Plant a symlink inside the sink root pointing to the outside file
    link = sink.root / "escape_link"
    link.symlink_to(secret)

    # _resolve() calls Path.resolve() which follows the symlink, then checks
    # _is_within(root, resolved_target).  Since outside.txt is NOT under sink.root,
    # the check must raise SinkNotFoundError.  This confirms the protection works.
    with pytest.raises(SinkNotFoundError):
        await sink.get("escape_link")
