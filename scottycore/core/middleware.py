"""Security middleware and exception handlers."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from scottycore.core.exceptions import AppError


def register_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "status_code": exc.status_code},
        )


_TOTP_EXEMPT_PREFIXES = (
    "/api/v1/auth/",
    "/health",
    "/login",
    "/setup-2fa",
    "/change-password",
    "/static/",
    "/sw.js",
    "/api/v1/admin/version",
    "/api/v1/admin/update-check",
)


def register_totp_enforcement(app: FastAPI) -> None:
    """Block API access for users who haven't enrolled TOTP when required.

    Only active when the ``require_totp`` global setting is ``True``.
    Returns 403 with ``totp_setup_required`` detail on protected endpoints.
    Web pages are handled client-side by the auth gate in base.html / main.js.
    """

    @app.middleware("http")
    async def totp_enforcement(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path

        if any(path.startswith(p) for p in _TOTP_EXEMPT_PREFIXES):
            return await call_next(request)

        # Only enforce on API requests that carry a Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return await call_next(request)

        registry = getattr(request.app.state, "registry", None)
        if registry is None:
            return await call_next(request)

        try:
            require_totp = await registry.settings.resolve("require_totp")
        except Exception:
            require_totp = None

        if require_totp is not True:
            return await call_next(request)

        # Decode token to check totp_enabled without duplicating full auth
        try:
            payload = registry.auth.decode_token(auth_header[7:])
            if payload.get("totp_pending"):
                return await call_next(request)
            user_id = payload.get("sub")
            if not user_id:
                return await call_next(request)

            from uuid import UUID
            user = await registry.users.get_by_id(UUID(user_id))
            if user and not user.totp_enabled:
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "TOTP setup required",
                        "totp_setup_required": True,
                    },
                )
        except Exception:
            pass

        return await call_next(request)


def register_security_headers(app: FastAPI) -> None:
    """Add security headers to every response."""

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Guard for cases where lifespan hasn't populated state yet (e.g. tests, startup errors)
        settings = getattr(request.app.state, "settings", None)
        if settings is not None and not settings.app_debug:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net https://unpkg.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
                "img-src 'self' data:; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "connect-src 'self'"
            )
        return response
