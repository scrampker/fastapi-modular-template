"""Admin endpoints — update check, restart, version info.

Detects two kinds of updates:
  1. Local code changes — source files modified on disk since server started
     (e.g. an editor edited files while the app is running)
  2. Remote git updates — new commits on the remote branch
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scottycore.core.config import get_settings

# Fallback version — replace with a proper __version__ in app/__init__.py
# or read from importlib.metadata once the package is installed.
try:
    from scottycore import __version__  # type: ignore[attr-defined]
except ImportError:
    __version__ = "0.1.0"

router = APIRouter(tags=["admin"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# PROJECT_ROOT: repo root, 3 levels above app/api/v1/admin.py
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]

# Exit code convention: tell an outer launcher to pull updates and restart.
RESTART_EXIT_CODE = 75

# ---------------------------------------------------------------------------
# Local change detection — snapshot source-file mtimes at import time
# (i.e. at server startup) so we can detect edits made while running.
# ---------------------------------------------------------------------------

SRC_DIR: Path = PROJECT_ROOT / "app"
_WATCH_GLOBS: list[str] = ["**/*.py", "**/*.html"]
_WATCH_EXTRA: list[Path] = [
    PROJECT_ROOT / "launch.py",
    PROJECT_ROOT / "pyproject.toml",
]


def _snapshot_mtimes() -> dict[str, float]:
    """Record the mtime of every watched source file right now."""
    mtimes: dict[str, float] = {}
    for glob in _WATCH_GLOBS:
        # rglob expects the pattern without the leading "**/"
        for f in SRC_DIR.rglob(glob.replace("**/", "")):
            mtimes[str(f)] = f.stat().st_mtime
    for f in _WATCH_EXTRA:
        if f.exists():
            mtimes[str(f)] = f.stat().st_mtime
    return mtimes


# Captured once at import time (= server start).
_startup_mtimes: dict[str, float] = _snapshot_mtimes()
_startup_time: float = time.time()
_build_timestamp: str = (
    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
)


def _detect_local_changes() -> list[str]:
    """Return paths of files modified on disk since the server started.

    This catches edits made while the server is running — e.g. a developer
    editing files, a git pull run in another terminal, etc.  The snapshot
    resets on every restart, so after a restart this correctly returns [].
    """
    changed: list[str] = []
    current = _snapshot_mtimes()
    for path, mtime in current.items():
        startup_mtime = _startup_mtimes.get(path)
        if startup_mtime is None or mtime > startup_mtime:
            changed.append(path)
    return changed


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class VersionInfo(BaseModel):
    version: str
    git_branch: str | None = None
    git_commit: str | None = None
    git_dirty: bool = False
    build_ts: str | None = None


class UpdateStatus(BaseModel):
    update_available: bool
    local_changes: bool = False
    local_changed_files: list[str] = []
    remote_update: bool = False
    current_commit: str | None = None
    remote_commit: str | None = None
    commits_behind: int = 0
    summary: str = ""


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------


def _run_git(*args: str, timeout: int = 15) -> str | None:
    """Run a git command rooted at PROJECT_ROOT; return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/version", response_model=VersionInfo)
async def get_version() -> VersionInfo:
    """Return the running version and current git state."""
    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    commit = _run_git("rev-parse", "--short", "HEAD")
    status = _run_git("status", "--porcelain")
    return VersionInfo(
        version=__version__,
        git_branch=branch,
        git_commit=commit,
        git_dirty=bool(status),
        build_ts=_build_timestamp,
    )


@router.get("/update-check", response_model=UpdateStatus)
async def check_for_updates() -> UpdateStatus:
    """Check for local file changes and remote git commits.

    Two detection strategies run on every call:

    (a) **Local mtime scan** — compares current file mtimes against the
        snapshot taken at startup.  Fast: only stat(2) syscalls.

    (b) **Remote git check** — runs ``git fetch`` then counts commits on
        ``origin/<branch>`` that are not in HEAD.  Requires network access.
    """
    # --- (a) Local change detection ---
    changed_files = _detect_local_changes()
    local_changes = len(changed_files) > 0

    # Cap display list and make paths relative for readability
    rel_changed: list[str] = []
    for f in changed_files[:10]:
        try:
            rel_changed.append(str(Path(f).relative_to(PROJECT_ROOT)))
        except ValueError:
            rel_changed.append(f)

    # --- (b) Remote git check ---
    _run_git("fetch", "--quiet", timeout=10)

    current = _run_git("rev-parse", "HEAD")
    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")

    remote_update = False
    behind = 0
    remote: str | None = None

    if current and branch:
        behind_str = _run_git("rev-list", "--count", f"HEAD..origin/{branch}")
        behind = int(behind_str) if behind_str and behind_str.isdigit() else 0
        remote = _run_git("rev-parse", "--short", f"origin/{branch}")
        remote_update = behind > 0

    # Build human-readable summary
    summary_parts: list[str] = []
    if local_changes:
        summary_parts.append(f"{len(changed_files)} file(s) changed since server started")
    if remote_update:
        summary_parts.append(f"{behind} remote commit(s) available")
    if not summary_parts:
        summary_parts.append("Up to date")

    return UpdateStatus(
        update_available=local_changes or remote_update,
        local_changes=local_changes,
        local_changed_files=rel_changed,
        remote_update=remote_update,
        current_commit=current[:8] if current else None,
        remote_commit=remote,
        commits_behind=behind,
        summary="; ".join(summary_parts),
    )


@router.post("/restart")
async def restart_server() -> dict:
    """Pull the latest commits then restart the server.

    Performs ``git pull --ff-only`` before spawning the new process so any
    pull errors are surfaced in the HTTP response rather than silently during
    relaunch.
    """
    settings = get_settings()
    app_name = settings.app_name

    # Attempt a fast-forward-only pull first; fall back to a plain pull.
    pull_result = _run_git("pull", "--ff-only", timeout=30)
    if pull_result is None:
        pull_result = _run_git("pull", timeout=30)
        if pull_result is None:
            raise HTTPException(
                status_code=500,
                detail="git pull failed — check for merge conflicts or network issues",
            )

    _schedule_self_restart()

    return {
        "ok": True,
        "message": f"Pulling updates and restarting {app_name}...",
        "pull_result": pull_result,
    }


@router.post("/restart-only")
async def restart_only() -> dict:
    """Restart the server without pulling updates.

    Useful after a configuration change or to pick up files that were already
    deployed to disk by an external mechanism.
    """
    settings = get_settings()
    app_name = settings.app_name

    _schedule_self_restart()

    return {"ok": True, "message": f"Restarting {app_name}..."}


# ---------------------------------------------------------------------------
# Self-restart helper
# ---------------------------------------------------------------------------


def _schedule_self_restart() -> None:
    """Spawn a detached launch.py and exit the current process.

    Works whether or not an outer crash-detection wrapper is running as the
    parent process.  The new launcher is responsible for freeing the port and
    re-binding.  A short asyncio delay lets FastAPI flush the HTTP response
    before os._exit() is called.
    """

    async def _do_restart() -> None:
        await asyncio.sleep(0.5)

        launch_script = PROJECT_ROOT / "launch.py"
        python = sys.executable

        # Platform-specific flags for a fully detached child process
        if sys.platform == "win32":
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            DETACHED_PROCESS = 0x00000008
            popen_kwargs: dict = {
                "creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
                "close_fds": True,
            }
        else:
            popen_kwargs = {"start_new_session": True}

        subprocess.Popen(
            [python, str(launch_script)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )

        # Hard exit — bypasses atexit handlers so uvicorn doesn't try to
        # restart itself before the new launcher is ready.
        os._exit(0)

    asyncio.create_task(_do_restart())
