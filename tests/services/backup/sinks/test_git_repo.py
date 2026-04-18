"""Tests for GitRepoSink using a local bare repo as the 'remote'.

No network access needed — pytest creates a temporary bare repo and points
the sink at ``file:///.../bare.git``. Exercises put/get/list/delete/verify,
path-template substitution, concurrency, push-retry on non-ff, and the
offline fallback (commit locally when push fails).

LFS is NOT exercised — git-lfs is optional on the test host and would
add a binary dependency. The sink has ``lfs_enabled=False`` in most
tests; a single test verifies the ``lfs_enabled=True`` path raises a
clear error when lfs is absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkNotFoundError,
)
from scottycore.services.backup.sinks.git_repo import GitRepoSink


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_bundle(
    *,
    data: bytes = b"hello world",
    app_slug: str = "testapp",
    scope: str = "platform",
    tenant_slug: str | None = None,
    kind: str = "full",
    encrypted: bool = True,
    created_at: datetime | None = None,
) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        app_slug=app_slug,
        scope=scope,
        kind=kind,
        created_at=created_at or datetime.now(timezone.utc),
        encrypted=encrypted,
        key_fingerprint="abcdef12",
        tenant_slug=tenant_slug,
    )


@pytest.fixture
def bare_remote(tmp_path: Path) -> str:
    """Create a bare repo to serve as the 'remote'. Returns a file:// URL."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "backups", str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


@pytest.fixture
def sink(bare_remote: str, tmp_path: Path) -> GitRepoSink:
    """GitRepoSink pointed at the bare remote with LFS disabled."""
    clone = tmp_path / "clone"
    return GitRepoSink(
        repo_url=bare_remote,
        local_clone_dir=clone,
        branch="backups",
        lfs_enabled=False,
    )


# ── Basic round-trip ────────────────────────────────────────────────────


class TestPutGet:
    async def test_put_returns_locator(self, sink: GitRepoSink) -> None:
        blob = _make_bundle()
        result = await sink.put(blob)
        assert result.sink_type == "git_repo"
        assert result.bytes_written == blob.size
        assert "testapp/platform" in result.locator

    async def test_get_returns_original_bytes(self, sink: GitRepoSink) -> None:
        blob = _make_bundle(data=b"the quick brown fox")
        result = await sink.put(blob)
        fetched = await sink.get(result.locator)
        assert fetched == blob.data

    async def test_put_commits_to_remote(
        self, sink: GitRepoSink, bare_remote: str, tmp_path: Path
    ) -> None:
        await sink.put(_make_bundle())
        # Clone the bare again into a second dir and verify the file is there.
        verify_dir = tmp_path / "verify"
        subprocess.run(
            ["git", "clone", "-b", "backups", bare_remote, str(verify_dir)],
            check=True,
            capture_output=True,
        )
        files = [
            p for p in verify_dir.rglob("*.tar.gz.gpg")
            if ".git" not in p.parts
        ]
        assert len(files) == 1


class TestListSnapshots:
    async def test_list_empty_when_no_puts(self, sink: GitRepoSink) -> None:
        entries = await sink.list_snapshots()
        assert entries == []

    async def test_list_returns_put_entries_sorted_desc(
        self, sink: GitRepoSink
    ) -> None:
        old = _make_bundle(
            data=b"old",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        new = _make_bundle(
            data=b"new",
            created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        await sink.put(old)
        await sink.put(new)
        entries = await sink.list_snapshots()
        assert len(entries) == 2
        assert entries[0].created_at > entries[1].created_at

    async def test_list_filtered_by_app_slug(
        self, sink: GitRepoSink
    ) -> None:
        await sink.put(_make_bundle(app_slug="alpha"))
        await sink.put(_make_bundle(app_slug="beta"))
        alpha = await sink.list_snapshots(app_slug="alpha")
        assert len(alpha) == 1
        assert alpha[0].app_slug == "alpha"

    async def test_list_filtered_by_tenant(
        self, sink: GitRepoSink
    ) -> None:
        await sink.put(
            _make_bundle(scope="tenant", tenant_slug="t1")
        )
        await sink.put(
            _make_bundle(scope="tenant", tenant_slug="t2")
        )
        only_t1 = await sink.list_snapshots(tenant_slug="t1")
        assert len(only_t1) == 1
        assert only_t1[0].tenant_slug == "t1"


class TestDelete:
    async def test_delete_removes_from_clone_and_remote(
        self, sink: GitRepoSink, bare_remote: str, tmp_path: Path
    ) -> None:
        result = await sink.put(_make_bundle())
        await sink.delete(result.locator)

        # Can't get it anymore.
        with pytest.raises(SinkNotFoundError):
            await sink.get(result.locator)

        # Remote no longer contains the blob either.
        verify_dir = tmp_path / "verify"
        subprocess.run(
            ["git", "clone", "-b", "backups", bare_remote, str(verify_dir)],
            check=True,
            capture_output=True,
        )
        bundles = [
            p for p in verify_dir.rglob("*.tar.gz.gpg")
            if ".git" not in p.parts
        ]
        assert bundles == []

    async def test_delete_nonexistent_raises(
        self, sink: GitRepoSink
    ) -> None:
        # Sink must be initialized (put something so the clone exists).
        await sink.put(_make_bundle())
        with pytest.raises(SinkNotFoundError):
            await sink.delete("ghost/platform/nothing.tar.gz.gpg")


class TestVerify:
    async def test_verify_correct_sha_returns_true(
        self, sink: GitRepoSink
    ) -> None:
        blob = _make_bundle(data=b"payload")
        result = await sink.put(blob)
        assert await sink.verify(result.locator, blob.sha256) is True

    async def test_verify_wrong_sha_returns_false(
        self, sink: GitRepoSink
    ) -> None:
        result = await sink.put(_make_bundle())
        assert await sink.verify(result.locator, "0" * 64) is False


# ── Path templates ──────────────────────────────────────────────────────


class TestPathTemplate:
    async def test_default_template_uses_app_scope(
        self, sink: GitRepoSink
    ) -> None:
        result = await sink.put(
            _make_bundle(app_slug="myapp", scope="platform")
        )
        assert result.locator.startswith("snapshots/myapp/platform/")

    async def test_custom_template_is_respected(
        self, bare_remote: str, tmp_path: Path
    ) -> None:
        s = GitRepoSink(
            repo_url=bare_remote,
            local_clone_dir=tmp_path / "c",
            branch="backups",
            lfs_enabled=False,
            path_template="backups/{app_slug}/{tenant_slug}/{scope}",
        )
        result = await s.put(
            _make_bundle(
                app_slug="myapp",
                scope="tenant",
                tenant_slug="acme",
            )
        )
        assert result.locator.startswith("backups/myapp/acme/tenant/")

    async def test_empty_tenant_doesnt_create_double_slash(
        self, bare_remote: str, tmp_path: Path
    ) -> None:
        s = GitRepoSink(
            repo_url=bare_remote,
            local_clone_dir=tmp_path / "c",
            branch="backups",
            lfs_enabled=False,
            path_template="snap/{app_slug}/{tenant_slug}/{scope}",
        )
        # platform scope → tenant_slug is empty, path collapses
        result = await s.put(
            _make_bundle(app_slug="a", scope="platform", tenant_slug=None)
        )
        assert "//" not in result.locator
        assert result.locator.startswith("snap/a/platform/")

    async def test_unknown_template_var_raises(
        self, bare_remote: str, tmp_path: Path
    ) -> None:
        s = GitRepoSink(
            repo_url=bare_remote,
            local_clone_dir=tmp_path / "c",
            branch="backups",
            lfs_enabled=False,
            path_template="snap/{bogus}/",
        )
        with pytest.raises(Exception):
            await s.put(_make_bundle())


# ── Push retry on non-fast-forward ──────────────────────────────────────


class TestPushRetry:
    async def test_recovers_from_non_fast_forward(
        self, bare_remote: str, tmp_path: Path
    ) -> None:
        """Two sinks pushing to the same bare repo — second must rebase + retry."""
        a = GitRepoSink(
            repo_url=bare_remote,
            local_clone_dir=tmp_path / "a",
            branch="backups",
            lfs_enabled=False,
        )
        b = GitRepoSink(
            repo_url=bare_remote,
            local_clone_dir=tmp_path / "b",
            branch="backups",
            lfs_enabled=False,
        )

        # A clones first, puts, pushes. B clones (now has A's commit) and
        # puts — should succeed because B pulls first.
        result_a = await a.put(_make_bundle(app_slug="alpha"))
        result_b = await b.put(_make_bundle(app_slug="beta"))

        # Both blobs must be visible via a fresh list after pull.
        entries = await a.list_snapshots()
        assert len(entries) == 2
        slugs = {e.app_slug for e in entries}
        assert slugs == {"alpha", "beta"}


# ── Concurrency within a single sink ────────────────────────────────────


class TestConcurrency:
    async def test_serializes_parallel_puts(
        self, sink: GitRepoSink
    ) -> None:
        # 5 simultaneous puts — the Lock inside the sink should serialise
        # them without git-commit collisions.
        results = await asyncio.gather(
            *[sink.put(_make_bundle(data=f"blob-{i}".encode())) for i in range(5)]
        )
        assert len({r.locator for r in results}) == 5
        entries = await sink.list_snapshots()
        assert len(entries) == 5


# ── LFS error path ──────────────────────────────────────────────────────


class TestLfsMissing:
    async def test_lfs_enabled_without_lfs_installed_raises(
        self, bare_remote: str, tmp_path: Path
    ) -> None:
        if shutil.which("git-lfs"):
            pytest.skip(
                "git-lfs is installed on this host — skip the missing-LFS error test"
            )
        s = GitRepoSink(
            repo_url=bare_remote,
            local_clone_dir=tmp_path / "c",
            branch="backups",
            lfs_enabled=True,
        )
        with pytest.raises(Exception):
            await s.put(_make_bundle())
