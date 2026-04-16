"""Backup / restore service for scottycore.

Two scopes are supported:
  - PLATFORM: all scottycore-owned data (users, tenants, roles, settings,
    items, audit log).  Superadmin-only.
  - TENANT: everything scoped to one tenant (tenant settings, items, audit
    subset, plus any domain resources registered by the app).  Superadmin
    OR tenant-admin.

Domain services in apps can extend coverage by registering a
``BackupContributor`` implementation with ``BackupService.register()``.
No scottycore changes are needed when apps add new contributors.
"""
from __future__ import annotations
