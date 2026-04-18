"""Brand configuration — makes the Scotty fleet brand-agnostic.

The framework, orchestrator, and infra-worker names are configurable so a
fork can deploy its own ecosystem (e.g. ``briancore`` + ``briandev`` +
``brianlab``) without touching source code. All hardcoded paths, env var
names, systemd unit names, and default URLs derive from this object.

Loaded at process startup from environment variables (prefixed ``BRAND_``)
with scotty defaults for backwards compatibility. A fork overrides the
env vars — nothing else needs to change.

Environment variables
---------------------
``BRAND_FRAMEWORK_NAME``
    The shared-framework pip package name. Drives ``/etc/<name>/``,
    ``/opt/<name>/<app>/``, ``<NAME>_DATA_DIR`` env var, and systemd
    unit names like ``<name>-update-check@<app>.timer``. Default:
    ``scottycore``.

``BRAND_FAMILY_NAME``
    The app-family slug prefix (e.g. ``scotty`` for ``scottybiz``,
    ``scottystrike``). Default: ``scotty``.

``BRAND_ORCHESTRATOR_NAME``
    The orchestrator app slug. Default: ``scottydev``.

``BRAND_INFRA_WORKER_NAME``
    The infrastructure worker app slug, or empty for solo mode.
    Default: ``scottylab``.

``BRAND_INFRA_WORKER_URL``
    Base URL for the infra worker API. Empty disables remote infra
    calls (solo mode — user runs ops manually). Default: empty.

``BRAND_DOMAIN_ROOT``
    Default FQDN root for apps. Used when apps don't specify their own
    fqdn. Default: ``scotty.consulting``.

``BRAND_FRAMEWORK_REPO_URL``
    Git URL for the shared framework. Used by auto-update pollers and
    repo watchers. Default: the upstream scottycore repo.

``BRAND_DISPLAY_NAME``
    Human-readable brand name for UI and logs. Default: ``Scotty``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


_DEFAULT_FRAMEWORK = "scottycore"
_DEFAULT_FAMILY = "scotty"
_DEFAULT_ORCHESTRATOR = "scottydev"
_DEFAULT_INFRA_WORKER = "scottylab"
_DEFAULT_INFRA_WORKER_URL = ""
_DEFAULT_DOMAIN_ROOT = "scotty.consulting"
_DEFAULT_FRAMEWORK_REPO_URL = (
    "https://github.com/scrampker/scottycore.git"
)
_DEFAULT_DISPLAY_NAME = "Scotty"


@dataclass(frozen=True)
class BrandConfig:
    """Runtime brand identity for a scottycore-based ecosystem.

    Immutable by design — set once at process startup, never mutated.
    Forks override defaults via ``BRAND_*`` env vars (see module docstring).
    """

    framework_name: str = _DEFAULT_FRAMEWORK
    family_name: str = _DEFAULT_FAMILY
    orchestrator_name: str = _DEFAULT_ORCHESTRATOR
    infra_worker_name: str = _DEFAULT_INFRA_WORKER
    infra_worker_url: str = _DEFAULT_INFRA_WORKER_URL
    domain_root: str = _DEFAULT_DOMAIN_ROOT
    framework_repo_url: str = _DEFAULT_FRAMEWORK_REPO_URL
    display_name: str = _DEFAULT_DISPLAY_NAME

    @classmethod
    def from_env(cls) -> "BrandConfig":
        """Load the brand from environment variables with scotty defaults."""
        return cls(
            framework_name=os.environ.get(
                "BRAND_FRAMEWORK_NAME", _DEFAULT_FRAMEWORK
            ).strip().lower() or _DEFAULT_FRAMEWORK,
            family_name=os.environ.get(
                "BRAND_FAMILY_NAME", _DEFAULT_FAMILY
            ).strip().lower() or _DEFAULT_FAMILY,
            orchestrator_name=os.environ.get(
                "BRAND_ORCHESTRATOR_NAME", _DEFAULT_ORCHESTRATOR
            ).strip().lower() or _DEFAULT_ORCHESTRATOR,
            infra_worker_name=os.environ.get(
                "BRAND_INFRA_WORKER_NAME", _DEFAULT_INFRA_WORKER
            ).strip().lower(),
            infra_worker_url=os.environ.get(
                "BRAND_INFRA_WORKER_URL", _DEFAULT_INFRA_WORKER_URL
            ).strip(),
            domain_root=os.environ.get(
                "BRAND_DOMAIN_ROOT", _DEFAULT_DOMAIN_ROOT
            ).strip().lower() or _DEFAULT_DOMAIN_ROOT,
            framework_repo_url=os.environ.get(
                "BRAND_FRAMEWORK_REPO_URL", _DEFAULT_FRAMEWORK_REPO_URL
            ).strip() or _DEFAULT_FRAMEWORK_REPO_URL,
            display_name=os.environ.get(
                "BRAND_DISPLAY_NAME", _DEFAULT_DISPLAY_NAME
            ).strip() or _DEFAULT_DISPLAY_NAME,
        )

    # ── Derived values (paths, env var names, systemd units) ────────────

    @property
    def config_dir(self) -> Path:
        """System config directory, e.g. ``/etc/scottycore``."""
        return Path("/etc") / self.framework_name

    @property
    def apps_root(self) -> Path:
        """Fleet-wide apps root on the host, e.g. ``/opt/scottycore``."""
        return Path("/opt") / self.framework_name

    @property
    def data_dir_env_var(self) -> str:
        """Env var that overrides the app's data directory, e.g.
        ``SCOTTYCORE_DATA_DIR``."""
        return f"{self.framework_name.upper()}_DATA_DIR"

    @property
    def systemd_unit_prefix(self) -> str:
        """Prefix for auto-update systemd units, e.g.
        ``scottycore-update-check``."""
        return f"{self.framework_name}-update-check"

    @property
    def systemd_app_unit_prefix(self) -> str:
        """Prefix for the generic git-pull update units, e.g.
        ``scottycore-app-update-check``."""
        return f"{self.framework_name}-app-update-check"

    @property
    def update_mode_path(self) -> Path:
        """Path to the update-mode file used by the generic git-pull
        updater, e.g. ``/etc/scottycore/update-mode``."""
        return self.config_dir / "update-mode"

    @property
    def update_setting_key_mode(self) -> str:
        """Settings key for the in-DB update mode used by consumer apps,
        e.g. ``scottycore.update.mode``."""
        return f"{self.framework_name}.update.mode"

    @property
    def update_setting_key_pending(self) -> str:
        """Settings key used by consumer apps to surface a pending-update
        banner in their UI, e.g. ``scottycore.update.pending``."""
        return f"{self.framework_name}.update.pending"

    @property
    def rebuild_flag_filename(self) -> str:
        """Filename touched in a consumer app's repo when auto-update
        bumps the pin, e.g. ``.scottycore-auto-update-requested``."""
        return f".{self.framework_name}-auto-update-requested"

    @property
    def pin_pattern(self) -> str:
        """Regex pattern (uncompiled) matching
        ``<framework_name> @ git+...@<ref>`` in pyproject.toml."""
        return (
            rf"{self.framework_name}\s*@\s*git\+[^@]+@([^\s\"';]+)"
        )

    @property
    def has_infra_worker(self) -> bool:
        """True if an infra worker URL is configured (non-solo mode)."""
        return bool(self.infra_worker_url)

    @property
    def infra_worker_fqdn_default(self) -> str:
        """Default FQDN for the infra worker, derived from its slug +
        domain root, e.g. ``scottylab.scotty.consulting``. Useful when
        someone wants to build the URL without hardcoding it."""
        if not self.infra_worker_name:
            return ""
        return f"{self.infra_worker_name}.{self.domain_root}"

    @property
    def orchestrator_fqdn_default(self) -> str:
        """Default FQDN for the orchestrator (scottydev). e.g.
        ``scottydev.scotty.consulting``."""
        return f"{self.orchestrator_name}.{self.domain_root}"


@lru_cache(maxsize=1)
def get_brand() -> BrandConfig:
    """Singleton accessor — loads from env on first call, caches thereafter.

    Tests that need to override should call ``reset_brand_cache()`` after
    mutating the environment.
    """
    return BrandConfig.from_env()


def reset_brand_cache() -> None:
    """Clear the cached brand. Use in tests after changing env vars."""
    get_brand.cache_clear()
