"""``scottycore-update-check`` CLI.

Wire into systemd as a minute-tick timer:

    [Unit]
    Description=scottycore update poller (%i)
    [Service]
    Type=oneshot
    WorkingDirectory=/opt/scottycore/%i
    ExecStart=/usr/local/bin/scottycore-update-check --repo-root /opt/scottycore/%i
    User=root
    Environment="DATABASE_URL=postgresql+asyncpg://..."
    # ...plus whatever settings.get_settings() needs to boot
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.auto_update.service import AutoUpdateService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scottycore-update-check",
        description="Poll the scottycore pin and notify/auto-update per settings.",
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Path to the app repo (where pyproject.toml lives)",
    )
    parser.add_argument(
        "--forge-api",
        default="https://api.github.com/repos/scrampker/scottycore",
        help="Forge API base URL (github or forgejo-compatible)",
    )
    ns = parser.parse_args(argv)

    try:
        result = asyncio.run(_run(ns))
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, default=str))
    if result["action_taken"] == "auto-updated":
        # Signal the host runner to rebuild. Exit 75 = "please restart me"
        # (Python's EX_TEMPFAIL convention, also used by scottycore's admin
        # restart endpoint).
        return 75
    if result["action_taken"] == "error":
        return 1
    return 0


async def _run(ns: argparse.Namespace) -> dict:
    from scottycore.core.database import _build_engine

    engine = _build_engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    svc = AutoUpdateService(
        repo_root=Path(ns.repo_root),
        session_factory=factory,
        forge_api=ns.forge_api,
    )
    result = await svc.check_once()
    await engine.dispose()
    return {
        "current_pin": result.current_pin,
        "latest_pin": result.latest_pin,
        "mode": result.mode,
        "action_taken": result.action_taken,
        "detail": result.detail,
    }


if __name__ == "__main__":
    sys.exit(main())
