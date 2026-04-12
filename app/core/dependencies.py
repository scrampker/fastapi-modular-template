"""FastAPI dependency injection helpers.

API routes use these to get service instances and auth context.
Each dependency is a thin wrapper around the ServiceRegistry.
"""

from __future__ import annotations

from fastapi import Depends, Request

from app.core.service_registry import ServiceRegistry
from app.services.audit.service import AuditService
from app.services.auth.service import AuthService
from app.services.tenants.service import TenantsService
from app.services.users.service import UsersService
from app.services.search.service import SearchService
from app.services.settings.service import SettingsService
from app.services.items.service import ItemsService
from app.services.files.service import FilesService
from app.services.ai_backends.service import AIBackendsService


def _registry(request: Request) -> ServiceRegistry:
    return request.app.state.registry


# ── Service dependencies ──────────────────────────────────────────────
# Each returns a single service from the registry.
# API routes declare these as Depends() parameters.

def get_audit_service(reg: ServiceRegistry = Depends(_registry)) -> AuditService:
    return reg.audit

def get_auth_service(reg: ServiceRegistry = Depends(_registry)) -> AuthService:
    return reg.auth

def get_tenants_service(reg: ServiceRegistry = Depends(_registry)) -> TenantsService:
    return reg.tenants

def get_users_service(reg: ServiceRegistry = Depends(_registry)) -> UsersService:
    return reg.users

def get_settings_service(reg: ServiceRegistry = Depends(_registry)) -> SettingsService:
    return reg.settings

def get_search_service(reg: ServiceRegistry = Depends(_registry)) -> SearchService:
    return reg.search

def get_items_service(reg: ServiceRegistry = Depends(_registry)) -> ItemsService:
    return reg.items

def get_files_service(reg: ServiceRegistry = Depends(_registry)) -> FilesService:
    return reg.files

def get_ai_backends_service(reg: ServiceRegistry = Depends(_registry)) -> AIBackendsService:
    return reg.ai_backends
