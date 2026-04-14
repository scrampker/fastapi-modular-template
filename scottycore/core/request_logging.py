"""Request/response logging middleware with dual-mode output.

Log levels:
  Normal mode : method, path, status code, elapsed time, error tracebacks.
  Debug mode  : also captures request bodies (with sensitive-path redaction)
                and response bodies on error responses.

Usage
-----
In your application factory (e.g. app/main.py)::

    from scottycore.core.request_logging import RequestLoggingMiddleware, setup_logging

    setup_logging(app_name="myapp", data_dir="data", log_stdout=True)
    app.add_middleware(RequestLoggingMiddleware)

The module-level ``logger`` instance can be imported anywhere::

    from scottycore.core.request_logging import logger
"""

from __future__ import annotations

import logging
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Sequence

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max log file size before rotation (5 MB) and number of backup files kept.
_MAX_LOG_BYTES: int = 5 * 1024 * 1024
_BACKUP_COUNT: int = 3

# Paths whose request bodies are never captured regardless of debug mode.
# Extend this list (or replace it at runtime before calling setup_logging)
# to cover your application's sensitive endpoints.
DEFAULT_REDACT_PATHS: tuple[str, ...] = (
    "/auth/",
    "/vault/",
    "/password",
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

#: Module-level logger.  Call :func:`setup_logging` once at startup to
#: attach the appropriate handler(s).  This default instance is ready to
#: use even before ``setup_logging`` is called (it will inherit root-logger
#: configuration).
logger: logging.Logger = logging.getLogger("app")


def setup_logging(
    app_name: str,
    data_dir: str | Path,
    *,
    log_stdout: bool = False,
) -> logging.Logger:
    """Configure the application logger.

    Parameters
    ----------
    app_name:
        Logger name and the stem of the log file (``<data_dir>/logs/<app_name>.log``).
    data_dir:
        Root data directory.  The ``logs/`` subdirectory is created if needed.
    log_stdout:
        When *True* (container/cloud mode) emit all records to *stdout* only.
        When *False* (local/dev mode) write to a rotating file and emit
        WARNING+ records to the console as well.

    Returns
    -------
    logging.Logger
        The configured logger (same object as the module-level ``logger``).

    Notes
    -----
    Guards against duplicate handler registration so this function is safe
    to call multiple times (e.g. during hot-reload in development).
    """
    global logger  # noqa: PLW0603

    app_logger = logging.getLogger(app_name)
    app_logger.setLevel(logging.DEBUG)

    # Guard: don't register handlers twice (hot-reload / multiple calls).
    if app_logger.handlers:
        logger = app_logger
        return app_logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_stdout:
        # Container mode — stdout only so the platform log aggregator can
        # collect structured lines without fighting file-rotation.
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(fmt)
        app_logger.addHandler(stream_handler)
    else:
        # Local / dev mode — rotating file (all levels) + console (WARNING+).
        log_dir = Path(data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{app_name}.log"

        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        app_logger.addHandler(file_handler)

        # Console: WARNING+ only to keep the terminal readable during development.
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(fmt)
        app_logger.addHandler(console_handler)

    logger = app_logger
    return app_logger


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that logs every HTTP request with timing.

    Parameters
    ----------
    app:
        The ASGI application (passed automatically by Starlette/FastAPI).
    app_debug:
        When *True*, request bodies (with redaction) and error response
        bodies are also captured.
    redact_paths:
        Path substrings that trigger body redaction.  Any request whose
        URL path contains one of these strings will have its body replaced
        with ``[REDACTED]`` in the log output.  Defaults to
        :data:`DEFAULT_REDACT_PATHS`.
    skip_prefixes:
        URL path prefixes that are skipped entirely (no log entry).
        Defaults to ``["/static/", "/ws/"]``.
    """

    def __init__(
        self,
        app,
        *,
        app_debug: bool = False,
        redact_paths: Sequence[str] = DEFAULT_REDACT_PATHS,
        skip_prefixes: Sequence[str] = ("/static/", "/ws/"),
    ) -> None:
        super().__init__(app)
        self._app_debug = app_debug
        self._redact_paths = tuple(redact_paths)
        self._skip_prefixes = tuple(skip_prefixes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_skip(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self._skip_prefixes)

    def _is_sensitive(self, path: str) -> bool:
        return any(fragment in path for fragment in self._redact_paths)

    # ------------------------------------------------------------------
    # Middleware dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if self._should_skip(path):
            return await call_next(request)

        method = request.method
        start = time.perf_counter()

        # Optionally capture and log the request body before forwarding.
        if self._app_debug and method in ("POST", "PUT", "PATCH"):
            await self._log_request_body(request, path, method)

        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - start) * 1000

            status = response.status_code
            level = logging.WARNING if status >= 400 else logging.INFO
            logger.log(level, "%s %s | %d | %.0fms", method, path, status, elapsed_ms)

            # Optionally capture and log the response body on error responses.
            if self._app_debug and status >= 400:
                response = await self._log_response_body(response, path, status)

            return response

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            tb = traceback.format_exc()
            logger.error(
                "%s %s | CRASH | %.0fms | %s: %s",
                method,
                path,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            logger.error("TRACEBACK | %s\n%s", path, tb)
            raise

    async def _log_request_body(
        self,
        request: Request,
        path: str,
        method: str,
    ) -> None:
        """Read, log, and re-inject the request body so downstream handlers still see it."""
        try:
            body = await request.body()

            # Re-inject the body so downstream handlers can still read it.
            async def _receive():
                return {"type": "http.request", "body": body}

            request._receive = _receive  # type: ignore[assignment]

            if self._is_sensitive(path):
                logger.debug("REQ BODY | %s %s | [REDACTED]", method, path)
            else:
                preview = body[:2000].decode("utf-8", errors="replace")
                logger.debug("REQ BODY | %s %s | %s", method, path, preview)
        except Exception:
            # Never let logging failures affect request processing.
            pass

    async def _log_response_body(
        self,
        response: Response,
        path: str,
        status: int,
    ) -> Response:
        """Consume, log, and reconstruct the response so the client still receives it."""
        try:
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
            body_bytes = b"".join(chunks)

            preview = body_bytes[:2000].decode("utf-8", errors="replace")
            logger.debug("RSP BODY | %d %s | %s", status, path, preview)

            # Reconstruct the response with the already-consumed body.
            from starlette.responses import Response as RawResponse

            return RawResponse(
                content=body_bytes,
                status_code=status,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        except Exception:
            return response
