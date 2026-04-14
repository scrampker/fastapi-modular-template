"""Web page routes — Jinja2 templates calling the same services as the API."""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

web_router = APIRouter()

# Resolve templates directory relative to this file so the router works
# regardless of the working directory the server is started from.
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


# ── Service Worker (must be served from root scope, not /static/) ─────────────

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@web_router.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    """Serve the service worker from root scope so it can control all pages."""
    return FileResponse(
        path=os.path.join(_STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# ── Root ──────────────────────────────────────────────────────────────────


@web_router.get("/", response_class=RedirectResponse, include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect bare root to login page."""
    return RedirectResponse(url="/login", status_code=302)


# ── Auth ──────────────────────────────────────────────────────────────────


@web_router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html")


# ── Tenant-scoped pages ───────────────────────────────────────────────────


@web_router.get("/c/{slug}/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {"slug": slug})


@web_router.get("/c/{slug}/items", response_class=HTMLResponse, include_in_schema=False)
async def items_page(request: Request, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "items.html", {"slug": slug})


@web_router.get("/c/{slug}/users", response_class=HTMLResponse, include_in_schema=False)
async def users_page(request: Request, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "users.html", {"slug": slug})


@web_router.get("/c/{slug}/audit-log", response_class=HTMLResponse, include_in_schema=False)
async def audit_log_page(request: Request, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "audit_log.html", {"slug": slug})


# ── Settings pages ────────────────────────────────────────────────────────


@web_router.get("/me", response_class=HTMLResponse, include_in_schema=False)
async def profile_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "profile.html")


@web_router.get("/me/settings", response_class=HTMLResponse, include_in_schema=False)
async def user_settings_page(request: Request) -> HTMLResponse:
    """User personal settings — any authenticated user."""
    return templates.TemplateResponse(request, "user_settings.html")


@web_router.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
async def admin_settings_page(request: Request) -> HTMLResponse:
    """Platform settings — superadmin only."""
    return templates.TemplateResponse(request, "admin_settings.html")


@web_router.get("/c/{slug}/settings", response_class=HTMLResponse, include_in_schema=False)
async def tenant_settings_page(request: Request, slug: str) -> HTMLResponse:
    """Tenant settings — tenant admin only."""
    return templates.TemplateResponse(request, "tenant_settings.html", {"slug": slug})
