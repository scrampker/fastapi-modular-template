"""Application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration loaded from environment / .env file."""

    # Database
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # JWT
    jwt_secret_key: str = "CHANGE-ME-IN-PRODUCTION"
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
