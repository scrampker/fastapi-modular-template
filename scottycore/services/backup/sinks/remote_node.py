"""RemoteNodeSink — write/read snapshots via SSH/SCP to another host.

Wraps ``ssh``/``scp`` CLIs (must be on PATH). A different deployment might
inject its own transport (Paramiko, asyncssh) but shelling out keeps the hard
dependency surface zero and makes auth transparent (ssh-agent, key file, etc.).

The remote layout mirrors :class:`LocalDiskSink` under ``remote_dir`` on the
target host — so a remote node can serve as a promotion target for a local
sink. List/delete invoke ``find``/``rm`` on the remote side.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkError,
    SinkNotFoundError,
    SinkWriteResult,
    SnapshotEntry,
    StorageSink,
    default_filename,
)


class RemoteNodeSink(StorageSink):
    """SSH/SCP-backed sink."""

    sink_type: ClassVar[str] = "remote_node"

    def __init__(
        self,
        *,
        host: str,
        remote_dir: str,
        user: str | None = None,
        port: int = 22,
        ssh_key: str | None = None,
        ssh_options: list[str] | None = None,
    ):
        self._host = host
        self._user = user
        self._port = port
        self._remote_dir = remote_dir.rstrip("/")
        self._ssh_key = ssh_key
        self._ssh_options = list(ssh_options or [])

    @property
    def target(self) -> str:
        return f"{self._user}@{self._host}" if self._user else self._host

    async def put(self, blob: BackupBlob) -> SinkWriteResult:
        rel = default_filename(blob)
        remote_path = f"{self._remote_dir}/{rel}"
        remote_dir = remote_path.rsplit("/", 1)[0]

        await self._ssh(["mkdir", "-p", remote_dir])

        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp.write(blob.data)
            tmp_path = Path(tmp.name)

        meta = {
            "sha256": blob.sha256,
            "size": blob.size,
            "app_slug": blob.app_slug,
            "scope": blob.scope,
            "kind": blob.kind,
            "encrypted": blob.encrypted,
            "key_fingerprint": blob.key_fingerprint,
            "tenant_slug": blob.tenant_slug,
            "created_at": blob.created_at.isoformat(),
            "metadata": blob.metadata,
        }
        sidecar_local = tmp_path.with_suffix(tmp_path.suffix + ".meta.json")
        sidecar_local.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        try:
            await self._scp_put(tmp_path, remote_path)
            await self._scp_put(sidecar_local, f"{remote_path}.meta.json")
        finally:
            tmp_path.unlink(missing_ok=True)
            sidecar_local.unlink(missing_ok=True)

        return SinkWriteResult(
            locator=rel,
            sink_type=self.sink_type,
            bytes_written=blob.size,
            created_at=blob.created_at,
        )

    async def get(self, locator: str) -> bytes:
        remote_path = f"{self._remote_dir}/{locator}"
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            exists = await self._ssh(["test", "-f", remote_path], check=False)
            if exists != 0:
                raise SinkNotFoundError(f"no snapshot at {locator}")
            await self._scp_get(remote_path, tmp_path)
            return tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

    async def list_snapshots(
        self, *, app_slug: str | None = None, tenant_slug: str | None = None
    ) -> list[SnapshotEntry]:
        # Remote find → read each sidecar. Could be optimised with tar/xargs;
        # keep simple until perf becomes an issue.
        rc, out, _ = await self._ssh_capture(
            ["find", self._remote_dir, "-name", "*.meta.json", "-type", "f"]
        )
        if rc != 0:
            return []
        sidecars = [p for p in out.splitlines() if p.strip()]
        entries: list[SnapshotEntry] = []
        for sc in sidecars:
            rc2, content, _ = await self._ssh_capture(["cat", sc])
            if rc2 != 0:
                continue
            try:
                meta = json.loads(content)
            except json.JSONDecodeError:
                continue
            if app_slug and meta.get("app_slug") != app_slug:
                continue
            if tenant_slug and meta.get("tenant_slug") != tenant_slug:
                continue
            bundle_remote = sc[: -len(".meta.json")]
            rel = bundle_remote[len(self._remote_dir) + 1 :]
            entries.append(
                SnapshotEntry(
                    locator=rel,
                    app_slug=meta.get("app_slug", ""),
                    scope=meta.get("scope", ""),
                    kind=meta.get("kind", "full"),
                    size=int(meta.get("size", 0)),
                    created_at=_parse_ts(meta.get("created_at")),
                    encrypted=bool(meta.get("encrypted", False)),
                    sha256=meta.get("sha256"),
                    key_fingerprint=meta.get("key_fingerprint"),
                    tenant_slug=meta.get("tenant_slug"),
                )
            )
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    async def delete(self, locator: str) -> None:
        remote_path = f"{self._remote_dir}/{locator}"
        rc = await self._ssh(["test", "-f", remote_path], check=False)
        if rc != 0:
            raise SinkNotFoundError(f"no snapshot at {locator}")
        await self._ssh(["rm", "-f", remote_path, f"{remote_path}.meta.json"])

    # ── SSH/SCP plumbing ───────────────────────────────────────────────────

    def _ssh_base(self) -> list[str]:
        cmd = ["ssh", "-p", str(self._port)]
        if self._ssh_key:
            cmd += ["-i", self._ssh_key]
        for opt in self._ssh_options:
            cmd += ["-o", opt]
        cmd.append(self.target)
        return cmd

    def _scp_base(self) -> list[str]:
        cmd = ["scp", "-P", str(self._port)]
        if self._ssh_key:
            cmd += ["-i", self._ssh_key]
        for opt in self._ssh_options:
            cmd += ["-o", opt]
        return cmd

    async def _ssh(self, argv: list[str], *, check: bool = True) -> int:
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_base(),
            "--",
            *[shlex.quote(a) for a in argv],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        rc = proc.returncode or 0
        if check and rc != 0:
            raise SinkError(f"ssh {argv} failed ({rc}): {err.decode(errors='replace')}")
        return rc

    async def _ssh_capture(self, argv: list[str]) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_base(),
            "--",
            *[shlex.quote(a) for a in argv],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def _scp_put(self, local: Path, remote_rel: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self._scp_base(),
            str(local),
            f"{self.target}:{remote_rel}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if (proc.returncode or 0) != 0:
            raise SinkError(f"scp put failed: {err.decode(errors='replace')}")

    async def _scp_get(self, remote_rel: str, local: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self._scp_base(),
            f"{self.target}:{remote_rel}",
            str(local),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if (proc.returncode or 0) != 0:
            raise SinkError(f"scp get failed: {err.decode(errors='replace')}")


def _parse_ts(raw: object) -> datetime:
    if not isinstance(raw, str):
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
