"""Top-level v1 API router — mounts all sub-routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, audit, auth, items, search, settings, tasks, tenants, users, ws

api_v1_router = APIRouter()

# Auth
api_v1_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Tenants
api_v1_router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])

# Tenant-scoped resources
api_v1_router.include_router(
    items.router,
    prefix="/tenants/{slug}/items",
    tags=["items"],
)
api_v1_router.include_router(
    users.router,
    prefix="/tenants/{slug}/users",
    tags=["users"],
)
api_v1_router.include_router(
    audit.router,
    prefix="/tenants/{slug}/audit-log",
    tags=["audit"],
)

# Current user endpoints (no tenant prefix)
api_v1_router.include_router(users.me_router, tags=["users"])

# Settings and search
api_v1_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_v1_router.include_router(search.router, prefix="/search", tags=["search"])

# Admin (version, update-check, restart)
api_v1_router.include_router(admin.router, prefix="/admin", tags=["admin"])

# Tasks (background task lifecycle)
api_v1_router.include_router(tasks.router)

# WebSocket (task streaming)
api_v1_router.include_router(ws.router)
