"""Shared config loader for ScottyCore scripts.

Reads config/apps.yaml and provides a consistent API for all scripts
(sync-watcher.py, pattern_tracker.py, scottycore-validate.py, scottycore-init.py).

Uses a hand-rolled YAML parser to avoid requiring PyYAML as a dependency.
Only supports the flat key-value-per-app structure used by apps.yaml.
"""

from __future__ import annotations

from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = CORE_DIR / "config" / "apps.yaml"


def load_apps_config(config_path: Path | None = None) -> dict[str, dict]:
    """Load config/apps.yaml and return {app_name: {path, stack, branch, ...}}.

    Each app entry is a dict with at least: path (str), stack (str), branch (str).
    Optional: is_core (bool).
    """
    path = config_path or CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(f"App registry not found: {path}")

    apps: dict[str, dict] = {}
    current_app: str | None = None

    for line in path.read_text().splitlines():
        stripped = line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        # Top-level key (app name) — no leading whitespace, ends with ':'
        if not line[0].isspace() and stripped.endswith(":"):
            current_app = stripped[:-1]
            apps[current_app] = {}
            continue

        # Nested key-value under an app
        if current_app and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.split("#", 1)[0].strip()  # strip inline comments

            # Type coercion
            if value.lower() == "true":
                apps[current_app][key] = True
            elif value.lower() == "false":
                apps[current_app][key] = False
            else:
                apps[current_app][key] = value

    return apps


def get_consumer_apps(config_path: Path | None = None) -> dict[str, dict]:
    """Return only consumer apps (excluding scottycore itself)."""
    all_apps = load_apps_config(config_path)
    return {
        name: cfg for name, cfg in all_apps.items()
        if not cfg.get("is_core", False)
    }


def get_repos_dict(config_path: Path | None = None) -> dict[str, dict]:
    """Return all apps in the format expected by sync-watcher.py's REPOS dict.

    Returns: {name: {"path": str, "stack": str, "branch": str}}
    """
    all_apps = load_apps_config(config_path)
    repos = {}
    for name, cfg in all_apps.items():
        repos[name] = {
            "path": cfg.get("path", ""),
            "stack": cfg.get("stack", "unknown"),
            "branch": cfg.get("branch", "master"),
        }
    return repos
