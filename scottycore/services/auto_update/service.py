"""AutoUpdateService — minute-tick update poller for consumer apps.

Design
------
Each app deploys a systemd timer that runs ``scottycore-update-check`` once
a minute. The CLI uses this service to:

1. Read the app's current ``scottycore`` pin from its ``pyproject.toml``.
2. Query the configured forge (``github`` or ``forgejo``) for the latest
   scottycore tag.
3. Compare. If a newer version exists:
   * Look up the app's ``scottycore.update.mode`` setting:
     - ``"off"``     — do nothing.
     - ``"notify"``  — insert a :class:`UpdateNotice` row and log.
     - ``"auto"``    — bump the pin, commit, push, and trigger a rebuild
                       (the rebuild lives outside this service; we fire an
                       exit code / side-file that the host's pull-updater
                       picks up on its next tick).

Source of truth
---------------
The ``mode`` setting lives in the scottycore ``settings`` table at the
``global`` scope so operators can toggle it either:
  * **Locally** — by editing settings through the app's UI
  * **Centrally** — by having ScottyDev push settings via its sync API

The setting key is ``scottycore.update.mode`` with values off/notify/auto.

Restart / rebuild signal
------------------------
When mode is ``auto``, after updating the pin we touch
``{repo_root}/.scottycore-auto-update-requested`` and exit with code ``75``.
A separate host-side runner (``scottycore-autopull.sh`` or an ansible task)
watches for this file / exit code and runs ``docker compose up -d --build``.
We intentionally keep the rebuild out of the poller so it can run with
minimal privileges and doesn't need docker-in-docker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.core.brand import BrandConfig, get_brand
from scottycore.services.settings.repository import SettingsRepository

_log = logging.getLogger(__name__)

#: Valid values for the ``<framework>.update.mode`` setting.
MODE_OFF = "off"
MODE_NOTIFY = "notify"
MODE_AUTO = "auto"
_MODES = {MODE_OFF, MODE_NOTIFY, MODE_AUTO}

_DEFAULT_MODE = MODE_NOTIFY


def _default_forge_api(brand: BrandConfig) -> str:
    """Derive the GitHub-style API base from the brand's framework repo URL.

    ``https://github.com/owner/repo.git`` → ``https://api.github.com/repos/owner/repo``
    Anything we can't parse falls back to the scotty default.
    """
    url = brand.framework_repo_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("https://github.com/"):
        owner_repo = url[len("https://github.com/") :]
        return f"https://api.github.com/repos/{owner_repo}"
    return "https://api.github.com/repos/scrampker/scottycore"


@dataclass(frozen=True)
class UpdateCheckResult:
    """Outcome of one poll cycle."""

    current_pin: str | None
    latest_pin: str | None
    mode: str
    action_taken: str  # "none" | "notified" | "auto-updated" | "error"
    detail: str = ""


class AutoUpdateError(Exception):
    """Raised for poll-cycle failures."""


class AutoUpdateService:
    """Read-only poller — rebuild is deliberately out of scope.

    Not registered in the ServiceRegistry automatically; the CLI constructs
    one per invocation. Consumer apps that want an in-process background
    poller can wire it into their lifespan manager.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        session_factory: async_sessionmaker[AsyncSession],
        forge_api: str | None = None,
        http_timeout: float = 10.0,
        brand: BrandConfig | None = None,
    ) -> None:
        self._brand = brand or get_brand()
        self._root = Path(repo_root).resolve()
        self._factory = session_factory
        self._forge_api = (
            forge_api or _default_forge_api(self._brand)
        ).rstrip("/")
        self._timeout = http_timeout
        self._pin_re = re.compile(self._brand.pin_pattern, re.IGNORECASE)

    # ── Public API ────────────────────────────────────────────────────────

    async def check_once(self) -> UpdateCheckResult:
        """Run one poll. Caller owns scheduling (systemd timer, cron, etc.)."""
        try:
            current = self._read_current_pin()
        except AutoUpdateError as exc:
            return UpdateCheckResult(
                current_pin=None,
                latest_pin=None,
                mode=MODE_OFF,
                action_taken="error",
                detail=str(exc),
            )

        try:
            latest = await self._fetch_latest_tag()
        except AutoUpdateError as exc:
            return UpdateCheckResult(
                current_pin=current,
                latest_pin=None,
                mode=await self._read_mode(),
                action_taken="error",
                detail=str(exc),
            )

        mode = await self._read_mode()

        if not _is_newer(current, latest):
            return UpdateCheckResult(
                current_pin=current,
                latest_pin=latest,
                mode=mode,
                action_taken="none",
                detail="up-to-date",
            )

        if mode == MODE_OFF:
            return UpdateCheckResult(
                current_pin=current,
                latest_pin=latest,
                mode=mode,
                action_taken="none",
                detail="update available but mode=off",
            )

        if mode == MODE_NOTIFY:
            await self._emit_notify(current, latest)
            return UpdateCheckResult(
                current_pin=current,
                latest_pin=latest,
                mode=mode,
                action_taken="notified",
                detail=f"{current} → {latest}",
            )

        if mode == MODE_AUTO:
            self._bump_pin(current, latest)
            self._touch_rebuild_flag()
            return UpdateCheckResult(
                current_pin=current,
                latest_pin=latest,
                mode=mode,
                action_taken="auto-updated",
                detail=f"bumped {current} → {latest}; rebuild flag dropped",
            )

        return UpdateCheckResult(
            current_pin=current,
            latest_pin=latest,
            mode=mode,
            action_taken="error",
            detail=f"unknown mode: {mode}",
        )

    # ── Pin handling ──────────────────────────────────────────────────────

    def _read_current_pin(self) -> str:
        pp = self._root / "pyproject.toml"
        if not pp.is_file():
            raise AutoUpdateError(f"no pyproject.toml at {pp}")
        text = pp.read_text(encoding="utf-8")
        m = self._pin_re.search(text)
        if not m:
            raise AutoUpdateError(
                f"{self._brand.framework_name} @ git+… dep not found in pyproject"
            )
        return m.group(1)

    def _bump_pin(self, current: str, latest: str) -> None:
        pp = self._root / "pyproject.toml"
        text = pp.read_text(encoding="utf-8")
        updated = text.replace(f"@{current}", f"@{latest}", 1)
        if updated == text:
            raise AutoUpdateError(
                f"failed to bump pin {current} → {latest} (pattern not found)"
            )
        pp.write_text(updated, encoding="utf-8")
        _log.info("auto-update: pyproject.toml bumped %s → %s", current, latest)

    def _touch_rebuild_flag(self) -> None:
        flag = self._root / self._brand.rebuild_flag_filename
        flag.write_text(
            json.dumps(
                {
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "reason": f"{self._brand.framework_name} auto-update bump",
                }
            ),
            encoding="utf-8",
        )

    # ── Settings + notify plumbing ────────────────────────────────────────

    async def _read_mode(self) -> str:
        try:
            async with self._factory() as s:
                repo = SettingsRepository(s)
                row = await repo.get(
                    "global", None, self._brand.update_setting_key_mode
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning("auto-update: settings lookup failed: %s", exc)
            return _DEFAULT_MODE
        if row is None:
            return _DEFAULT_MODE
        try:
            raw = json.loads(row.value_json)
        except (TypeError, ValueError):
            return _DEFAULT_MODE
        if isinstance(raw, str) and raw in _MODES:
            return raw
        return _DEFAULT_MODE

    async def _emit_notify(self, current: str, latest: str) -> None:
        """Drop a settings row so the UI can surface the update.

        We don't have a standalone notifications table; the settings key
        ``<framework>.update.pending`` is the rendezvous point. The UI
        reads this key to show the yellow "update available" banner.
        """
        try:
            async with self._factory() as s:
                repo = SettingsRepository(s)
                await repo.upsert(
                    scope="global",
                    scope_id=None,
                    key=self._brand.update_setting_key_pending,
                    value_json=json.dumps(
                        {
                            "current": current,
                            "latest": latest,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    updated_by=None,
                )
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            _log.warning("auto-update: notify write failed: %s", exc)

    # ── Forge API ─────────────────────────────────────────────────────────

    async def _fetch_latest_tag(self) -> str:
        url = f"{self._forge_api}/releases/latest"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                r = await client.get(url)
            except httpx.HTTPError as exc:
                raise AutoUpdateError(f"forge HTTP error: {exc}") from exc
        if r.status_code == 404:
            # Fallback: list all tags (when there are no github releases)
            return await self._fetch_latest_from_tags()
        if r.status_code >= 400:
            raise AutoUpdateError(
                f"forge release API {r.status_code}: {r.text[:200]}"
            )
        try:
            name = r.json().get("tag_name") or r.json().get("name")
        except ValueError as exc:
            raise AutoUpdateError(f"malformed release JSON: {exc}") from exc
        if not isinstance(name, str) or not name:
            raise AutoUpdateError("release payload missing tag name")
        return name

    async def _fetch_latest_from_tags(self) -> str:
        url = f"{self._forge_api}/tags"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(url)
        if r.status_code >= 400:
            raise AutoUpdateError(
                f"forge tags API {r.status_code}: {r.text[:200]}"
            )
        tags = [t.get("name") for t in r.json() if isinstance(t, dict)]
        tags = [t for t in tags if isinstance(t, str)]
        if not tags:
            raise AutoUpdateError("no tags on remote")
        # Version-sort by PEP440; fall back to lexicographic for odd tags.
        tags_sorted = sorted(tags, key=_version_key, reverse=True)
        return tags_sorted[0]


def _version_key(tag: str) -> tuple[int, Version | str]:
    try:
        return (1, Version(tag.lstrip("v")))
    except InvalidVersion:
        return (0, tag)


def _is_newer(current: str | None, latest: str | None) -> bool:
    if not current or not latest:
        return False
    try:
        c = Version(current.lstrip("v"))
        l = Version(latest.lstrip("v"))
    except InvalidVersion:
        return current != latest
    return l > c
