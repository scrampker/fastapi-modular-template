"""Shared exception types used across all services."""

from __future__ import annotations


class AppError(Exception):
    """Base application error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(AppError):
    """Resource not found."""

    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource} '{identifier}' not found", status_code=404)


class ForbiddenError(AppError):
    """User lacks permission."""

    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message, status_code=403)


class ConflictError(AppError):
    """Resource already exists or state conflict."""

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class AuthenticationError(AppError):
    """Invalid credentials or expired token."""

    def __init__(self, message: str = "Invalid credentials"):
        super().__init__(message, status_code=401)


class ValidationError(AppError):
    """Business logic validation failure (not schema validation)."""

    def __init__(self, message: str):
        super().__init__(message, status_code=422)
