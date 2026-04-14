"""Application configuration via environment variables."""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings

_DEFAULT_JWT_SECRET = "CHANGE-ME-IN-PRODUCTION"
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


def _persist_jwt_secret_to_env(env_path: Path, key: str) -> None:
    """Write or update ``JWT_SECRET_KEY`` in *env_path*.

    - If the file does not exist, it is created with just the key line.
    - If ``JWT_SECRET_KEY=`` is already present (any value), it is replaced.
    - Otherwise the new line is appended.
    """
    line = f"JWT_SECRET_KEY={key}\n"
    try:
        if not env_path.exists():
            env_path.write_text(line, encoding="utf-8")
            return

        content = env_path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        updated = False
        new_lines: list[str] = []
        for existing in lines:
            if existing.lstrip().upper().startswith("JWT_SECRET_KEY="):
                new_lines.append(line)
                updated = True
            else:
                new_lines.append(existing)

        if not updated:
            # Ensure a trailing newline before appending
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(line)

        env_path.write_text("".join(new_lines), encoding="utf-8")
    except OSError:
        # Non-fatal: if .env is read-only or missing, the in-memory key still works
        pass


class Settings(BaseSettings):
    """All configuration loaded from environment / .env file."""

    # Database
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # JWT
    jwt_secret_key: str = _DEFAULT_JWT_SECRET
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_algorithm: str = "HS256"

    # Application
    app_name: str = "MyApp"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # CORS
    cors_origins: str = "http://localhost:8000"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Rate Limiting
    rate_limit_auth: str = "5/minute"
    rate_limit_api: str = "60/minute"

    # File manager
    uploads_base_dir: str = "uploads"

    # Initial admin (first-run setup only)
    init_admin_email: str = "admin@example.com"
    init_admin_password: str = "changeme"

    # External identity providers (header-based trust)
    # Comma-separated list of trusted providers: "cloudflare", "azure", "none"
    trusted_identity_providers: str = "cloudflare,azure"
    # Email that gets auto-promoted to superadmin on first sight via external auth
    admin_email: str = ""
    # Cloudflare Access tunnel URL (shown on login page as SSO link)
    cf_tunnel_url: str = ""

    # Dev bypass token — allows headless Chrome / Claude Code to access the app
    # without authenticating. Set to a random string in dev, leave empty to disable.
    # NEVER set this in production.
    dev_bypass_token: str = ""

    @model_validator(mode="after")
    def _auto_generate_jwt_secret(self) -> "Settings":
        """Generate and persist a JWT secret key when the placeholder is detected.

        On first run (or when .env still has the default), a cryptographically
        secure key is generated, written to .env, and used for this session.
        The ``main.py`` RuntimeError guard remains as a production safety net.
        """
        if self.jwt_secret_key not in (_DEFAULT_JWT_SECRET, ""):
            return self

        new_key = secrets.token_hex(32)
        self.jwt_secret_key = new_key
        _persist_jwt_secret_to_env(_ENV_FILE, new_key)
        print("[CONFIG] Generated new JWT_SECRET_KEY and saved to .env")  # noqa: T201
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_providers(self) -> set[str]:
        return {p.strip().lower() for p in self.trusted_identity_providers.split(",") if p.strip()}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": False}


def get_settings() -> Settings:
    """Singleton-ish settings loader. FastAPI caches via Depends."""
    return Settings()
