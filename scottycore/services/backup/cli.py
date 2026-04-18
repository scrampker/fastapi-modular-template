"""``scottycore-backup`` CLI — run backups/restores without the HTTP layer.

Exposed as ``console_scripts`` in pyproject.toml. Intended for use during
disaster-recovery, ops scripts, and CI — any time you want a backup operation
that doesn't go through the running web process.

Subcommands
-----------
* ``export``      — run a full export to a sink
* ``restore``     — restore a bundle from a sink
* ``verify``      — re-hash a bundle and compare against its sha256
* ``list-runs``   — print recent BackupRun rows as JSON
* ``rotate-key``  — re-encrypt an existing bundle with a new passphrase

Authentication
--------------
The CLI reads scottycore's Settings (i.e. DATABASE_URL, APP_NAME, …) directly
from the environment / .env — no HTTP login. Anyone with shell access to a
host where the app runs can use this; treat it accordingly.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from scottycore.core.brand import get_brand
from scottycore.services.backup.crypto import (
    CryptoError,
    decrypt_bundle,
    encrypt_bundle,
    fingerprint,
)
from scottycore.services.backup.models import BackupRun
from scottycore.services.backup.schemas import BackupScope
from scottycore.services.backup.service import BackupService, UnsupportedBundleError
from scottycore.services.backup.sinks import (
    BackupBlob,
    DownloadSink,
    LocalDiskSink,
    ScottyDevSink,
    SinkNotFoundError,
    StorageSink,
)
from scottycore.services.backup.wiring import build_backup_service


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    if not ns.command:
        parser.print_help()
        return 2

    try:
        return asyncio.run(_dispatch(ns))
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, CryptoError, UnsupportedBundleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    brand = get_brand()
    p = argparse.ArgumentParser(
        prog=f"{brand.framework_name}-backup",
        description=(
            f"Run {brand.framework_name} backups and restores without the "
            "HTTP API."
        ),
    )
    sub = p.add_subparsers(dest="command")

    ex = sub.add_parser("export", help="export a backup bundle to a sink")
    ex.add_argument("--scope", choices=["platform", "tenant"], default="platform")
    ex.add_argument("--tenant-slug", help="required when --scope=tenant")
    ex.add_argument("--sink", default="local_disk", choices=_sink_choices())
    ex.add_argument(
        "--root-dir", help="local_disk root dir (defaults to /app/data/backups)"
    )
    ex.add_argument(
        "--base-url",
        help="orchestrator sink base URL (the ScottyDev-compatible API)",
    )
    ex.add_argument(
        "--token",
        help="orchestrator sink bearer token (or $SCOTTYDEV_TOKEN)",
    )
    ex.add_argument(
        "--repo-url",
        help="git_repo sink remote URL (any forge: forgejo/github/gitlab/ADO)",
    )
    ex.add_argument(
        "--clone-dir",
        help="git_repo sink local working-clone dir (default /app/data/backups-git)",
    )
    ex.add_argument(
        "--branch",
        help="git_repo sink branch (default 'backups')",
    )
    ex.add_argument(
        "--path-template",
        help=(
            "git_repo sink in-repo path template — supports {app_slug}, "
            "{scope}, {tenant_slug}, {kind}, {timestamp}. Default: "
            "snapshots/{app_slug}/{scope}/{tenant_slug}"
        ),
    )
    ex.add_argument(
        "--no-lfs",
        action="store_true",
        help="git_repo sink: disable git-LFS (stores blobs as plain objects)",
    )
    ex.add_argument(
        "--passphrase",
        help="encrypt with GPG — reads from stdin if the value is '-'",
    )
    ex.add_argument(
        "--out",
        help="download mode only — write the bundle to this file path",
    )

    rs = sub.add_parser("restore", help="restore a bundle from a sink")
    rs.add_argument("--sink", default="local_disk", choices=_sink_choices())
    rs.add_argument("--root-dir")
    rs.add_argument("--base-url")
    rs.add_argument("--token")
    rs.add_argument("--repo-url")
    rs.add_argument("--clone-dir")
    rs.add_argument("--branch")
    rs.add_argument("--path-template")
    rs.add_argument("--no-lfs", action="store_true")
    rs.add_argument("--locator", required=True, help="sink-specific locator")
    rs.add_argument("--passphrase", help="decrypt with GPG — '-' for stdin")

    vf = sub.add_parser("verify", help="re-hash a bundle and compare to sha256")
    vf.add_argument("--sink", default="local_disk", choices=_sink_choices())
    vf.add_argument("--root-dir")
    vf.add_argument("--base-url")
    vf.add_argument("--token")
    vf.add_argument("--repo-url")
    vf.add_argument("--clone-dir")
    vf.add_argument("--branch")
    vf.add_argument("--path-template")
    vf.add_argument("--no-lfs", action="store_true")
    vf.add_argument("--locator", required=True)
    vf.add_argument("--expected-sha256", required=True)

    lr = sub.add_parser("list-runs", help="print recent BackupRun rows as JSON")
    lr.add_argument("--limit", type=int, default=20)

    rk = sub.add_parser(
        "rotate-key", help="re-encrypt a local bundle with a new passphrase"
    )
    rk.add_argument("--in", dest="infile", required=True)
    rk.add_argument("--out", dest="outfile", required=True)
    rk.add_argument(
        "--old-passphrase",
        help="decrypt input with this passphrase ('-' for stdin)",
        required=True,
    )
    rk.add_argument(
        "--new-passphrase",
        help="re-encrypt with this passphrase ('-' for stdin)",
        required=True,
    )

    return p


def _sink_choices() -> list[str]:
    return ["local_disk", "scottydev", "download", "git_repo"]


async def _dispatch(ns: argparse.Namespace) -> int:
    if ns.command == "export":
        return await _cmd_export(ns)
    if ns.command == "restore":
        return await _cmd_restore(ns)
    if ns.command == "verify":
        return await _cmd_verify(ns)
    if ns.command == "list-runs":
        return await _cmd_list_runs(ns)
    if ns.command == "rotate-key":
        return await _cmd_rotate_key(ns)
    return 2


# ── Commands ───────────────────────────────────────────────────────────────


async def _cmd_export(ns: argparse.Namespace) -> int:
    factory = _session_factory()
    svc = _make_service(factory)

    user_id = _ops_user_id()
    ip = "127.0.0.1"

    if ns.scope == "platform":
        bundle = await svc.export_platform(user_id=user_id, ip=ip)
        tenant_slug = None
    else:
        if not ns.tenant_slug:
            raise ValueError("--tenant-slug is required for --scope=tenant")
        from scottycore.services.tenants.models import Tenant

        async with factory() as s:
            t = (
                await s.scalars(select(Tenant).where(Tenant.slug == ns.tenant_slug))
            ).first()
        if t is None:
            raise ValueError(f"tenant not found: {ns.tenant_slug}")
        bundle = await svc.export_tenant(
            tenant_id=str(t.id),
            tenant_slug=t.slug,
            user_id=user_id,
            ip=ip,
        )
        tenant_slug = t.slug

    passphrase = _resolve_passphrase(ns.passphrase)
    key_fp: str | None = None
    if passphrase:
        bundle = await encrypt_bundle(bundle, passphrase)
        key_fp = fingerprint(passphrase)

    if ns.sink == "download":
        target = Path(ns.out or _default_download_path(svc._app_name, ns.scope, tenant_slug))  # noqa: SLF001
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "sink": "download",
                    "path": str(target),
                    "size": len(bundle),
                    "sha256": hashlib.sha256(bundle).hexdigest(),
                    "encrypted": bool(passphrase),
                    "key_fingerprint": key_fp,
                }
            )
        )
        return 0

    sink = _build_sink(ns)
    blob = BackupBlob(
        data=bundle,
        sha256=hashlib.sha256(bundle).hexdigest(),
        size=len(bundle),
        app_slug=svc._app_name,  # noqa: SLF001
        scope=ns.scope,
        kind="full",
        created_at=datetime.now(timezone.utc),
        encrypted=bool(passphrase),
        key_fingerprint=key_fp,
        tenant_slug=tenant_slug,
    )
    result = await sink.put(blob)

    async with factory() as s:
        s.add(
            BackupRun(
                app_slug=blob.app_slug,
                scope=blob.scope,
                tenant_slug=blob.tenant_slug,
                kind="full",
                status="success",
                sink_type=sink.sink_type,
                sink_locator=result.locator,
                bytes_written=result.bytes_written,
                sha256=blob.sha256,
                encrypted=blob.encrypted,
                key_fingerprint=key_fp,
                started_at=blob.created_at,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    print(
        json.dumps(
            {
                "status": "ok",
                "sink": sink.sink_type,
                "locator": result.locator,
                "size": blob.size,
                "sha256": blob.sha256,
                "encrypted": blob.encrypted,
                "key_fingerprint": key_fp,
            }
        )
    )
    return 0


async def _cmd_restore(ns: argparse.Namespace) -> int:
    factory = _session_factory()
    svc = _make_service(factory)

    sink = _build_sink(ns)
    try:
        data = await sink.get(ns.locator)
    except SinkNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    passphrase = _resolve_passphrase(ns.passphrase)
    if passphrase:
        data = await decrypt_bundle(data, passphrase)

    summary = await svc.restore_bundle(
        data, user_id=_ops_user_id(), ip="127.0.0.1"
    )
    print(summary.model_dump_json(indent=2))
    return 0


async def _cmd_verify(ns: argparse.Namespace) -> int:
    sink = _build_sink(ns)
    ok = await sink.verify(ns.locator, ns.expected_sha256)
    print(json.dumps({"locator": ns.locator, "matches": ok}))
    return 0 if ok else 2


async def _cmd_list_runs(ns: argparse.Namespace) -> int:
    factory = _session_factory()
    async with factory() as s:
        rows = list(
            (
                await s.scalars(
                    select(BackupRun)
                    .order_by(BackupRun.created_at.desc())
                    .limit(ns.limit)
                )
            ).all()
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r.id,
                "app_slug": r.app_slug,
                "scope": r.scope,
                "kind": r.kind,
                "status": r.status,
                "sink_type": r.sink_type,
                "sink_locator": r.sink_locator,
                "bytes_written": r.bytes_written,
                "sha256": r.sha256,
                "encrypted": r.encrypted,
                "key_fingerprint": r.key_fingerprint,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    print(json.dumps(out, indent=2))
    return 0


async def _cmd_rotate_key(ns: argparse.Namespace) -> int:
    old = _resolve_passphrase(ns.old_passphrase) or ""
    new = _resolve_passphrase(ns.new_passphrase) or ""
    if not old or not new:
        raise ValueError("both --old-passphrase and --new-passphrase are required")

    src = Path(ns.infile)
    data = await decrypt_bundle(src.read_bytes(), old)
    rotated = await encrypt_bundle(data, new)

    dst = Path(ns.outfile)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(rotated)

    print(
        json.dumps(
            {
                "status": "ok",
                "out": str(dst),
                "size": len(rotated),
                "sha256": hashlib.sha256(rotated).hexdigest(),
                "old_fingerprint": fingerprint(old),
                "new_fingerprint": fingerprint(new),
            }
        )
    )
    return 0


# ── Helpers ────────────────────────────────────────────────────────────────


def _session_factory() -> async_sessionmaker:
    from scottycore.core.database import _build_engine

    engine = _build_engine()
    from sqlalchemy.ext.asyncio import AsyncSession

    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _make_service(factory) -> BackupService:
    from scottycore.services.audit.service import AuditService

    audit = AuditService(factory)
    return build_backup_service(factory, audit)


def _build_sink(ns: argparse.Namespace) -> StorageSink:
    if ns.sink == "download":
        return DownloadSink()
    if ns.sink == "local_disk":
        return LocalDiskSink(ns.root_dir or "/app/data/backups")
    if ns.sink == "scottydev":
        import os

        token = ns.token or os.environ.get("SCOTTYDEV_TOKEN")
        if not ns.base_url:
            raise ValueError(
                "--base-url is required for the orchestrator sink"
            )
        return ScottyDevSink(base_url=ns.base_url, token=token)
    if ns.sink == "git_repo":
        from scottycore.services.backup.sinks import GitRepoSink

        if not getattr(ns, "repo_url", None):
            raise ValueError("--repo-url is required for the git_repo sink")
        clone_dir = getattr(ns, "clone_dir", None) or "/app/data/backups-git"
        return GitRepoSink(
            repo_url=ns.repo_url,
            local_clone_dir=clone_dir,
            branch=getattr(ns, "branch", None) or "backups",
            path_template=getattr(ns, "path_template", None)
            or "snapshots/{app_slug}/{scope}/{tenant_slug}",
            lfs_enabled=not getattr(ns, "no_lfs", False),
        )
    raise ValueError(f"unknown sink: {ns.sink}")


def _resolve_passphrase(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw == "-":
        if sys.stdin.isatty():
            return getpass.getpass("passphrase: ")
        return sys.stdin.read().rstrip("\n")
    return raw


def _ops_user_id() -> UUID:
    """Placeholder UUID for CLI-driven runs (audit records 'ops-cli' context)."""
    # Deterministic UUID so audit rows are grouped: uuid5(DNS, "ops-cli")
    import uuid as _uuid

    return _uuid.uuid5(_uuid.NAMESPACE_DNS, "ops-cli.scottycore")


def _default_download_path(app_name: str, scope: str, tenant: str | None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"tenant-{tenant}" if tenant else scope
    return f"{app_name}-{tag}-{ts}.tar.gz"


if __name__ == "__main__":
    sys.exit(main())
