"""FastAPI application factory."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import Settings, get_settings
from app.core.database import Base, get_engine, get_session_factory
from app.core.middleware import register_exception_handlers, register_security_headers
from app.core.schemas import HealthResponse
from app.core.service_registry import ServiceRegistry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown logic."""
    settings = get_settings()
    app.state.settings = settings

    # Security startup checks (non-debug only)
    if not settings.app_debug:
        if settings.jwt_secret_key == "CHANGE-ME-IN-PRODUCTION":
            raise RuntimeError(
                "CRITICAL: JWT_SECRET_KEY is set to the default value. "
                "Set a secure random value (64+ chars) in .env before running in production."
            )
        if settings.init_admin_password == "changeme":
            raise RuntimeError(
                "CRITICAL: INIT_ADMIN_PASSWORD is set to the default 'changeme'. "
                "Set a strong password in .env before running in production."
            )

    # Create tables (dev only — production uses Alembic)
    if settings.app_debug and settings.is_sqlite:
        engine = get_engine()
        # Import all models so Base.metadata knows about them
        import app.services.audit.models  # noqa: F401
        import app.services.auth.models  # noqa: F401
        import app.services.tenants.models  # noqa: F401
        import app.services.users.models  # noqa: F401
        import app.services.settings.models  # noqa: F401
        import app.services.items.models  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # Wire up service registry
    session_factory = get_session_factory()
    app.state.registry = ServiceRegistry(session_factory)

    # Seed roles if they don't exist
    await _seed_roles(session_factory)

    # Create initial superadmin if no users exist
    user_count = await app.state.registry.users.user_count()
    if user_count == 0:
        try:
            await app.state.registry.users.create_superadmin(
                email=settings.init_admin_email,
                password=settings.init_admin_password,
                display_name="Admin",
            )
        except Exception:
            pass  # Already exists or other issue — non-fatal

    yield

    # Shutdown
    engine = get_engine()
    await engine.dispose()


async def _seed_roles(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Insert predefined roles if they don't exist."""
    from app.services.users.models import Role

    roles = [
        (1, "viewer", "Read-only access within assigned tenants"),
        (2, "analyst", "Read + annotate + export within assigned tenants"),
        (3, "admin", "Full access within assigned tenants"),
        (4, "superadmin", "Platform-wide access"),
    ]
    async with session_factory() as session:
        from sqlalchemy import select
        existing = await session.scalars(select(Role))
        existing_ids = {r.id for r in existing.all()}
        for role_id, name, description in roles:
            if role_id not in existing_ids:
                session.add(Role(id=role_id, name=name, description=description))
        await session.commit()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="MyApp",
        description="Multi-tenant FastAPI platform",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Security headers + exception handlers
    register_security_headers(app)
    register_exception_handlers(app)

    # API routes
    from app.api.v1.router import api_v1_router
    app.include_router(api_v1_router, prefix="/api/v1")

    # Web UI routes (Jinja2 pages)
    from app.web.router import web_router
    app.include_router(web_router)

    # Static files (CSS / JS)
    _static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
    if os.path.isdir(_static_dir):
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    # Health check (no auth required)
    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(
            version="0.1.0",
            environment=settings.app_env,
        )

    return app


# Uvicorn entrypoint
app = create_app()
