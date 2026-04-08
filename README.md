# FastAPI Modular Template

A production-ready FastAPI template for building multi-tenant web applications
with role-based access control, pluggable service modules, and a full deployment
stack out of the box.

---

## What It Is

This template gives you a working, deployable FastAPI application with the
architectural decisions already made. Clone it, rename the placeholder `app`/
`myapp` identifiers to your project name, and start adding your business logic
as self-contained service modules.

It is designed for applications that need:
- Multiple tenants sharing a single deployment with data isolation
- Role-based access control (global and per-tenant roles)
- A settings system with four levels of configuration
- Standard auth flows (local + optional SSO via header injection)
- A modular codebase that multiple developers can work on in parallel

---

## Key Features

| Feature | Details |
|---------|---------|
| **RBAC** | Global admin, tenant admin, tenant user, and read-only roles |
| **Multi-tenancy** | Per-tenant data isolation via SQLAlchemy row-level filtering |
| **Settings hierarchy** | Global admin > global user > per-user > per-customer overrides |
| **Auth** | JWT (local) + optional Cloudflare/Azure AD header-based SSO |
| **Rate limiting** | Per-endpoint and per-IP via slowapi |
| **Async SQLAlchemy** | aiosqlite (dev) or asyncpg/PostgreSQL (prod) |
| **Alembic migrations** | Async-compatible, autogenerate from models |
| **Background tasks** | Celery + Redis (optional, drop-in) |
| **Deploy stack** | Gunicorn + Uvicorn, Nginx, systemd, Certbot |
| **Automated installer** | `deploy/install.sh` handles everything end-to-end |
| **Backups** | Auto-detect SQLite/PostgreSQL, compress, rotate |

---

## Quick Start

### 1. Clone and rename

```bash
git clone https://github.com/yourorg/fastapi-modular-template.git myapp
cd myapp

# Rename the placeholder throughout the codebase
grep -rl 'myapp\|fastapi-modular-template' . \
    --include='*.py' --include='*.toml' --include='*.env*' \
    --include='*.sh' --include='*.service' --include='*.conf' \
  | xargs sed -i 's/fastapi-modular-template/myapp/g; s/myapp/myapp/g'
```

### 2. Install for development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Edit .env — set SECRET_KEY at minimum
```

### 3. Create the database and run

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/docs` for the interactive API documentation.

### 4. Run tests

```bash
pytest --cov=app --cov-report=term-missing
```

---

## Architecture

```
fastapi-template/
  app/
    core/
      config.py          # Settings (pydantic-settings, env-driven)
      database.py        # Async engine, session factory, Base
      security.py        # JWT creation/verification, password hashing
      dependencies.py    # FastAPI dependency injectors (get_db, get_current_user)
      middleware.py       # Rate limiting, request ID, logging
      celery_app.py      # Celery application factory (optional)
    services/
      users/             # User model, CRUD, router, schemas
      tenants/           # Tenant model, CRUD, router, schemas
      auth/              # Login, register, SSO callback, token refresh
      settings/          # Four-level settings service
      <your_module>/     # Add new service modules here
    static/              # CSS, JS, images (served by Nginx in production)
    templates/           # Jinja2 HTML templates
    main.py              # FastAPI app factory, router registration
  alembic/               # Migration environment and version scripts
  tests/                 # pytest suite (mirrors app/ structure)
  deploy/                # Systemd units, Nginx config, installer, backup
  pyproject.toml
  .env.example
```

### Request lifecycle

```
Client
  -> Nginx (TLS termination, rate limit, static files)
  -> Gunicorn / Uvicorn worker
  -> FastAPI middleware (request ID, logging)
  -> Rate limit check (slowapi)
  -> Auth dependency (JWT decode or SSO header validation)
  -> RBAC dependency (role check)
  -> Router / endpoint handler
  -> Service layer (business logic)
  -> SQLAlchemy async session
  -> Database
```

---

## How to Add a Module

Adding a new feature domain (e.g. "reports") takes five steps:

**1. Create the directory**

```bash
mkdir -p app/services/reports
touch app/services/reports/__init__.py
```

**2. Define the model** (`app/services/reports/models.py`)

```python
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
```

**3. Define schemas** (`app/services/reports/schemas.py`)

```python
from pydantic import BaseModel

class ReportCreate(BaseModel):
    title: str

class ReportRead(BaseModel):
    id: int
    tenant_id: int
    title: str
    model_config = {"from_attributes": True}
```

**4. Write the service layer** (`app/services/reports/service.py`)

```python
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Report
from .schemas import ReportCreate

async def create_report(db: AsyncSession, tenant_id: int, data: ReportCreate) -> Report:
    report = Report(tenant_id=tenant_id, title=data.title)
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report
```

**5. Register the router** (`app/services/reports/router.py` + `app/main.py`)

```python
# router.py
from fastapi import APIRouter, Depends
from app.core.dependencies import get_current_user, get_db
from .service import create_report
from .schemas import ReportCreate, ReportRead

router = APIRouter(prefix="/reports", tags=["Reports"])

@router.post("/", response_model=ReportRead)
async def create(data: ReportCreate, db=Depends(get_db), user=Depends(get_current_user)):
    return await create_report(db, user.tenant_id, data)
```

```python
# app/main.py — add one line
from app.services.reports.router import router as reports_router
app.include_router(reports_router, prefix="/api/v1")
```

**6. Generate a migration**

```bash
alembic revision --autogenerate -m "add reports table"
alembic upgrade head
```

**7. Add a test** (`tests/services/test_reports.py`)

```python
async def test_create_report(authenticated_client):
    response = await authenticated_client.post("/api/v1/reports/", json={"title": "Q1"})
    assert response.status_code == 200
    assert response.json()["title"] == "Q1"
```

---

## Settings System

The settings service resolves values in priority order (highest wins):

```
1. Per-customer override  (CustomerSetting table)
2. Per-user override      (UserSetting table)
3. Global user default    (GlobalSetting, scope=user)
4. Global admin default   (GlobalSetting, scope=admin)
```

This lets global admins set safe defaults, users personalize their experience,
and per-customer overrides support enterprise customization without code changes.

---

## External Auth (SSO)

Enable `EXTERNAL_AUTH_ENABLED=true` and configure a Cloudflare Access policy
or Azure AD Application Proxy to forward the verified user email in the
`X-Forwarded-Email` header. The auth middleware will create or update the local
user record on each request, skipping password verification entirely.

Local JWT auth remains available as a fallback when SSO is disabled.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI 0.110+ |
| ASGI server | Uvicorn (dev) / Gunicorn + Uvicorn workers (prod) |
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Validation | Pydantic v2 |
| Auth | python-jose (JWT) + passlib/bcrypt |
| Rate limiting | slowapi |
| Background tasks | Celery + Redis (optional) |
| Templates | Jinja2 |
| Reverse proxy | Nginx |
| Process manager | systemd |
| TLS | Let's Encrypt / Certbot |
| Testing | pytest + pytest-asyncio + httpx |

---

## Contributing

1. Fork the repository and create a feature branch.
2. Follow the coding standards in `rules/` (immutability, small functions, explicit error handling).
3. Write tests first (TDD) — maintain 80%+ coverage.
4. Run `ruff check app tests` and `pytest` before opening a PR.
5. Use conventional commit messages (`feat:`, `fix:`, `refactor:`, etc.).
