"""Service registry: wires all services together via dependency injection.

This is the ONLY place where services know about each other's existence.
Services receive references to other services they need via constructor args.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from scottycore.services.audit.service import AuditService
from scottycore.services.auth.service import AuthService
from scottycore.services.backup.service import BackupService
from scottycore.services.backup.wiring import build_backup_service
from scottycore.services.tenants.service import TenantsService
from scottycore.services.users.service import UsersService
from scottycore.services.search.service import SearchService
from scottycore.services.settings.service import SettingsService
from scottycore.services.items.service import ItemsService
from scottycore.services.files.service import FilesService
from scottycore.services.ai_backends.service import AIBackendsService


class ServiceRegistry:
    """Central registry that owns all service instances.

    Construction order matters: services with no cross-deps first,
    then composite services that depend on others.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        uploads_base_dir: str = "uploads",
    ) -> None:
        # Layer 1: Infrastructure services (no cross-service deps)
        self.audit = AuditService(session_factory)
        self.auth = AuthService(session_factory, self.audit)
        self.tenants = TenantsService(session_factory, self.audit)
        self.users = UsersService(session_factory, self.audit)
        self.settings = SettingsService(session_factory)

        # Layer 1b: Stateless infrastructure (reads config from settings store)
        self.ai_backends = AIBackendsService(settings_service=self.settings)

        # Layer 2: Domain services
        self.items = ItemsService(session_factory, self.audit)
        self.files = FilesService(uploads_base_dir, self.audit)

        # Layer 3: Composite / cross-domain services
        self.search = SearchService(
            self.items,
            self.tenants,
        )

        # Layer 3: Backup — consumer apps can register additional contributors
        # on this instance after ServiceRegistry construction.
        self.backup: BackupService = build_backup_service(
            session_factory,
            self.audit,
            uploads_base_dir=uploads_base_dir,
        )
