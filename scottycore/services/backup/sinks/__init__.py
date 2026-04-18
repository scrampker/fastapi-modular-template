"""Storage sinks for backup bundles.

See :mod:`scottycore.services.backup.sinks.base` for the abstract contract
and snapshot/locator conventions.
"""

from __future__ import annotations

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkError,
    SinkNotFoundError,
    SinkWriteResult,
    SnapshotEntry,
    StorageSink,
    default_filename,
)
from scottycore.services.backup.sinks.download import DownloadSink
from scottycore.services.backup.sinks.git_repo import GitRepoSink
from scottycore.services.backup.sinks.local_disk import LocalDiskSink
from scottycore.services.backup.sinks.remote_node import RemoteNodeSink
from scottycore.services.backup.sinks.scottydev import ScottyDevSink

__all__ = [
    "BackupBlob",
    "DownloadSink",
    "GitRepoSink",
    "LocalDiskSink",
    "RemoteNodeSink",
    "ScottyDevSink",
    "SinkError",
    "SinkNotFoundError",
    "SinkWriteResult",
    "SnapshotEntry",
    "StorageSink",
    "default_filename",
]
