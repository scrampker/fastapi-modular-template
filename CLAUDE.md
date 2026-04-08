# FastAPI Modular Template

## What This Is

A battle-tested reusable project template for building multi-tenant web applications with FastAPI. Extracted from ScottyScan (50 API endpoints, 14 service modules, 15 GUI pages, full security review). Designed so multiple AI agents can build different modules simultaneously with zero merge conflicts.

This is a **living shared template**. Multiple Claude Code environments pull from it, use it as a starting point for new apps, and push generic improvements back. Domain-specific code stays in each app's own repo; universal infrastructure patterns go here.

## Architecture: Contract-First Modular Service Layer

Every module communicates through typed service interfaces (Pydantic schemas in, Pydantic schemas out). No module imports another module's ORM models or writes queries against another module's tables.

```
FastAPI Routes (thin wrappers: auth + service call + response)
       │
       ▼
┌────────┐┌──────┐┌──────┐┌──────┐┌──────┐
│  Auth  ││Tenant││ Users││ Audit ││ Items│  ← each developer/agent owns one
│Service ││ Svc  ││ Svc  ││ Svc  ││ Svc  │
└────┬───┘└──┬───┘└──┬───┘└──┬───┘└──┬───┘
     └───────┴───┬───┴───────┴───────┘
                 │
        Service Registry (DI)
                 │
           PostgreSQL / SQLite
```

## Module Structure

Each service module lives in `app/services/<name>/` with exactly these files:

| File | Visibility | Purpose |
|------|-----------|---------|
| `schemas.py` | **PUBLIC** | The contract. Pydantic models other modules depend on. Design first, change rarely. |
| `service.py` | **PUBLIC** | The interface. Async methods other modules call. Takes session_factory + other services via DI. |
| `models.py` | **PRIVATE** | SQLAlchemy ORM models. Only this module's code touches these. |
| `repository.py` | **PRIVATE** | Database queries. Separated from service for testability. |
| `__init__.py` | **PUBLIC** | Exports public symbols only. |

## Directory Layout

```
app/
  core/
    config.py              # Pydantic Settings (env-based)
    database.py            # SQLAlchemy async engine + session factory
    auth.py                # Unified auth: Cloudflare → Azure → JWT → API key
    schemas.py             # PaginatedResponse[T], RoleName, AuditContext
    exceptions.py          # NotFoundError, ForbiddenError, etc.
    dependencies.py        # FastAPI Depends() helpers
    service_registry.py    # Wires all services together at startup
    middleware.py          # Security headers, error handlers
  services/
    auth/                  # JWT + bcrypt + token prefix O(1) lookup
    tenants/               # Multi-tenant management (rename to your domain term)
    users/                 # CRUD + role assignment per tenant
    audit/                 # Immutable action log (SOC2 compliance)
    settings/              # 4-tier KV store: global → tenant → user with inheritance
    search/                # Cross-entity search for command palette
    items/                 # EXAMPLE domain module — replace with your domain
  api/v1/                  # Thin route wrappers (auth + service call + response)
  web/
    router.py              # Jinja2 page routes
    templates/             # Jinja2 + HTMX + Alpine.js, dark Grafana-style theme
    static/                # CSS + JS (no npm, no build step, all CDN)
deploy/                    # Nginx, systemd, install.sh, backup.sh, logrotate
alembic/                   # Database migrations
tests/                     # pytest + httpx async test client
```

## Parallel Development Rules

### What an agent working on a module CAN do:
- Modify anything inside `services/<their_module>/`
- Add new schemas to their `schemas.py`
- Add new methods to their service class
- Add migrations for tables they own
- Write tests for their module

### What an agent MUST NOT do:
- Import models from another service's `models.py`
- Write SQL joining another service's tables
- Modify another service's schemas or service interface
- Add columns to tables they don't own
- Put business logic in API routes

### Cross-service communication:
```python
# WRONG: findings service imports hosts model
from services.hosts.models import Host  # NEVER DO THIS

# RIGHT: findings service calls hosts service
class FindingsService:
    def __init__(self, ..., hosts_service: HostsService):
        self._hosts = hosts_service

    async def get_with_context(self, tenant_id, finding_id):
        finding = await self._repo.get(finding_id)
        host = await self._hosts.get_by_ip(tenant_id, finding.host_ip)
        return FindingWithHost(finding=finding, host=host)
```

## Tech Stack

- **FastAPI** — async, native OpenAPI docs, dependency injection
- **SQLAlchemy 2.x** async + **Alembic** migrations
- **PostgreSQL** (production) / **SQLite+aiosqlite** (dev)
- **Pydantic v2** — schemas + env config
- **passlib[bcrypt]** + **python-jose** — auth (pin bcrypt>=4.0,<5.0)
- **Jinja2** + **HTMX** + **Alpine.js** + **Chart.js** — all CDN, no npm, no build step
- **Celery** + **Redis** — background tasks (optional)

## Auth Chain (priority order)

| Priority | Method | Header/Token | Use Case |
|----------|--------|-------------|----------|
| 1 | Cloudflare Zero Trust | `Cf-Access-Authenticated-User-Email` | Behind CF Access tunnel |
| 2 | Azure AD / Entra ID | `X-MS-CLIENT-PRINCIPAL-NAME` | Behind Azure App Proxy |
| 3 | JWT Bearer | `Authorization: Bearer <token>` | Local login |
| 4 | API Key | `X-API-Key: <key>` | Programmatic/agent access |

External providers auto-provision users on first login. `ADMIN_EMAIL` env var auto-promotes to superadmin. Unknown emails get created but need tenant assignment from an admin.

## RBAC Model

4 roles with hierarchy: `superadmin > admin > analyst > viewer`

```python
# In user_tenant_roles junction table:
# A user can have different roles for different tenants
# superadmin has implicit access to all tenants

require_role(RoleName.ANALYST)  # allows analyst, admin, superadmin
require_role(RoleName.VIEWER)   # allows everyone
require_superadmin              # superadmin only
```

## Settings System (4-tier KV with inheritance)

Single `settings` table: `(scope, scope_id, key, value_json)` with unique constraint.

Resolution chain: `user → tenant → global → schema default`

```python
# Each tier has a Pydantic schema for validation:
class GlobalSettings(BaseModel):
    session_timeout_minutes: int = Field(15, ge=5, le=1440)
    retention_days_default: int = Field(90, ge=1, le=3650)
    ...

class TenantSettings(BaseModel):
    retention_days_override: int | None = Field(None, ge=1, le=3650)
    ...

class UserSettings(BaseModel):
    theme: str = Field("system", pattern="^(dark|light|system)$")
    page_size: int = Field(25, ge=10, le=200)
    ...
```

API: `GET/PATCH /settings/global`, `/settings/tenants/{slug}`, `/settings/users/me`, `GET /settings/effective`

## Security Patterns Baked In

- **RBAC on every route** — superadmin/admin/analyst/viewer per tenant
- **API key + refresh token O(1) lookup** — SHA-256 prefix column, not O(n*bcrypt) scan
- **Provider-disable lockout prevention** — refuses to disable last auth provider if no local-password superadmin exists
- **JWT + admin password startup guards** — app refuses to start in production with default secrets
- **Tenant list scoped to user's access** — non-superadmins only see their assigned tenants
- **Error messages sanitized** — no internal details leaked to clients
- **SMTP password write-only** — masked in GET responses, never in audit logs
- **Security headers** — HSTS, CSP, X-Frame-Options, rate limiting via middleware

## Service Registry (Dependency Injection)

```python
class ServiceRegistry:
    def __init__(self, session_factory):
        # Layer 1: Infrastructure (no cross-deps)
        self.audit = AuditService(session_factory)
        self.auth = AuthService(session_factory, self.audit)
        self.tenants = TenantsService(session_factory, self.audit)
        self.users = UsersService(session_factory, self.audit)
        self.settings = SettingsService(session_factory)

        # Layer 2: Domain services
        self.items = ItemsService(session_factory, self.audit)

        # Layer 3: Composite services (call Layer 2 services)
        self.search = SearchService(session_factory, self.items, self.tenants)
```

FastAPI routes get services via dependency injection:
```python
@router.get("")
async def list_items(
    slug: str,
    user: UserContext = Depends(require_role(RoleName.VIEWER)),
    svc: ItemsService = Depends(get_items_service),
):
    ...
```

## Bootstrap Launcher (`launch.py`)

A zero-dependency Python script that handles the full startup lifecycle:
- Python version gate (configurable `MIN_PYTHON`)
- Venv creation + automatic re-exec inside venv via `os.execv`
- Dependency caching: hashes `pyproject.toml`, skips reinstall when unchanged
- `.env.example` → `.env` auto-copy on first run
- PID file management + port cleanup (kills stale processes)
- Crash supervision: tracks rapid failures, auto-restarts on exit code 75
- Environment probing: writes `data/env_probe.json` for the web UI
- Crash log persistence to `data/crash_logs/`

Configure via constants at the top of `launch.py`:
```python
APP_NAME = "MyApp"
ENTRY_POINT = "app.main:app"
MIN_PYTHON = (3, 10)
DEFAULT_PORT = 8000
```

## Admin API (`/api/v1/admin/`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/version` | GET | Version, git branch/commit/dirty, `build_ts` (ISO timestamp from server start) |
| `/update-check` | GET | Dual detection: local file mtime changes + remote git commits behind |
| `/restart` | POST | `git pull --ff-only` then restart server |
| `/restart-only` | POST | Restart without pulling updates |

The update-check snapshots source file mtimes at startup and compares against current disk state every time it's called — catches edits made while the server runs (Claude Code, manual edits, `git pull` in another terminal).

## Background Task Engine (`app/core/task_engine.py`)

In-process async task lifecycle without Celery/Redis:
- States: PENDING → RUNNING → COMPLETED/FAILED/CANCELLED/WAITING
- Progress tracking (0-100) with status text
- Output streaming via per-subscriber `asyncio.Queue`
- Human-in-the-loop: `ask_question()` / `submit_answer()` with configurable timeout
- REST API: `/api/v1/tasks/summary`, `/tasks`, `/tasks/{id}/cancel`, `/tasks/{id}/respond`, `/tasks/clear-finished`
- WebSocket: `/api/v1/ws/tasks/{task_id}` — live streaming with 30s keepalive

## TOTP / Two-Factor Authentication

Full 2FA stack baked in:
- TOTP secret generation via `pyotp` + QR code via `qrcode`
- JWT flow: successful password login with TOTP enabled returns a 5-minute `totp_pending` partial token
- `get_current_user` blocks access when `totp_pending` claim is present
- Backup codes: 8 random codes, bcrypt-hashed, single-use
- `must_change_password` flag: forces password rotation on first login
- Login UI: two-step Alpine.js flow (credentials → TOTP code)
- Endpoints: `/auth/totp/setup`, `/totp/enable`, `/totp/verify`, `/totp/disable`

## Request Logging (`app/core/request_logging.py`)

Dual-mode logging middleware:
- Request/response timing on every request
- Conditional body capture in debug mode with sensitive path redaction
- File mode: `RotatingFileHandler` (5MB, 3 backups) to `data/logs/{app_name}.log`
- Stdout mode: `StreamHandler` only (for containers / Azure Container Insights)
- Toggle via `app_env` setting (development → file, production → stdout)

## Auto-Update UI (base.html)

The frontend polls `/api/v1/admin/update-check` every 30s and when an update is detected:
1. Plays a notification ding (Web Audio API)
2. Shows a centered modal with SVG countdown ring (10s)
3. "Restart Now" / "Cancel" buttons
4. Auto-restarts when countdown reaches 0
5. Full-page restart overlay with server health polling until new instance is up

Also includes: server status indicator (nav), task switcher (Tab key), tricolor notification bell, toast system, `appCache` (stale-while-revalidate fetch wrapper), mobile responsive sidebar.

## Docker & CI/CD

- **Dockerfile**: Multi-stage build, non-root user, `HEALTHCHECK`, configurable port via `APP_PORT` arg
- **azure-pipelines-ci.yml**: Ruff lint → pytest → Docker build + ACR push (placeholder service connection names)
- **azure-pipelines-cd.yml**: 3-stage deployment (Dev auto, Test + Prod with manual approval gates)

## Proven Gotchas

1. **Composite services may not need session_factory** — Pure composites that only call other services don't need a DB session. Only give them session_factory if they own transactions (e.g., ingestion pipelines).
2. **bcrypt version pinning** — passlib 1.7.x is incompatible with bcrypt 5.x (`__about__` attribute removed). Pin `bcrypt>=4.0,<5.0`.
3. **SQLite dev startup is slow** — Creating 16+ tables + bcrypt hashing on startup takes ~20 seconds. Consider pre-built dev DB or patience.
4. **Hatch build config** — If your package directory doesn't match your project name, add `[tool.hatch.build.targets.wheel] packages = ["app"]` to pyproject.toml.
5. **Starlette TemplateResponse signature** — Newer versions: `TemplateResponse(request, "name.html", context_dict)` not `TemplateResponse("name.html", {"request": request})`.
6. **SQLite doesn't add columns to existing tables** — `create_all()` only creates tables that don't exist. Delete the `.db` file when models change during dev, or use Alembic migrations.

## How to Use This Template

### Starting a new app:
1. `git clone https://github.com/scrampker/fastapi-modular-template.git my-app`
2. `cd my-app && rm -rf .git && git init` (or keep the remote to pull template updates)
3. Rename `app/` to your project name
4. Update `pyproject.toml` (name, description, packages path)
5. Keep universal modules (`auth`, `tenants`, `users`, `audit`, `settings`) as-is
6. Delete `services/items/` and add your domain modules
7. Wire new modules into `service_registry.py`, `dependencies.py`, and `api/v1/router.py`
8. Design schemas first (the contracts), then implement services
9. Each developer/agent gets assigned a module directory

### Adding a new service module:
1. Create `services/<name>/` with `__init__.py`, `schemas.py`, `models.py`, `repository.py`, `service.py`
2. Design `schemas.py` first — these are the contract other modules depend on
3. Implement `repository.py` with async SQLAlchemy queries (always filter by `tenant_id`)
4. Implement `service.py` calling the repository
5. Add to `ServiceRegistry.__init__()` in the appropriate layer
6. Add `get_<name>_service()` to `dependencies.py`
7. Create `api/v1/<name>.py` with thin route wrappers
8. Mount the router in `api/v1/router.py`
9. Import the model in `main.py` lifespan for dev auto-creation

## Contributing Back to the Template

When building any app and you discover a generic improvement:
1. `cd /script/fastapi-modular-template && git pull`
2. Make the change (strip domain-specific details, use "tenant"/"items" naming)
3. Test that the template still starts: `cd /script/fastapi-modular-template && pip install -e . && uvicorn app.main:app`
4. Commit and push to `scrampker/fastapi-modular-template`
5. **NEVER put app-specific domain code in the template** — only universal infrastructure

## Environment Variables

See `.env.example` for all available settings. Key ones:

```bash
DATABASE_URL=sqlite+aiosqlite:///./app.db          # Dev
DATABASE_URL=postgresql+asyncpg://user:pass@host/db # Production

JWT_SECRET_KEY=<random-64-chars>                    # REQUIRED in production
INIT_ADMIN_EMAIL=admin@example.com
INIT_ADMIN_PASSWORD=<strong-password>               # REQUIRED in production

TRUSTED_IDENTITY_PROVIDERS=cloudflare,azure         # Comma-separated
ADMIN_EMAIL=you@example.com                         # Auto-promoted to superadmin
CF_TUNNEL_URL=https://app.example.com               # Shown as SSO link on login
```
