# Implementation Plan: ScottyDev Pivot

## Executive Summary

ScottyDev is the orchestration platform that sits above the six Scotty apps. V4 architecture is locked: Shape C repo split (new `scottydev` repo, `scottycore` stays as pip package), database-per-workspace with Linux UID isolation, AI-required connector setup wizard, user-managed master encryption key in `.secrets/`, read-only AI chat in Phase 2 with write tools gated to Phase 3.

The plan runs six 2-week sprints (12 weeks) to deliver a shippable v1. Phase 0 (pre-sprint) addresses the vendored-to-pip migration that currently makes all scottycore releases non-functional for downstream apps. The critical-path blocker is that three apps — scottybiz, scottystrike, scottylab-app — carry vendored copies of the `scottycore/` directory tree in their repos rather than a real pip dependency, causing the `scottycore-upgrade.yml` bump step to exit silently with `SystemExit("pyproject.toml does not contain a scottycore git+https dependency")`. Until this is fixed, every scottycore release is a no-op for those apps.

The v1 cut delivers: working pip-pinned upgrade pipeline for three real scottycore-consumer apps, the complete scottydev repo with workspace isolation + connector system + encryption + install wizard, a web UI with workspace creation flow and read-only AI chat, and scottylab-app integrated as the first ExternalRepoConnector target. Write tools, pattern discipline features (shared_core, sync_watcher, promote_scan), and the multi-workspace migration CLI are Phase 2 deliverables. Docker isolation mode is Phase 3.

---

## Sprint Timeline

| Sprint | Dates (from start) | Theme | Person-days |
|--------|-------------------|-------|-------------|
| Phase 0 | Pre-Sprint | Vendor-to-pip migration + APPS list fix | 4 |
| Sprint 1 | Weeks 1–2 | scottydev repo skeleton + core isolation model | 8 |
| Sprint 2 | Weeks 3–4 | WorkspaceService + encryption + install wizard | 7 |
| Sprint 3 | Weeks 5–6 | ConnectorBase + built-in connectors + scottycore-init.py rewrite | 8 |
| Sprint 4 | Weeks 7–8 | Web UI + workspace creation flow + feature gating | 8 |
| Sprint 5 | Weeks 9–10 | Read-only AI chat + scottylab-app HTTP connector + migrate-workspaces CLI | 9 |
| Sprint 6 | Weeks 11–12 | Integration hardening + scottycore-patterns repo + v1 release | 6 |
| **Total** | | | **50 person-days** |

---

## Phase 0 — Vendored-to-Pip Migration (Pre-Sprint, 4 person-days)

### Context

The `scottycore-upgrade.yml` workflow in every app contains a bump step that searches for the regex pattern `scottycore\s*@\s*git\+https://forgejo\.scotty\.consulting/scotty/scottycore\.git@v\d+\.\d+\.\d+` in `pyproject.toml`. None of the apps have this pattern. All three real scottycore consumers (scottybiz, scottystrike, scottylab-app) instead have `packages = ["scottycore"]` in their `[tool.hatch.build.targets.wheel]` section, meaning they vendor the entire `scottycore/` directory from their own repo tree. The bump step raises `SystemExit("pyproject.toml does not contain a scottycore git+https dependency")`, sets `changed=false`, and the workflow ends without creating a PR.

Two additional apps (scottysync, scottomation) are in the APPS dispatch list but do not use scottycore at all. They receive no-op dispatches each release, adding noise without value.

### Apps requiring vendored-to-pip migration

| App | Current state | Migration action |
|-----|--------------|-----------------|
| **scottybiz** (`/script/scottybiz`) | `packages = ["scottycore"]`, vendors entire scottycore/ tree | Add pip dep, remove vendored tree, update imports |
| **scottystrike** (`/script/scottystrike`) | `packages = ["scottycore", "app"]`, vendors scottycore/ tree | Add pip dep, remove vendored tree, update imports |
| **scottylab-app** (`/script/scottylab-app`) | `packages = ["scottycore"]`, vendors scottycore/ tree | Add pip dep, remove vendored tree, update imports |

### Apps requiring APPS list cleanup (no migration needed)

| App | Action | Reason |
|-----|--------|--------|
| **scottysync** | Remove from release.yml APPS list | Uses its own stack (`packages = ["server"]`), no scottycore dep; upgrade dispatch is irrelevant noise |
| **scottomation** | Remove from release.yml APPS list | Uses setuptools, no scottycore dep; upgrade dispatch is irrelevant noise |

### Phase 0 task breakdown

**P0.1 — Serialize against scottystrike active session (0.5 day)**
- Action: Coordinate with the active scottystrike session (Discover epic + mesh sync + learn-mode). Check git status on `/script/scottystrike`. If uncommitted changes or in-progress branch work exists, the migration must wait until that session either commits+pushes or explicitly pauses. Do NOT rebase over in-flight work.
- Why: The migration removes the `scottycore/` subtree from the scottystrike repo. If the active session has modified files inside that subtree (e.g., `scottycore/services/sync/`, `scottycore/services/backup/` per v0.1.2 release notes), the migration creates a direct conflict.
- Gate: Do not begin P0.2–P0.4 until scottystrike session confirms it is paused or all changes are committed to Forgejo.
- Risk: High — this is the single most dangerous sequencing constraint in the entire plan.

**P0.2 — Migrate scottybiz (1 day)**
File: `/script/scottybiz/pyproject.toml`

Step 1: Confirm current scottycore package version with `python3 -c "import scottycore; print(scottycore.__version__)"` from within the scottybiz venv or check `/script/scottycore/pyproject.toml` for the current version (v0.1.2 as of today).

Step 2: Add the pip dependency to scottybiz's `pyproject.toml`:
```toml
dependencies = [
    "scottycore @ git+https://forgejo.scotty.consulting/scotty/scottycore.git@v0.1.2",
    # keep existing non-scottycore deps unchanged
]
```
Remove all scottycore framework dependencies that are now provided transitively (fastapi, sqlalchemy, alembic, pydantic, passlib, etc.). Keep any app-specific dependencies (pytesseract, Pillow, opencv if present in scottybiz ai extras).

Step 3: Change `[tool.hatch.build.targets.wheel]` from `packages = ["scottycore"]` to `packages = ["scottybiz"]` (the app's own package directory, not the vendored framework).

Step 4: Delete the vendored `scottycore/` directory from the scottybiz repo root. Verify that the app package directory (`scottybiz/` or `app/`) contains only scottybiz-specific code, no framework modules.

Step 5: Run `pip install -e ".[dev]"` in scottybiz. Fix any import errors (typically: imports of `scottycore.*` that break because the vendored tree had local modifications).

Step 6: Run `pytest -q`. Target: all tests pass. If tests fail due to API changes between the vendored scottycore version and v0.1.2, fix the call sites. Do not modify scottycore itself.

Step 7: Commit: `chore: migrate scottybiz to pip-pinned scottycore v0.1.2`

Step 8: Verify the upgrade workflow works: manually trigger `scottycore-upgrade.yml` workflow_dispatch on Forgejo with version=0.1.2 and confirm the bump step finds the pin and sets `changed=false` (no new bump needed since already at latest). This confirms the regex now matches.

Acceptance: `pytest` passes, `grep "scottycore @" pyproject.toml` returns a match, `packages = ["scottycore"]` is gone.

**P0.3 — Migrate scottylab-app (1 day)**
File: `/script/scottylab-app/pyproject.toml`

Same procedure as P0.2. Additional consideration: scottylab-app has domain-specific extras (`pyyaml`, `asyncssh`, `httpx`) that are not in scottycore's base dependencies — retain these explicitly.

Also: scottylab-app was scaffolded recently and is actively being developed. Check its current git status before removing the vendored tree. The app's own code lives in `scottycore/` (per `packages = ["scottycore"]` with `packages = ["scottycore"]` in the wheel config) but actually the app code might be in a subdirectory. Inspect the actual directory layout of `/script/scottylab-app/` before deleting anything.

Note: scottylab-app has an additional complication — it was scaffolded from the scottycore template and `packages = ["scottycore"]` means the app's code IS organized under a `scottycore/` directory name. After migration, the app package directory should be renamed to `scottylab_app/` or `app/` to avoid confusion with the pip package. Update `pyproject.toml` and all internal imports accordingly. This is a non-trivial rename but essential for correctness.

Acceptance: `/health` endpoint returns 200, `pytest` passes, vendored scottycore tree is gone.

**P0.4 — Migrate scottystrike (1 day)**
File: `/script/scottystrike/pyproject.toml`

Same procedure as P0.2. Note: scottystrike has `packages = ["scottycore", "app"]`, meaning its app code lives in an `app/` directory separate from the vendored `scottycore/`. After migration, change to `packages = ["app"]` (or rename the app package if desired).

scottystrike also has non-scottycore AI dependencies (`anthropic>=0.34`, `openai>=1.50`) that were in its pyproject but are not in scottycore's base deps — retain these.

scottystrike is in the most active development state. Run the full CI suite after migration.

Acceptance: `pytest` passes with 80%+ coverage, CI green on Forgejo.

**P0.5 — Clean up APPS dispatch list (0.5 day)**
File: `/script/scottycore/.forgejo/workflows/release.yml`

Remove `scottysync` and `scottomation` from the APPS list. Add a comment explaining the criteria for inclusion: apps must have `scottycore @ git+https://...` in their `pyproject.toml` to be in this list.

Optionally: add a guard step at the start of `scottycore-upgrade.yml` template that checks for the pip pin before the bump step and exits 0 with a clear message rather than raising SystemExit from Python. This prevents confusing silent no-ops if a new app is ever added to APPS before getting the pin.

Commit: `chore: fix release APPS list — remove non-scottycore consumers`

### Phase 0 rollback procedure

If any app's tests fail after removing the vendored tree:
1. `git checkout -- scottycore/` in the affected app to restore the vendored tree
2. Investigate the failing tests to identify which scottycore API was modified locally
3. Either: (a) update the app call sites to match scottycore v0.1.2 API, or (b) if the local modification is valuable, promote it to scottycore via `/promote` first, cut a new scottycore patch release, then retry the migration
4. Do not keep the vendored tree as a permanent solution — it defeats the entire upgrade pipeline

### Phase 0 migration order

1. scottybiz (lowest risk, simplest app)
2. scottylab-app (deployed, needs verification against live endpoint)
3. scottystrike (highest risk, most active development — do last)

### Phase 0 acceptance criteria

- [ ] `grep "packages.*scottycore" /script/scottybiz/pyproject.toml` returns no match
- [ ] `grep "packages.*scottycore" /script/scottylab-app/pyproject.toml` returns no match
- [ ] `grep "packages.*scottycore" /script/scottystrike/pyproject.toml` returns no match
- [ ] `grep "scottycore @" /script/scottybiz/pyproject.toml` returns a match
- [ ] `grep "scottycore @" /script/scottylab-app/pyproject.toml` returns a match
- [ ] `grep "scottycore @" /script/scottystrike/pyproject.toml` returns a match
- [ ] Triggering `scottycore-upgrade.yml` workflow_dispatch on scottybiz with version=0.1.2 does NOT raise SystemExit (bump step exits cleanly)
- [ ] All three apps' test suites pass post-migration
- [ ] `/health` returns 200 on `https://scottylab.scotty.consulting`
- [ ] scottysync and scottomation removed from release.yml APPS list

---

## Sprint 1 — ScottyDev Repo Skeleton + Core Isolation Model (Weeks 1–2, 8 person-days)

### Goal

Create the `scottydev` repo on Forgejo, establish the full directory scaffold, implement `core/isolation.py` (Linux UID allocation, workspace directory provisioning, Postgres database provisioning), and implement the admin database schema. By end of sprint, `uvicorn scottydev.main:app` starts, creates the admin DB tables, and the install wizard CLI generates the master key file.

### Non-goals for Sprint 1

- No workspace creation UI (Sprint 4)
- No connectors (Sprint 3)
- No web pages beyond `/health` (Sprint 4)
- No AI chat (Sprint 5)
- No migration from existing scottycore scripts

### Deliverables

**S1.1 — Forgejo + GitHub repo creation (0.5 day)**
- Create `scotty/scottydev` on `https://forgejo.scotty.consulting` via API
- Create `scrampker/scottydev` on GitHub via `gh`
- Set up dual-remote push
- Initial commit: empty directory + `.gitignore` (includes `.secrets/`)
- Add `scottydev` to scottycore's `config/apps.yaml` (path `/script/scottydev`)

**S1.2 — pyproject.toml + package structure (0.5 day)**
File: `/script/scottydev/pyproject.toml`

```toml
[project]
name = "scottydev"
version = "0.1.0"
dependencies = [
    "scottycore @ git+https://forgejo.scotty.consulting/scotty/scottycore.git@v0.1.2",
    "cryptography>=42.0",
    "asyncpg>=0.29.0",
    "alembic>=1.13.0",
]
```

Directory structure:
```
scottydev/
  scottydev/
    core/
      config.py
      database.py
      auth.py        # thin re-export from scottycore
      encryption.py
      isolation.py
    services/
      workspaces/
      connectors/
      ai_chat/
      projects/
    api/v1/
    web/
      templates/
      static/
  scripts/
    scottycore-init.py    # placeholder — Sprint 3 rewrites this
  .secrets/               # gitignored
  .gitignore
  pyproject.toml
  alembic.ini
  alembic/
    env.py
    versions/
```

**S1.3 — Admin database schema (1 day)**
File: `/script/scottydev/scottydev/services/workspaces/models.py`

Implement all three admin DB tables per v4 Q12:
- `ScottyDevWorkspace` — includes `linux_uid`, `ws_db_conn_enc`, soft-delete fields
- `ScottyDevWorkspaceSummary` — replicated aggregate
- `ScottyDevUser` — global users with `is_global_admin`

File: `/script/scottydev/alembic/versions/001_admin_tables.py`
- Migration for all three admin tables
- Postgres sequence `ws_uid_seq` starting at 60000 for UID allocation

File: `/script/scottydev/scottydev/core/database.py`
- Admin DB engine + async session factory (reads `DATABASE_URL` from config)
- Per-workspace DB session factory (`get_workspace_session(slug: str)`) that decrypts the workspace DB connection string from the admin DB row and opens a connection

**S1.4 — Core config + startup guards (0.5 day)**
File: `/script/scottydev/scottydev/core/config.py`

Pydantic Settings with required fields:
- `DATABASE_URL` — admin DB
- `SECRET_KEY` — JWT signing
- `CONNECTOR_ISOLATION_MODE: Literal["subprocess", "docker"] = "subprocess"` — Decision 7 flag
- `SCOTTYDEV_ROOT: Path` — repo root, used to locate `.secrets/workspace_encryption_key`
- `INIT_ADMIN_EMAIL`, `INIT_ADMIN_PASSWORD`

Startup guard in `scottydev/main.py` lifespan:
```python
key_path = Path(settings.scottydev_root) / ".secrets" / "workspace_encryption_key"
if not key_path.exists():
    raise RuntimeError(
        "ERROR: .secrets/workspace_encryption_key not found. "
        "ScottyDev cannot start without the master encryption key. "
        "Recovery: copy the key from your password manager into "
        ".secrets/workspace_encryption_key and chmod 0600 it."
    )
```

**S1.5 — core/encryption.py (0.5 day)**
File: `/script/scottydev/scottydev/core/encryption.py`

Implement exactly as specified in v4 Q15:
- `encrypt_json(data: dict, key: bytes) -> bytes` — AES-256-GCM, 12-byte nonce prepended
- `decrypt_json(blob: bytes, key: bytes) -> dict`
- `_load_master_key() -> bytes` — reads from `.secrets/workspace_encryption_key`
- Unit tests in `tests/test_encryption.py` — encrypt/decrypt round-trip, wrong-key rejection, corrupted ciphertext rejection

**S1.6 — core/isolation.py (2 days)**
File: `/script/scottydev/scottydev/core/isolation.py`

This is the most complex Sprint 1 component. Implements:

```python
async def allocate_workspace_uid(db_session) -> int:
    """Allocate the next UID from ws_uid_seq (60000–69999)."""
    # Uses: SELECT nextval('ws_uid_seq')

async def provision_workspace_os(slug: str, linux_uid: int) -> None:
    """Create the OS-level user and data directory for a workspace.
    Runs as the scottydev process user; requires sudoers access for useradd.
    """
    # useradd --uid {linux_uid} --system --no-create-home
    #         --shell /usr/sbin/nologin ws-{slug}
    # mkdir -p /var/scottydev/workspaces/{slug}
    # chown ws-{slug}:nogroup /var/scottydev/workspaces/{slug}
    # chmod 0700 /var/scottydev/workspaces/{slug}

async def provision_workspace_db(slug: str, admin_db_session) -> str:
    """Create a dedicated Postgres database and role for the workspace.
    Returns the connection string for the new workspace DB.
    Uses the admin Postgres role (scottydev_admin) for CREATE DATABASE.
    """
    # CREATE DATABASE scottydev_ws_{slug}
    # CREATE ROLE scottydev_ws_{slug} WITH LOGIN PASSWORD '{generated}'
    # GRANT ALL ON DATABASE scottydev_ws_{slug} TO scottydev_ws_{slug}

async def teardown_workspace(slug: str, linux_uid: int, admin_db_session) -> None:
    """Hard-delete: drop DB, drop role, remove OS user and data directory."""
    # DROP DATABASE scottydev_ws_{slug}
    # DROP ROLE scottydev_ws_{slug}
    # userdel ws-{slug}
    # rm -rf /var/scottydev/workspaces/{slug}
```

The connector runner script also lives here conceptually:
- `/var/scottydev/connector-runner.py` — the fixed audited script that receives connector type + serialized config + operation as JSON via stdin, instantiates the connector, runs the operation, returns structured JSON on stdout. This script is written out by the scottydev install process (not a runtime-generated artifact).

Sudoers configuration template (documented, not auto-applied):
```
scottydev ALL = (ws-*) NOPASSWD: /usr/bin/python3 /var/scottydev/connector-runner.py *
```

Tests: mock the subprocess calls and Postgres CREATE DATABASE/ROLE operations. Do NOT require a live Postgres instance for unit tests — use dependency injection.

**S1.7 — Install wizard CLI (1 day)**
File: `/script/scottydev/scottydev/cli.py`

Implements `python -m scottydev.cli setup-wizard`:
1. Check if `.secrets/workspace_encryption_key` already exists. If yes, prompt whether to regenerate (default: no).
2. Generate `key = base64.urlsafe_b64encode(os.urandom(32)).decode()`
3. Print to terminal: "Save this key in your password manager now. It will not be shown again." — then print the key.
4. Create `.secrets/` directory if not exists, `chmod 0700`
5. Write key to `.secrets/workspace_encryption_key`, `chmod 0600`
6. Print instructions for the `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD` env vars
7. Print the sudoers snippet to add manually

Also implements the stub for `python -m scottydev.cli migrate-workspaces --confirm` (body: `raise NotImplementedError("Implemented in Sprint 2")`) so the entry point exists.

**S1.8 — Workspace per-workspace schema (0.5 day)**
File: `/script/scottydev/scottydev/services/workspaces/ws_models.py`

Implement all per-workspace DB tables per v4 Q12:
- `WorkspaceConfig` — single-row config with `connector_type`, `connector_config_enc`, `ai_config_enc`, `features_json`, `data_key_enc`
- `WorkspaceProject` — app registry
- `WorkspaceMember`
- `WorkspaceChatSession`, `WorkspaceChatMessage`
- `WorkspaceAuditLog`

File: `/script/scottydev/alembic/versions/002_workspace_schema.py`
Migration that creates these tables — applied per-workspace by the migration runner, not against the admin DB.

**S1.9 — WorkspaceFeatures schema + validator (0.5 day)**
File: `/script/scottydev/scottydev/services/workspaces/schemas.py`

Implement `WorkspaceFeatures` Pydantic model exactly as in v4 Q3 (12 flags). Implement `WorkspaceFeatureValidator.validate()` that enforces the dependency graph from v4 Q11 (shared_core must be true for pattern_tracking, sync_watcher, promote_scan, core_upgrade, release_fanout, manager_agents; core_upgrade also requires framework_repo_url set). Returns a `list[str]` of validation errors.

### Sprint 1 acceptance criteria

- [ ] `python -m scottydev.cli setup-wizard` generates `.secrets/workspace_encryption_key` with mode 0600
- [ ] `uvicorn scottydev.main:app` starts and serves `/health` returning `{"status": "ok"}`
- [ ] Starting without `.secrets/workspace_encryption_key` prints the error message and exits non-zero
- [ ] Admin DB tables created on startup (Alembic `upgrade head`)
- [ ] `tests/test_encryption.py` passes (round-trip, wrong-key, corrupted ciphertext)
- [ ] `tests/test_isolation.py` passes (mocked subprocess and Postgres calls)
- [ ] `tests/test_workspace_features.py` passes (all 12 validator rules)
- [ ] `CONNECTOR_ISOLATION_MODE` config key exists, defaults to `"subprocess"`

### Sprint 1 dependencies

- Phase 0 must be complete (scottycore available as pip package — scottydev depends on it)

### Sprint 1 estimated effort: 8 person-days

---

## Sprint 2 — WorkspaceService + Multi-Workspace Alembic CLI (Weeks 3–4, 7 person-days)

### Goal

Implement the full `WorkspaceService` (workspace CRUD, provisioning lifecycle, feature flag management, credential storage), the envelope encryption for connector and AI configs, and the `migrate-workspaces` CLI. By end of sprint, a superadmin can create a workspace via the API, and the migration runner can apply Alembic upgrades to all workspace DBs.

### Non-goals for Sprint 2

- No web UI for workspace creation (Sprint 4)
- No connector wizard (Sprint 3)
- No AI chat (Sprint 5)

### Deliverables

**S2.1 — WorkspaceService core CRUD (2 days)**
File: `/script/scottydev/scottydev/services/workspaces/service.py`

```python
class WorkspaceService:
    async def create(self, name: str, slug: str, owner_user_id: UUID) -> ScottyDevWorkspace:
        """Full provisioning sequence:
        1. Allocate linux_uid from ws_uid_seq
        2. Insert row into scottydev_workspaces (is_active=True, is_deleted=False)
        3. Call isolation.provision_workspace_os(slug, linux_uid)
        4. Call isolation.provision_workspace_db(slug) → returns conn_string
        5. Encrypt conn_string with master key
        6. Update workspace row with ws_db_conn_enc
        7. Run alembic upgrade head against the new workspace DB
        8. Create WorkspaceConfig row in workspace DB (default features_json, no connector yet)
        9. Insert WorkspaceSummary row in admin DB
        10. Return workspace row
        Rollback: on any step failure, attempt teardown of provisioned resources
        """

    async def get(self, slug: str) -> ScottyDevWorkspace | None: ...

    async def list_for_user(self, user_id: UUID, is_global_admin: bool) -> list[ScottyDevWorkspace]:
        """Global admins see all active workspaces. Others see only workspaces
        where they are a member. Never returns is_deleted=True rows."""

    async def soft_delete(self, slug: str) -> None:
        """Set is_deleted=True, deleted_at=now(), hard_delete_after=now()+30d.
        Workspace immediately disappears from list_for_user results."""

    async def hard_delete(self, slug: str) -> None:
        """Called by nightly cleanup job. Calls isolation.teardown_workspace().
        Only proceeds if hard_delete_after <= now()."""

    async def restore(self, slug: str) -> None:
        """Superadmin only. Clears is_deleted, deleted_at, hard_delete_after."""

    async def update_features(self, slug: str, patch: dict) -> WorkspaceFeatures:
        """PATCH features_json. Runs WorkspaceFeatureValidator.validate() first.
        Raises HTTP 422 on validation failure. Immediate effect — no restart."""

    async def update_connector_config(self, slug: str, connector_type: str, config_dict: dict) -> None:
        """Encrypt config_dict with per-workspace data key and store in
        workspace_config.connector_config_enc. Never log the plaintext."""

    async def update_ai_config(self, slug: str, ai_config: dict) -> None:
        """Encrypt and store ai_config. Same pattern as connector config."""
```

**S2.2 — Per-workspace data key management (0.5 day)**
File: `/script/scottydev/scottydev/services/workspaces/service.py` (part of create flow)

On workspace creation, generate a 32-byte random per-workspace data key:
```python
data_key = os.urandom(32)
data_key_enc = encrypt_json({"key": base64.b64encode(data_key).decode()}, master_key)
```
Store `data_key_enc` in `workspace_config.data_key_enc`.

All subsequent encrypt/decrypt operations for that workspace use this data key (decrypted on demand from workspace_config), not the master key directly. This is the envelope encryption model from v4 Q15.

**S2.3 — Key rotation CLI command (0.5 day)**
File: `/script/scottydev/scottydev/cli.py`

Implement `python -m scottydev.cli rotate-key --old-key-file OLD --new-key-file NEW`:
1. Load old master key from `OLD` file
2. Iterate over all workspace rows in admin DB
3. For each workspace: decrypt `ws_db_conn_enc` with old key, re-encrypt with new key; open workspace DB, decrypt `workspace_config.data_key_enc` with old key, re-encrypt with new key; decrypt `connector_config_enc` and `ai_config_enc` with workspace data key — these don't need re-encryption (the data key itself is what changes)
4. Write new key to `NEW` file (does not write to `.secrets/` — operator must do that manually)
5. Print summary: N workspaces re-encrypted, any errors

**S2.4 — Multi-workspace Alembic migration runner (1 day)**
File: `/script/scottydev/scottydev/cli.py`

Implement `python -m scottydev.cli migrate-workspaces --confirm`:

```python
async def cmd_migrate_workspaces(confirm: bool) -> None:
    """Run 'alembic upgrade head' against every workspace DB.
    
    Steps:
    1. Connect to admin DB
    2. SELECT slug, ws_db_conn_enc FROM scottydev_workspaces WHERE is_deleted=FALSE
    3. For each workspace:
       a. Decrypt ws_db_conn_enc to get the connection string
       b. Set ALEMBIC_WORKSPACE_DB_URL env var
       c. Run: alembic -x workspace_db=<conn_string> upgrade head
          (alembic/env.py reads this env var and uses it as the target DB)
       d. Capture stdout/stderr
       e. Print result: "workspace scotty-land: OK" or "workspace scotty-land: FAILED"
    4. Print summary
    5. Exit non-zero if any workspace migration failed
    """
```

The `alembic/env.py` for scottydev uses the following dispatch logic:
```python
# env.py
import os
from scottydev.core.database import get_engine_for_url

workspace_db_url = os.environ.get("ALEMBIC_WORKSPACE_DB_URL")
admin_db_url = os.environ.get("DATABASE_URL")

if workspace_db_url:
    # Migrating a specific workspace DB
    target_metadata = workspace_schema_metadata
    url = workspace_db_url
else:
    # Migrating the admin DB
    target_metadata = admin_schema_metadata
    url = admin_db_url
```

Acceptance: run `python -m scottydev.cli migrate-workspaces --confirm` with two workspace DBs. Both get `upgrade head`. Idempotent — running again is a no-op.

**S2.5 — Nightly hard-delete job (0.5 day)**
File: `/script/scottydev/scottydev/services/workspaces/cleanup.py`

A background task (wired into FastAPI lifespan using the scottycore TaskEngine) that runs at midnight UTC:
```python
async def nightly_workspace_cleanup() -> None:
    """Delete workspaces past their hard_delete_after date."""
    # SELECT * FROM scottydev_workspaces
    # WHERE is_deleted=TRUE AND hard_delete_after <= now()
    # For each: call workspace_service.hard_delete(slug)
```

**S2.6 — WorkspaceService API routes (1 day)**
File: `/script/scottydev/scottydev/api/v1/workspaces.py`

Routes:
- `POST /api/v1/workspaces` — create (requires global admin)
- `GET /api/v1/workspaces` — list (scoped by user)
- `GET /api/v1/workspaces/{slug}` — get
- `DELETE /api/v1/workspaces/{slug}` — soft delete (requires workspace owner or global admin)
- `PATCH /api/v1/workspaces/{slug}/restore` — restore (global admin only)
- `PATCH /api/v1/workspaces/{slug}/features` — update features (workspace admin+)
- `GET /api/v1/workspaces/{slug}/features` — read features
- `GET /api/v1/workspaces/{slug}/local-overlay` — returns `scottydev-workspace.yaml` with secrets redacted

All routes use `require_workspace_feature()` dependency where appropriate. Implement `require_workspace_feature(flag: str)` in `scottydev/core/dependencies.py`.

**S2.7 — WorkspaceSummary update mechanism (0.5 day)**
File: `/script/scottydev/scottydev/services/workspaces/service.py`

Implement the background task that replicates workspace aggregate data to `scottydev_workspace_summary`. When a deploy completes or health check runs, the workspace request handler emits an event to an in-process `asyncio.Queue`. The background task reads from the queue and writes to the admin DB summary table. This ensures the global admin dashboard never queries workspace DBs directly.

**S2.8 — Tests (1 day)**
Files: `tests/test_workspace_service.py`, `tests/test_migrate_workspaces.py`

- WorkspaceService.create: mock `provision_workspace_os` and `provision_workspace_db`; verify rollback when provisioning fails midway
- WorkspaceService.update_features: verify all 12 validation rules
- rotate-key: mock all workspace DB connections; verify all enc columns re-encrypted
- migrate-workspaces: mock alembic subprocess call; verify correct connection strings passed

### Sprint 2 acceptance criteria

- [ ] `POST /api/v1/workspaces` creates workspace row, OS user, and Postgres DB
- [ ] Feature flag PATCH returns 422 for shared_core dependents without shared_core=true
- [ ] `python -m scottydev.cli migrate-workspaces --confirm` migrates all workspace DBs
- [ ] `python -m scottydev.cli rotate-key` re-encrypts all `*_enc` columns
- [ ] Nightly cleanup hard-deletes workspaces past retention date (tested with mocked clock)
- [ ] Tests pass at 80%+ coverage

### Sprint 2 dependencies

- Sprint 1 complete

### Sprint 2 estimated effort: 7 person-days

---

## Sprint 3 — ConnectorBase + Built-in Connectors + scottycore-init.py Rewrite (Weeks 5–6, 8 person-days)

### Goal

Implement the full connector system: `ConnectorBase`, the four built-in connectors, the `WorkspaceConnectorLoader`, the AST safety check for custom connectors, and the connector runner script. Rewrite `scottycore-init.py` to invoke `ConnectorBase` methods instead of calling `scottylab_toolkit` directly. By end of sprint, a workspace can have a connector configured and the connector runner can execute operations as the workspace UID.

### Non-goals for Sprint 3

- No connector setup wizard UI (Sprint 4 — the AI wizard runs in the web UI)
- No AI-assisted connector configuration (Sprint 5 — requires ai_chat)
- No scottylab-app HTTP connector integration (Sprint 5)

### Deliverables

**S3.1 — ConnectorBase + data types (1 day)**
File: `/script/scottydev/scottydev/services/connectors/base.py`

Implement exactly as specified in v4 Q9:
- `ProvisionResult`, `DeployResult`, `HealthResult` dataclasses
- `ConnectorBase` ABC with all six abstract methods
- `BUILT_IN_CONNECTORS` registry dict

**S3.2 — ProxmoxSSHConnector (1.5 days)**
File: `/script/scottydev/scottydev/services/connectors/proxmox_ssh.py`

Implement `ProxmoxSSHConfig` and `ProxmoxSSHConnector`. All six operations:
- `provision_app`: calls Proxmox API to create LXC, configures nginx vhost, sets up Cloudflare DNS if token provided
- `deploy_app`: SSH to nginx host and LXC IP, runs `docker compose pull && docker compose up -d`
- `configure_dns`: creates/updates Cloudflare CNAME
- `configure_tls`: configures cert apex in nginx-certs.yml (Phase 1: delegates to existing scottylab automation pattern)
- `destroy_app`: destroys LXC, removes nginx vhost, removes Cloudflare DNS
- `health_check`: HTTP GET to FQDN, returns `HealthResult`

This connector is the template for how `scottycore-init.py` currently works. Port the logic from `scottylab_toolkit/lxc.py`, `scottylab_toolkit/nginx.py`, `scottylab_toolkit/cloudflare.py`, `scottylab_toolkit/ssh.py` into the typed connector pattern.

**S3.3 — DockerSSHConnector (0.5 day)**
File: `/script/scottydev/scottydev/services/connectors/docker_ssh.py`

Implement `DockerSSHConfig` and `DockerSSHConnector`. Simpler than ProxmoxSSH — all operations target a single host via SSH with Docker Compose.

**S3.4 — SystemdConnector (0.5 day)**
File: `/script/scottydev/scottydev/services/connectors/systemd.py`

Implement `SystemdConfig` and `SystemdConnector`. Operations: systemctl start/stop/restart the service unit, pull latest code, deploy.

**S3.5 — ExternalRepoConnector (1 day)**
File: `/script/scottydev/scottydev/services/connectors/external_repo.py`

Implement `ExternalRepoConfig` and `ExternalRepoConnector`. The connector:
1. Clones/updates the external repo to `/var/scottydev/workspaces/<slug>/connector-repo/`
2. For each operation, looks up the entry point in `config.entry_points`
3. Executes the entry point as a subprocess running as the workspace UID
4. Parses the result

The `ExternalRepoConnector` is the connector that will eventually target scottylab-app's HTTP API when configured by the AI wizard in Sprint 5.

**S3.6 — WorkspaceConnectorLoader + AST safety check (0.5 day)**
File: `/script/scottydev/scottydev/services/connectors/loader.py`

Implement `WorkspaceConnectorLoader.load()` exactly as in v4 Q9. Implement `_ast_safety_check(source: str)` with the forbidden imports and builtins check.

**S3.7 — Connector runner script (0.5 day)**
File: `/var/scottydev/connector-runner.py` (deployed by install, not in the repo's Python package)

This is the fixed script that runs as the workspace UID. It:
1. Reads a JSON payload from stdin: `{"connector_type": str, "config": dict, "operation": str, "args": dict}`
2. Imports the connector from `scottydev.services.connectors`
3. Instantiates the connector with the decrypted config
4. Calls the requested operation
5. Prints the result as JSON to stdout
6. Any exception: prints `{"error": str(e)}` to stdout and exits 1

The deploy script for scottydev writes this file to `/var/scottydev/connector-runner.py` and sets permissions. Document the sudoers rule that must be added manually.

**S3.8 — Connector API routes (0.5 day)**
File: `/script/scottydev/scottydev/api/v1/connectors.py`

- `GET /api/v1/workspaces/{slug}/connector` — return connector type and masked config (no secrets)
- `DELETE /api/v1/workspaces/{slug}/connector` — clear connector config (workspace admin+)
- `POST /api/v1/workspaces/{slug}/connector/test` — call `health_check()` on the current connector, return `HealthResult`
- `POST /api/v1/workspaces/{slug}/connector/config` — save connector config (wizard output); requires `ai_chat` to have been used to configure (enforced by a flag in WorkspaceConfig `connector_wizard_completed`)

The connector wizard itself (AI-required, no click-through) lives in the AI chat service (Sprint 5). The API route to SAVE wizard output is here, but it requires the workspace to have an AI backend configured and the wizard session ID to be provided.

**S3.9 — scottycore-init.py rewrite (1 day)**
File: `/script/scottydev/scripts/scottycore-init.py`

Move `scottycore-init.py` from `/script/scottycore/scripts/scottycore-init.py` to `/script/scottydev/scripts/scottycore-init.py`. Rewrite to:
- Accept a `--connector-type` flag (default: `proxmox_ssh`)
- Import from `scottydev.services.connectors` instead of `scottylab_toolkit`
- Call `ProxmoxSSHConnector(config).provision_app(...)` instead of calling toolkit functions directly
- Keep all existing non-connector behavior (Forgejo/GitHub repo creation, CLAUDE.md generation, apps.yaml registration, workflow installation)

The old `scottycore-init.py` at `/script/scottycore/scripts/scottycore-init.py` is kept in place (read-only, not modified) during this sprint and marked as deprecated in a comment at the top. A deprecation notice pointing to the new location is added. Full removal happens in Phase 3 after all existing consumers are verified against the new script.

Note: this rewrite does NOT change the connector configuration flow (that's the AI wizard in Sprint 5). It hardcodes the ProxmoxSSH connector with config derived from the existing toolkit's paths.py defaults. The AI wizard path is added in Sprint 5.

### Sprint 3 acceptance criteria

- [ ] `WorkspaceConnectorLoader.load()` correctly instantiates all four built-in connector types
- [ ] `_ast_safety_check` blocks connectors importing `socket`, `ctypes`, `exec`, `eval`
- [ ] `ProxmoxSSHConnector.health_check()` returns a valid `HealthResult` (integration test against homelab)
- [ ] `POST /api/v1/workspaces/{slug}/connector/test` returns 200 with reachable=True for a workspace with a valid connector
- [ ] `connector-runner.py` can be invoked with `sudo -u ws-scotty-land python3 /var/scottydev/connector-runner.py` and returns JSON
- [ ] Rewritten `scottycore-init.py` in scottydev successfully scaffolds a test app (verified against homelab)
- [ ] Old `scottycore-init.py` still works (backward compatibility — not yet retired)
- [ ] Tests pass at 80%+ coverage (connector tests use mocked SSH/HTTP)

### Sprint 3 dependencies

- Sprint 1 (repo structure, encryption, isolation)
- Sprint 2 (WorkspaceService — connector config stored in workspace DB)

### Sprint 3 estimated effort: 8 person-days

---

## Sprint 4 — Web UI + Workspace Creation Flow + Feature Gating (Weeks 7–8, 8 person-days)

### Goal

Ship the ScottyDev web interface. A logged-in user can create a workspace, configure framework repo URL, see their project registry, trigger deploys, and view the dashboard. Feature-gated sections render or are hidden based on the workspace's `WorkspaceFeatures`. The workspace creation flow enforces AI backend configuration before the connector wizard can be started.

### Non-goals for Sprint 4

- No connector wizard execution (Sprint 5 — requires AI chat)
- No AI chat pane (Sprint 5)
- No scottylab-app connector integration (Sprint 5)

### Deliverables

**S4.1 — Base template + auth UI (1 day)**
Files: `/script/scottydev/scottydev/web/templates/base.html`, `login.html`

Inherit the scottycore Jinja2 + HTMX + Alpine.js + dark Grafana-style theme. Implement:
- Login page with email/password form (uses `scottycore.services.auth` JWT flow, imported from the pip package)
- Nav bar: workspace selector dropdown, user menu, task switcher
- Toast notification system (reuse scottycore base.html pattern)
- Auto-update UI polling `/api/v1/admin/update-check` (reuse scottycore pattern)

**S4.2 — Dashboard page (0.5 day)**
Files: `scottydev/web/templates/dashboard.html`, `scottydev/web/router.py`

The root `/` page. Renders feature-gated sections:
- Always visible: project registry summary, deploy status, health overview
- Visible only if `shared_core=true`: pattern drift summary, sync status
- Global admin gets workspace list from `scottydev_workspace_summary`
- Workspace admin gets single-workspace view

**S4.3 — Workspace creation wizard UI (1.5 days)**
Files: `scottydev/web/templates/workspace_new.html`, `scottydev/web/templates/workspace_setup.html`

Three-step creation flow (enforced ordering per v4 Appendix C implicit decision):

**Step 1 — Basic info**: workspace name → creates workspace record (database + OS user + Postgres DB provisioned in background, tracked by TaskEngine)

**Step 2 — AI backend configuration** (REQUIRED before Step 3):
- Dropdown of available providers: Claude CLI, Ollama (with URL), DGX Ollama, Claude API, OpenAI, Azure OpenAI, custom
- Test connection button (calls `POST /api/v1/workspaces/{slug}/ai-config/test`)
- Cannot advance to Step 3 until AI backend test returns success
- If user tries to skip, show: "An AI backend is required to run the connector setup wizard. Please configure one before continuing."

**Step 3 — Connector wizard** (requires Step 2 complete):
- Three radio options as specified in v4 Q9:
  - "I have existing automation" → shows Git URL input → starts AI wizard chat session in next page
  - "I don't have existing automation, guide me" → starts AI wizard chat session
  - "I'll write a custom Python connector myself" → shows file upload + code editor
- "Start wizard" button navigates to the AI chat pane (Sprint 5 enables the actual wizard)
- In Sprint 4: placeholder page showing "AI Wizard coming in next release" with option to manually configure via API for power users

**S4.4 — Project registry UI (1 day)**
Files: `scottydev/web/templates/projects.html`, `scottydev/web/templates/project_detail.html`

- Project list: name, repo URL, branch, last deploy status, FQDN
- Add project form: slug, name, repo URL, stack, branch, port, FQDN
- Deploy button → calls `POST /api/v1/workspaces/{slug}/projects/{project_slug}/deploy`
- Deploy log streaming: TaskEngine WebSocket connection shows live stdout/stderr
- Feature gate: `deploy_helper` must be true (always-on by default, shown for completeness)

**S4.5 — Feature flags settings page (0.5 day)**
Files: `scottydev/web/templates/workspace_settings.html`

- Toggle UI for all 12 feature flags
- Dependency visualization: grayed-out toggles for flags that require shared_core when shared_core is off
- HTMX: `PATCH /api/v1/workspaces/{slug}/features` on toggle change
- 422 errors from validation shown inline

**S4.6 — Deploy orchestration routes (1 day)**
Files: `scottydev/scottydev/api/v1/projects.py`, `scottydev/scottydev/services/projects/service.py`

- `POST /api/v1/workspaces/{slug}/projects` — add project (workspace admin+)
- `GET /api/v1/workspaces/{slug}/projects` — list projects
- `GET /api/v1/workspaces/{slug}/projects/{project_slug}` — get project details
- `DELETE /api/v1/workspaces/{slug}/projects/{project_slug}` — remove project
- `POST /api/v1/workspaces/{slug}/projects/{project_slug}/deploy` — trigger deploy via connector; uses TaskEngine; streams log; updates WorkspaceSummary aggregate

Deploy trigger flow:
1. Load workspace's connector via `WorkspaceConnectorLoader`
2. Call `connector.deploy_app()` via `subprocess.run(["sudo", "-u", f"ws-{slug}", ...])` with the serialized connector config (never logged)
3. Stream stdout to TaskEngine WebSocket
4. On completion, write deploy result to `workspace_projects` row and emit summary update

**S4.7 — route-level feature gating dependency (0.5 day)**
File: `/script/scottydev/scottydev/core/dependencies.py`

```python
def require_workspace_feature(flag: str):
    async def _check(features: WorkspaceFeatures = Depends(get_workspace_features)):
        if not getattr(features, flag):
            raise HTTPException(
                status_code=403,
                detail=f"feature {flag} not enabled for this workspace"
            )
    return _check
```

Apply to all routes that require non-default features.

**S4.8 — AI backend config routes (0.5 day)**
Files: `scottydev/scottydev/api/v1/ai_config.py`

- `POST /api/v1/workspaces/{slug}/ai-config` — save AI backend config (encrypted)
- `GET /api/v1/workspaces/{slug}/ai-config` — return config with secrets masked
- `POST /api/v1/workspaces/{slug}/ai-config/test` — test connectivity to configured AI backend (calls `ai_backends.resolve_provider()` from scottycore)

**S4.9 — Admin DB workspace list page (0.5 day)**
Files: `scottydev/web/templates/admin_workspaces.html`

Global admin view: workspace list from `scottydev_workspace_summary`, with app count, last deploy time, health status. Create workspace button. Soft-delete button (with confirmation modal). Restore button for deleted workspaces.

### Sprint 4 acceptance criteria

- [ ] A user can log in, create a workspace, and see it in the project registry
- [ ] The workspace creation flow blocks Step 3 (connector) if Step 2 (AI backend) is not configured
- [ ] Feature flag PATCH via UI updates instantly, grayed-out flags correctly reflect dependencies
- [ ] Deploy button triggers TaskEngine job and streams logs to UI
- [ ] Global admin dashboard shows workspace summary from admin DB
- [ ] All feature-gated routes return 403 with correct message when feature is disabled
- [ ] `/health` and `/docs` remain accessible (no auth regression)

### Sprint 4 dependencies

- Sprint 1 (repo structure, config, encryption)
- Sprint 2 (WorkspaceService, feature flags)
- Sprint 3 (connector loader — needed for deploy trigger route)

### Sprint 4 estimated effort: 8 person-days

---

## Sprint 5 — Read-only AI Chat + scottylab-app HTTP Connector + migrate-workspaces Integration (Weeks 9–10, 9 person-days)

### Goal

Ship the Phase 2 AI chat with the four read-only tools (`read_file`, `list_files`, `get_drift_report`, `run_health_check`), wire the connector wizard into the workspace creation flow (now that AI chat exists), integrate scottylab-app's HTTP API as the first `ExternalRepoConnector` target, and harden the `migrate-workspaces` CLI for production use.

### Prerequisites for Sprint 5

- scottylab-app Phase 1 MVP is deployed and stable at `https://scottylab.scotty.consulting` (CT 123, port 8102). The RUNBOOK.md describing the scottycore-init.py swap must exist in the scottylab-app repo.
- This is an external dependency. Sprint 5 cannot begin the scottylab-app connector integration until scottylab-app has stabilized post-MVP (defined as: all five Phase 1 service modules deployed, `/health` green for 5 consecutive days, RUNBOOK.md committed).

### Non-goals for Sprint 5

- No write AI chat tools — these are Phase 3 (`commit_file`, `create_branch`, `trigger_deploy`, `rollback_deploy`, `update_app_config`)
- No `scottydev_pending_confirmations` table — Phase 3 only
- No `shared_core`, `sync_watcher`, `promote_scan` features — Phase 3

### Deliverables

**S5.1 — AI chat service backend (2 days)**
File: `/script/scottydev/scottydev/services/ai_chat/service.py`

Implements the chat session lifecycle from v4 Q16:
- `create_session(workspace_slug, user_id) -> WorkspaceChatSession` — creates session row, injects system prompt
- `send_message(session_id, content: str) -> AsyncGenerator[str]` — appends user message, calls AI backend with tool definitions, executes tool calls, streams final response via SSE
- `get_history(session_id) -> list[WorkspaceChatMessage]` — returns last 50 messages (older messages summarised)

System prompt injection on session open includes:
- Workspace name, framework repo URL, current HEAD commit (via Forgejo API if framework_repo_url is set)
- List of registered projects: name, repo URL, last deploy status, FQDN
- Active connector type and health (last known)
- Active AI backend

**S5.2 — Phase 2 AI chat tools (1 day)**
File: `/script/scottydev/scottydev/services/ai_chat/tools.py`

Implement the four read-only tools from v4 Q18:

```python
async def read_file(repo: str, path: str, ref: str = "HEAD") -> str:
    """Read a file from a project or framework repo via Forgejo API.
    repo is the Forgejo repo slug (e.g., 'scotty/scottystrike').
    Returns raw file content as string. 404 → clear error message."""

async def list_files(repo: str, path: str = "/", ref: str = "HEAD") -> list[str]:
    """List directory contents via Forgejo API. Returns list of paths."""

async def get_drift_report(workspace_slug: str) -> dict:
    """Return current pattern drift report for this workspace.
    Reads from workspace DB (pattern_tracking must be enabled).
    If pattern_tracking is False, returns {"status": "pattern_tracking not enabled"}."""

async def run_health_check(project_slug: str) -> HealthResult:
    """Call connector.health_check() for a registered app.
    Uses WorkspaceConnectorLoader to load the workspace's connector."""
```

**S5.3 — AI chat connector wizard implementation (1.5 days)**
File: `/script/scottydev/scottydev/services/ai_chat/wizard.py`

The connector wizard is a specialized AI chat session. When a user starts the connector wizard from the workspace creation flow (Step 3, Sprint 4 placeholder):

Path A — existing automation repo:
1. AI receives the repo URL and clones it to `/var/scottydev/workspaces/<slug>/connector-repo/` (as workspace UID via subprocess)
2. AI calls `list_files(repo_local_path, "/")` to read the directory structure
3. AI calls `read_file(...)` for READMEs, Makefiles, entry scripts
4. AI asks clarifying questions via chat
5. When AI has enough information, it proposes an `ExternalRepoConfig` manifest
6. User approves or asks for changes
7. On approval, `POST /api/v1/workspaces/{slug}/connector/config` is called to save the config

Path B — no existing automation:
1. AI asks the four structured questions about deployment target
2. AI selects a built-in connector and parameterizes it
3. AI proposes the config
4. User approves
5. Config saved via API

The wizard runs `connector.health_check()` before closing to verify the config works.

The wizard is a specific system prompt + tool set that is activated when `wizard_mode=true` is passed to the chat session. It uses the same `ai_chat` service but with a different set of available tools (read-only file tools for repo exploration, plus a `propose_connector_config` tool that triggers the save flow).

**S5.4 — scottylab-app ExternalRepoConnector integration (1.5 days)**
Files: `scottydev/scottydev/services/connectors/external_repo.py` (updates), documentation

This is the first real-world ExternalRepoConnector integration. The workspace for "ScottyLand" will use an `ExternalRepoConnector` pointing at scottylab-app's HTTP API (via the RUNBOOK.md swap path) rather than the scottylab Ansible repo directly.

Concretely, this sprint:
1. Documents how to configure an `ExternalRepoConnector` that calls scottylab-app's REST API for each `ConnectorBase` operation
2. Maps each ConnectorBase op to scottylab-app endpoints per the table in the rebuild brief:
   - `provision_app` → `POST /api/v1/lxc`
   - `deploy_app` → `POST /api/v1/ssh/exec` with docker-compose pull+up targeting LXC IP
   - `configure_dns` → `POST /api/v1/dns/cloudflare/cname` + `POST /api/v1/dns/cloudflare/tunnel-ingress`
   - `configure_tls` → returns 501 (deferred to scottylab-app Phase 2)
   - `destroy_app` → `DELETE /api/v1/lxc/{vmid}` + cleanup
   - `health_check` → `GET /api/v1/lxc/{vmid}/health`
3. The AI wizard, when run for ScottyLand workspace, reads the scottylab-app `/openapi.json` and generates the `ExternalRepoConfig.entry_points` manifest automatically
4. Smoke test: run the wizard for the ScottyLand workspace in a test session, verify it produces a valid manifest, verify `health_check()` returns reachable=True

This integration does NOT replace scottycore-init.py yet. It runs alongside it. The swap to scottylab-app as the authoritative connector is a Phase 3 task.

**S5.5 — AI chat web UI (1 day)**
Files: `scottydev/web/templates/workspace_chat.html`, `scottydev/web/templates/workspace_wizard.html`

- Chat pane accessible from workspace dashboard when `ai_chat=true` (always-on)
- Message bubbles for user/assistant, streaming response via SSE
- Tool call visualization: when AI calls a tool, show "Calling run_health_check for scottylab-app..." in a collapsible block
- Session persistence: chat history loaded from `workspace_chat_messages` on page load
- Provider override dropdown (let user force a specific AI backend for this session)
- Wizard mode: separate page with guided multi-step UI, progressive disclosure of wizard questions

**S5.6 — migrate-workspaces hardening (0.5 day)**
File: `/script/scottydev/scottydev/cli.py`

Harden the `migrate-workspaces` CLI for production use:
- `--dry-run` flag: show which workspaces would be migrated and current/target revision without executing
- `--workspace` flag: migrate only a specific workspace by slug
- Proper connection cleanup on failure (close asyncpg connections)
- Exit code: 0 if all succeed, 1 if any fail, 2 if admin DB is unreachable
- Log output to `data/logs/migrate-workspaces.log` (rotating file handler)

**S5.7 — Chat API routes (0.5 day)**
File: `/script/scottydev/scottydev/api/v1/chat.py`

- `POST /api/v1/workspaces/{slug}/chat/sessions` — create session
- `GET /api/v1/workspaces/{slug}/chat/sessions` — list sessions
- `GET /api/v1/workspaces/{slug}/chat/sessions/{session_id}/messages` — get history
- `POST /api/v1/workspaces/{slug}/chat/sessions/{session_id}/messages` — send message (SSE response)
- `DELETE /api/v1/workspaces/{slug}/chat/sessions/{session_id}` — clear session

All routes require `ai_chat` feature flag (always-on, but gating is present for forward compatibility).

**S5.8 — SSE event stream for chat (0.5 day)**
File: `/script/scottydev/scottydev/api/v1/chat.py`

The chat response endpoint streams using Server-Sent Events. Each SSE event is one of:
- `data: {"type": "token", "content": "..."}` — streamed token
- `data: {"type": "tool_call", "tool": "read_file", "args": {...}}` — tool call in progress
- `data: {"type": "tool_result", "tool": "read_file", "result": "..."}` — tool result
- `data: {"type": "done"}` — response complete

### Sprint 5 acceptance criteria

- [ ] AI chat pane loads in the browser for a workspace with `ai_chat=true`
- [ ] User can ask the AI "What files are in scotty/scottystrike?" and get a response via `list_files`
- [ ] User can ask the AI to run a health check on scottylab-app and get a `HealthResult`
- [ ] Connector wizard runs end-to-end for Path B (no existing automation) and produces a valid built-in connector config
- [ ] scottylab-app ExternalRepoConnector wizard produces a valid `ExternalRepoConfig` manifest that can be saved and tested
- [ ] `migrate-workspaces --dry-run` shows correct output without mutating anything
- [ ] `migrate-workspaces --workspace scotty-land` migrates only that workspace
- [ ] AI chat tools are read-only — there is no `commit_file` tool available in Phase 2
- [ ] Chat history persists across page reloads

### Sprint 5 dependencies

- Sprint 4 (web UI, workspace creation flow, project registry)
- Sprint 3 (connector loader — needed for `run_health_check` tool)
- External: scottylab-app Phase 1 MVP stable (for S5.4 only — S5.1–S5.3 can proceed without it)

### Sprint 5 estimated effort: 9 person-days

---

## Sprint 6 — Integration Hardening + scottycore-patterns Repo + v1 Release (Weeks 11–12, 6 person-days)

### Goal

Complete the scottycore-patterns repo, stabilize the end-to-end ScottyLand workspace flow, implement the remaining gap items from v1 scope, and cut the v1.0.0 release of scottydev. By end of sprint, the ScottyLand workspace is fully operational with real connector, real AI chat, and the scottycore-patterns repo exists with initial content.

### Non-goals for Sprint 6

- No write AI chat tools (Phase 3)
- No Docker connector isolation mode (Phase 3)
- No shared_core/sync_watcher/promote_scan (Phase 3)
- No release_fanout/manager_agents (Phase 3)
- No write UI for scottycore-init.py rewrite into scottydev (the scottydev version already exists from Sprint 3)

### Deliverables

**S6.1 — scottycore-patterns repo (0.5 day)**

Create `scotty/scottycore-patterns` on Forgejo with one file: `patterns.yaml`. Initial content as specified in v4 Q13:
```yaml
version: "1.0.0"
patterns:
  - id: auth.unified_resolver
  - id: auth.session_version
  - id: auth.idle_timeout
  - id: auth.lockout
  - id: auth.totp
  - id: middleware.security_headers
  - id: settings.kv_hierarchy
  - id: audit.immutable_log
  - id: ai_backends.multi_provider
```

This repo is a prerequisite for the `core_upgrade` and `pattern_tracking` features (Phase 3), but creating it now costs 30 minutes and unblocks Phase 3 work.

**S6.2 — End-to-end ScottyLand workspace test (1 day)**

Execute the full workflow on the homelab:
1. Run `python -m scottydev.cli setup-wizard` on the ScottyDev host
2. Start ScottyDev (`uvicorn scottydev.main:app`)
3. Log in, create "scotty-land" workspace
4. Configure DGX Ollama as AI backend
5. Run connector wizard for ScottyLand (Path A: scottylab-app HTTP API as ExternalRepoConnector target)
6. Add 3 test projects to the registry
7. Trigger a deploy via the UI, verify logs stream
8. Open AI chat, ask it to list files in scotty/scottystrike
9. Ask AI to run health check on scottylab-app
10. Verify feature flags render correctly

Document any failures as issues. Fix P1 failures in this sprint; defer P2/P3 to the issue tracker.

**S6.3 — Global admin views (0.5 day)**
Files: `scottydev/web/templates/admin_*.html`

Implement the global admin workspace management views:
- Workspace list with health status from `scottydev_workspace_summary`
- Workspace detail: member management (add/remove workspace members, assign roles)
- Workspace credential rotation UI (`rotate-key` workflow exposed as an admin action)

**S6.4 — Error handling and edge case hardening (1 day)**

Address the common failure modes discovered in S6.2:
- Connector provisioning failure midway: rollback leaves no orphaned OS users or Postgres roles
- AI backend unreachable during wizard: wizard shows clear error, allows backend reconfiguration
- `migrate-workspaces` on a workspace whose DB is unreachable: skips that workspace, continues with others, exits with code 1
- Chat session with no AI backend configured: returns 412 Precondition Failed with `{"detail": "No AI backend configured for this workspace"}`
- Workspace creation with a slug that already exists: 409 Conflict with clear message

**S6.5 — Security review pass (0.5 day)**

Review the following for common issues before v1 release:
- All `*_enc` columns: confirm no plaintext values leak in logs, API responses, or error messages
- Connector runner script: confirm no connector config values appear in subprocess args (must be passed via stdin JSON, not argv)
- Workspace DB connection strings: confirm they never appear in API responses (masked to `postgres://***@host/db`)
- Audit log: confirm all write operations are recorded in `workspace_audit_log`
- RBAC: verify global admin cannot read workspace-internal data without being a workspace member

**S6.6 — Deployment + CLAUDE.md + documentation (0.5 day)**

Deploy ScottyDev to its own LXC on proxmox1.melbourne using the rewritten `scottycore-init.py` in the scottydev repo. Suggested: CT 124, port 8103, FQDN `scottydev.scotty.consulting`.

Write `scottydev/CLAUDE.md` with:
- Live deployment coordinates (CT ID, IP, port, FQDN)
- `.secrets/workspace_encryption_key` location and recovery procedure
- Sudoers snippet needed for connector execution
- `migrate-workspaces` usage
- `rotate-key` usage
- Development workflow (run locally with SQLite for admin DB, skip OS provisioning in dev mode)

**S6.7 — v1.0.0 release preparation (1 day)**

- Bump `scottydev/pyproject.toml` to `version = "1.0.0"`
- Write `docs/release-notes/v1.0.0.md`
- Tag `v1.0.0` on Forgejo and GitHub
- Add scottydev to scottycore's `config/apps.yaml` so the scottycore release pipeline can dispatch upgrade notifications to it

### Sprint 6 acceptance criteria

- [ ] `https://scottydev.scotty.consulting/health` returns 200
- [ ] ScottyLand workspace is configured end-to-end with real connector and AI chat
- [ ] scottycore-patterns repo exists at `https://forgejo.scotty.consulting/scotty/scottycore-patterns`
- [ ] No plaintext secrets in API responses (verified by security review)
- [ ] CLAUDE.md is accurate (deployment coordinates verified against running instance)
- [ ] v1.0.0 tag exists on Forgejo with a release

### Sprint 6 dependencies

- All prior sprints complete
- scottylab-app Phase 1 MVP stable (for S6.2)

### Sprint 6 estimated effort: 6 person-days

---

## Phase 3 and Later (Rough Outline)

### Phase 3 — Write AI Chat Tools + Pattern Discipline (Post-Week 12)

Add the five write tools from v4 Q18 (`commit_file`, `create_branch`, `trigger_deploy`, `rollback_deploy`, `update_app_config`) with the `scottydev_pending_confirmations` table and confirmation UX (10-minute expiry, diff display, Approve/Reject). Enable `shared_core`, `pattern_tracking`, `sync_watcher`, and `promote_scan` for the ScottyLand workspace. Move the `.claude/agents/*-manager.md` files from scottycore into the scottydev repo. Add `CONNECTOR_ISOLATION_MODE=docker` as a functional implementation (Sprint 1 already added the config flag). Retire the deprecated `scottycore-init.py` in the scottycore repo, replacing all references with the scottydev version.

### Phase 4 — Release Fan-out + Manager Agents + Hosted Deployment

Enable `release_fanout` and `manager_agents` feature flags. Wire the scottycore release pipeline to use scottydev's workspace-aware orchestration instead of the current raw GitHub Actions fan-out. Implement the `ai_routing_rules` feature. Add billing primitives and self-service signup to make a hosted ScottyDev deployment architecturally complete (not commercially launched, just architecturally sound).

### Phase 5 — scottylab-app Namespace Claim + Full Stack Retirement

After scottylab-app reaches full feature parity with the Ansible scottylab repo (Phase 3/4 of scottylab-app's own roadmap): rename `scotty/scottylab-app` → `scotty/scottylab`, delete the old Ansible IaC repo, remove the vendored `scripts/scottylab_toolkit/` from scottycore. ScottyDev's connector for ScottyLand switches to scottylab-app's HTTP API as the authoritative connector. The entire scottylab_toolkit import chain in scottycore-init.py is retired.

---

## Risk Register

### Risk 1 — scottystrike active session conflicts with Phase 0 migration

**Likelihood: High. Impact: High.**

scottystrike is the most active repo in the ecosystem (Discover epic + mesh sync + learn-mode). The vendored-to-pip migration requires deleting the `scottycore/` subtree from scottystrike's repo. If the active session has uncommitted changes in that subtree (particularly `scottycore/services/sync/` and `scottycore/services/backup/` which were extracted in v0.1.2), the migration creates a direct conflict.

**Mitigation:**
1. Before starting P0.4, inspect `git status -s /script/scottystrike`. If ANY modified files exist under `scottycore/`, do not proceed.
2. Coordinate with the scottystrike session: ask it to commit all work to Forgejo before Phase 0 begins, or explicitly pause its open changes.
3. If the scottystrike session must continue mid-migration: defer scottystrike's migration to a dedicated maintenance window after the session completes its current task. scottybiz and scottylab-app can be migrated independently.
4. Rollback: `git checkout -- scottycore/` restores the vendored tree in under 5 seconds.
5. Order: always migrate scottystrike last in Phase 0.

### Risk 2 — Per-workspace Postgres provisioning fails on Proxmox LXC

**Likelihood: Medium. Impact: High.**

The `CREATE DATABASE` and `CREATE ROLE` calls in `isolation.provision_workspace_db()` require the ScottyDev process user to hold a Postgres admin role (`scottydev_admin`). On the homelab, the Postgres instance may be configured differently from development assumptions, or the `scottydev` process user may not have been granted the necessary Postgres privileges during initial setup.

**Mitigation:**
1. Test the Postgres provisioning against the homelab early in Sprint 1 (S1.6 acceptance criteria requires integration verification).
2. Document the required Postgres grants in `CLAUDE.md` and the install wizard CLI output.
3. Add a startup health check that verifies `scottydev_admin` can connect and execute `SELECT pg_catalog.pg_database.datname FROM pg_catalog.pg_database LIMIT 1`. If this fails, log a clear error and continue startup (degraded mode: workspace creation is unavailable but existing workspaces function).
4. For the homelab, the simplest mitigation is running the install wizard step `provision-postgres` explicitly before the first workspace is created.

### Risk 3 — Vendored tree removal breaks app imports due to local modifications

**Likelihood: Medium. Impact: Medium.**

scottybiz and scottylab-app both use `packages = ["scottycore"]` — meaning their app code lives INSIDE a `scottycore/` directory, creating a namespace collision. After switching to pip, `import scottycore` resolves to the pip package, not the app's local code. Any app code in `scottycore/` that imports from itself (common pattern: `from scottycore.core.config import settings`) will resolve correctly after migration. However, if any app has modified scottycore framework files locally (not their app-specific code, but changes to the actual framework modules), those modifications will be silently dropped when the vendored tree is deleted.

**Mitigation:**
1. Before migration, run `diff -r /script/scottycore/scottycore/ /script/scottybiz/scottycore/` to identify any local modifications to framework files. If diffs exist, they must be evaluated: either promote to scottycore via `/promote`, or note that the app intentionally diverged.
2. After migration, the app package directory (the app's OWN code, not the framework) must be renamed from `scottycore/` to the app name (e.g., `scottybiz/`, `app/`). This rename touches every `from scottycore...` import — use `sed -i` to bulk-replace, then verify.
3. Run `ruff check` and `pytest` before committing the migration.

### Risk 4 — scottylab-app Phase 1 MVP stability blocks Sprint 5 S5.4

**Likelihood: Medium. Impact: Low (partial).**

Sprint 5's scottylab-app HTTP connector integration (S5.4) depends on scottylab-app Phase 1 MVP being stable. If scottylab-app hasn't reached stability by Week 9 (Sprint 5 start), S5.4 must be deferred but the rest of Sprint 5 can proceed. S5.1 (AI chat), S5.2 (read-only tools), S5.3 (connector wizard), and S5.5 (chat UI) are all independent of scottylab-app.

**Mitigation:**
1. S5.4 is a bounded 1.5-day task that can slip to Sprint 6 without affecting the critical path.
2. Stability gate: scottylab-app must have all five Phase 1 service modules deployed AND `/health` green for 5 consecutive days before S5.4 begins.
3. If S5.4 slips to Sprint 6, the scottydev v1.0.0 release ships without the scottylab-app connector integration but with a documented integration path.

### Risk 5 — Admin DB connection strings to workspace DBs: key loss scenario

**Likelihood: Low. Impact: Critical.**

If `.secrets/workspace_encryption_key` is lost, all workspace DB connection strings (stored as `ws_db_conn_enc` in the admin DB) become unreadable. ScottyDev cannot connect to any workspace DB. All workspace-specific data (projects, chat history, connector configs) is inaccessible.

**Mitigation:**
1. The install wizard displays the key once and explicitly instructs the admin to store it in a password manager. This is v4 Directive 4 behavior — non-negotiable.
2. The `rotate-key` CLI provides a migration path if the admin generates a new key before losing the old one.
3. Backup procedure: the scottydev deploy documentation (CLAUDE.md) must explicitly state "back up `.secrets/workspace_encryption_key` before any system migration or container move."
4. Development mode: in local dev with SQLite, the key is auto-generated on first start and stored in `.secrets/`. Production mode requires the admin to explicitly run the wizard. The distinction is enforced by `app_env` setting.
5. There is no recovery path from total key loss other than reprovisioning all workspaces from scratch. This is the intended behavior per v4 Decision 4 — the admin owns recovery.

---

## Dependency Graph

```
Phase 0 (Vendor-to-pip migration)
  └── P0.1 Serialize with scottystrike session
       └── P0.2 Migrate scottybiz
       └── P0.3 Migrate scottylab-app
       └── P0.4 Migrate scottystrike (serialize after P0.2, P0.3)
       └── P0.5 Fix APPS list

Sprint 1 (repo skeleton + isolation model)
  ├── Requires: Phase 0 complete (scottycore pip-available)
  └── S1.1–S1.9 (all internal dependencies)
       └── Enables: Sprint 2

Sprint 2 (WorkspaceService + encryption + CLI)
  ├── Requires: Sprint 1 complete
  └── Enables: Sprint 3, Sprint 4

Sprint 3 (ConnectorBase + built-in connectors + init rewrite)
  ├── Requires: Sprint 1 (encryption, isolation), Sprint 2 (workspace DB storage)
  └── Enables: Sprint 4 (deploy routes), Sprint 5 (connector wizard, health check tool)

Sprint 4 (Web UI + workspace creation flow)
  ├── Requires: Sprint 2 (WorkspaceService), Sprint 3 (connector loader)
  └── Enables: Sprint 5 (AI chat has a UI to live in)

Sprint 5 (AI chat + scottylab-app connector + migrate-workspaces hardening)
  ├── Requires: Sprint 3 (connector tools), Sprint 4 (UI)
  ├── External: scottylab-app MVP stable (for S5.4 only)
  └── Enables: Sprint 6

Sprint 6 (hardening + scottycore-patterns + v1 release)
  └── Requires: Sprint 5 complete
```

```
Phase 3 (write tools + pattern discipline)
  └── Requires: v1 shipped (Sprint 6 complete)

Phase 4 (release fan-out + hosted deployment)
  └── Requires: Phase 3 complete

Phase 5 (scottylab-app namespace claim + toolkit retirement)
  └── Requires: Phase 4 complete, scottylab-app Phases 3–4 complete
```

---

## Phase 1 Detailed Task Index

All Phase 1 deliverables mapped to sprint tasks above:

| v4 Phase 1 Deliverable | Sprint Task |
|------------------------|-------------|
| Create scottydev repo on Forgejo | S1.1 |
| ConnectorBase interface | S3.1 |
| Built-in connectors: ProxmoxSSHConnector | S3.2 |
| Built-in connectors: DockerSSHConnector | S3.3 |
| Built-in connectors: SystemdConnector | S3.4 |
| ExternalRepoConnector (hand-written stub) | S3.5 |
| scottydev/core/isolation.py | S1.6 |
| scottydev/core/encryption.py | S1.5 |
| Install setup wizard (master key generation) | S1.7 |
| WorkspaceService writing to admin DB + provisioning per-workspace DBs | S2.1 |
| scottycore-init.py rewrite (connector-aware) | S3.9 |
| Vendor-to-pip migration for existing apps | Phase 0 |
| CONNECTOR_ISOLATION_MODE config flag | S1.4 |
| migrate-workspaces CLI stub | S1.7; full implementation S2.4 |
| Per-workspace DB schema | S1.8 |
| WorkspaceFeatures schema + validator | S1.9 |
| scottycore-patterns repo | S6.1 |

---

## Phase 2 Detailed Task Index

All Phase 2 deliverables mapped to sprint tasks:

| v4 Phase 2 Deliverable | Sprint Task |
|------------------------|-------------|
| Launch scottydev as FastAPI app | S1.4, S4.1 |
| Workspace creation flow: login → new workspace → framework repo → AI connector wizard → AI backend config | S4.3 |
| AI backend config order enforcement (before connector wizard) | S4.3 (S4.3 Step 2 → Step 3 gating) |
| Project registry: add apps via UI | S4.4 |
| Deploy helper: trigger deploy via connector, stream logs | S4.6 |
| ai_chat feature: read-only tool set (4 tools) | S5.2 |
| Dashboard with feature-gated sections | S4.2 |
| Global admin workspace list from summary table | S4.9 |
| Connector setup wizard (AI-required) | S5.3 |
| scottylab-app HTTP connector integration | S5.4 |
| migrate-workspaces CLI (fully hardened) | S5.6 |

---

## Phase 1 → Phase 2 Handoff Checklist

The following must ALL be true before Phase 2 work begins (before Sprint 4):

- [ ] `scottydev` repo exists on Forgejo and GitHub with initial commit
- [ ] `uvicorn scottydev.main:app` starts without errors on the homelab
- [ ] `.secrets/workspace_encryption_key` is generated and backed up in password manager
- [ ] Admin DB tables created and Alembic migration `001_admin_tables` applied
- [ ] `WorkspaceService.create()` successfully provisions: OS user, Postgres DB, workspace schema
- [ ] Envelope encryption round-trip verified: encrypt_json → store → decrypt_json returns original data
- [ ] `ProxmoxSSHConnector.health_check()` returns reachable=True for at least one homelab app
- [ ] `migrate-workspaces` CLI can apply workspace schema migrations to two test workspace DBs
- [ ] All three vendored-to-pip migrations complete: scottybiz, scottylab-app, scottystrike
- [ ] scottycore release pipeline (release.yml) creates upgrade PRs in scottybiz and scottystrike after a test scottycore tag push
- [ ] `CONNECTOR_ISOLATION_MODE` config flag exists in `scottydev/core/config.py`
- [ ] Rewritten `scottycore-init.py` in scottydev repo successfully scaffolds a test app end-to-end

## Phase 2 → Phase 3 Handoff Checklist

The following must ALL be true before Phase 3 work begins:

- [ ] ScottyLand workspace fully configured: connector (scottylab-app ExternalRepoConnector), AI backend (DGX Ollama), 3+ projects registered
- [ ] AI chat read-only tools work in browser: `list_files`, `read_file`, `run_health_check`, `get_drift_report`
- [ ] Connector wizard runs end-to-end for both Path A and Path B
- [ ] All four Phase 2 read-only tools covered by unit tests (mocked Forgejo API and connector)
- [ ] `migrate-workspaces` has been run successfully against two real workspace DBs
- [ ] scottydev is deployed to homelab (CT 124 or equivalent) and accessible at `https://scottydev.scotty.consulting`
- [ ] scottycore-patterns repo exists with initial `patterns.yaml`
- [ ] No `commit_file`, `create_branch`, `trigger_deploy`, `rollback_deploy`, `update_app_config` tools exposed in Phase 2 chat (verified by reviewing `tools.py`)
- [ ] `scottydev_pending_confirmations` table does NOT exist in any workspace DB (Phase 3 migration only)
- [ ] scottydev v1.0.0 tagged on Forgejo

---

## Explicit Non-Goals by Phase

### Phase 0 Non-Goals

- NOT migrating scottyscribe (Flask, non-scottycore stack — never in upgrade pipeline)
- NOT migrating ScottyScan/webapp (not in APPS list, separate migration complexity)
- NOT migrating scottysync or scottomation to scottycore (these are independent stacks; they should be REMOVED from APPS, not migrated)
- NOT modifying scottycore-init.py to call scottylab-app HTTP API (Phase 5)
- NOT building any ScottyDev infrastructure in Phase 0

### Sprint 1 Non-Goals

- No web pages beyond `/health`
- No workspace CRUD API (Sprint 2)
- No connectors (Sprint 3)
- No AI chat (Sprint 5)
- No Forgejo workflow installation in scottydev (scottydev's own upgrade pipeline is Phase 6)

### Sprint 2 Non-Goals

- No connector wizard (Sprint 5)
- No workspace creation UI (Sprint 4)
- No AI-assisted anything — this is pure backend

### Sprint 3 Non-Goals

- No AI-assisted connector configuration — that requires the AI chat service (Sprint 5)
- No scottylab-app HTTP connector integration — the app must be stable first (Sprint 5)
- No retirement of the old `scottycore-init.py` — it is deprecated but kept functional

### Sprint 4 Non-Goals

- The connector wizard UI placeholder does NOT run the actual wizard — it shows "coming in next release"
- No chat sessions (Sprint 5)
- No write operations via UI — deploy trigger is the only mutating action, and it invokes the connector which already exists

### Sprint 5 Non-Goals

- No write AI chat tools (`commit_file`, `create_branch`, etc.) — Phase 3
- No `scottydev_pending_confirmations` table or confirmation UX — Phase 3
- No `shared_core`, `sync_watcher`, `promote_scan`, `release_fanout`, `manager_agents` features — Phase 3
- No Docker connector isolation mode — Phase 3
- No retirement of vendored `scottylab_toolkit` — Phase 5

### Sprint 6 Non-Goals

- No write tools — Phase 3
- No billing or self-service signup — Phase 4
- No scottylab-app namespace claim or Ansible IaC retirement — Phase 5

---

## Open Questions

The following items are not specified in v4 and should be resolved before the affected sprint begins:

1. **Development mode for isolation.py**: On a developer's local machine without Proxmox access, `provision_workspace_db()` can use a local Postgres. But `provision_workspace_os()` calls `useradd` which requires root. Should there be a `SCOTTYDEV_DEV_SKIP_OS_PROVISIONING=true` env var that mocks the OS provisioning step? Recommend yes — required for Sprint 1 testing without a homelab.

2. **scottydev process user on the homelab**: v4 specifies the ScottyDev process runs as `scottydev:scottydev`. The LXC provisioned for scottydev needs this user created, the sudoers rule configured, and the `/var/scottydev/` directory structure initialized. These steps should be automated by the install wizard but the exact LXC setup is not specified in v4. Recommend adding a `setup-host` subcommand to the CLI.

3. **scottylab-app SSE subscription for connector health**: v4 Q14 sub-question 4 specifies that workspace health data is replicated via a background event queue. The scottylab-app SSE stream at `/api/v1/events` is the natural source. Should scottydev subscribe to this stream as a persistent background connection, or only poll on-demand? v4 doesn't specify the subscription model. Recommend on-demand poll for Phase 2 (simpler), subscription model for Phase 3.

4. **Workspace slug naming constraints**: v4 doesn't specify allowed characters for workspace slugs. Recommend: lowercase alphanumeric + hyphens, 3–32 characters, must start with a letter. The Linux UID allocation uses the slug in the OS username (`ws-<slug>`), which must be a valid Linux username.

5. **scottydev Forgejo workflow installation**: Should scottydev's own repo have `promote-scan.yml` and `scottycore-upgrade.yml` installed? Since scottydev depends on scottycore as a pip package, it should be in the release APPS list and receive upgrade notifications. This is consistent but wasn't explicitly called out. Recommend: yes, add scottydev to scottycore's release.yml APPS list in Sprint 6.

---

## File Path Reference

Key files created or modified by this plan:

**Phase 0:**
- `/script/scottybiz/pyproject.toml` — add scottycore pip dep, remove vendored packages entry
- `/script/scottylab-app/pyproject.toml` — add scottycore pip dep, rename app package dir
- `/script/scottystrike/pyproject.toml` — add scottycore pip dep, update packages entry
- `/script/scottycore/.forgejo/workflows/release.yml` — remove scottysync, scottomation from APPS

**Sprint 1:**
- `/script/scottydev/pyproject.toml`
- `/script/scottydev/scottydev/core/config.py`
- `/script/scottydev/scottydev/core/database.py`
- `/script/scottydev/scottydev/core/encryption.py`
- `/script/scottydev/scottydev/core/isolation.py`
- `/script/scottydev/scottydev/cli.py`
- `/script/scottydev/scottydev/services/workspaces/models.py`
- `/script/scottydev/scottydev/services/workspaces/ws_models.py`
- `/script/scottydev/scottydev/services/workspaces/schemas.py`
- `/script/scottydev/alembic/versions/001_admin_tables.py`
- `/script/scottydev/alembic/versions/002_workspace_schema.py`
- `/script/scottydev/.gitignore` — includes `.secrets/`
- `/script/scottydev/.secrets/workspace_encryption_key` — generated by wizard, gitignored

**Sprint 2:**
- `/script/scottydev/scottydev/services/workspaces/service.py`
- `/script/scottydev/scottydev/services/workspaces/cleanup.py`
- `/script/scottydev/scottydev/api/v1/workspaces.py`
- `/script/scottydev/scottydev/core/dependencies.py`

**Sprint 3:**
- `/script/scottydev/scottydev/services/connectors/base.py`
- `/script/scottydev/scottydev/services/connectors/proxmox_ssh.py`
- `/script/scottydev/scottydev/services/connectors/docker_ssh.py`
- `/script/scottydev/scottydev/services/connectors/systemd.py`
- `/script/scottydev/scottydev/services/connectors/external_repo.py`
- `/script/scottydev/scottydev/services/connectors/loader.py`
- `/script/scottydev/scottydev/api/v1/connectors.py`
- `/script/scottydev/scripts/scottycore-init.py`
- `/var/scottydev/connector-runner.py` — deployed by install, not in repo package

**Sprint 4:**
- `/script/scottydev/scottydev/web/router.py`
- `/script/scottydev/scottydev/web/templates/base.html`
- `/script/scottydev/scottydev/web/templates/dashboard.html`
- `/script/scottydev/scottydev/web/templates/workspace_new.html`
- `/script/scottydev/scottydev/web/templates/workspace_settings.html`
- `/script/scottydev/scottydev/web/templates/projects.html`
- `/script/scottydev/scottydev/web/templates/admin_workspaces.html`
- `/script/scottydev/scottydev/services/projects/service.py`
- `/script/scottydev/scottydev/api/v1/projects.py`
- `/script/scottydev/scottydev/api/v1/ai_config.py`

**Sprint 5:**
- `/script/scottydev/scottydev/services/ai_chat/service.py`
- `/script/scottydev/scottydev/services/ai_chat/tools.py`
- `/script/scottydev/scottydev/services/ai_chat/wizard.py`
- `/script/scottydev/scottydev/api/v1/chat.py`
- `/script/scottydev/scottydev/web/templates/workspace_chat.html`
- `/script/scottydev/scottydev/web/templates/workspace_wizard.html`

**Sprint 6:**
- `https://forgejo.scotty.consulting/scotty/scottycore-patterns/patterns.yaml` (new repo)
- `/script/scottydev/CLAUDE.md`
- `/script/scottydev/docs/release-notes/v1.0.0.md`

---

Now I'll write this to the file. Wait — I confirmed I only have Read/Grep/Glob tools. I cannot write files. I'll deliver the full content above as my response and note that it should be saved to the path. The system prompt confirms the parent agent reads text output.

However, re-reading the instructions: "Save the plan to `/script/scottycore/docs/scottydev-pivot-plan.md`." I should attempt to fulfill this. Since I don't have a Write tool in this session, I'll deliver the complete plan inline and flag that the file write step needs to be executed by the invoking session.

---

**Inline summary (~400 words):**

**Phase 0 scope.** Three apps require vendored-to-pip migration before any scottycore release has downstream effect: scottybiz (`/script/scottybiz`), scottylab-app (`/script/scottylab-app`), and scottystrike (`/script/scottystrike`). All three carry the scottycore framework tree inside their repos and declare `packages = ["scottycore"]` in their wheel config instead of a `scottycore @ git+https://...` pip dependency. The `scottycore-upgrade.yml` bump step searches for that exact regex, finds nothing, and exits silently — explaining why v0.1.2 release generated zero PRs. Phase 0 also removes scottysync and scottomation from the release APPS dispatch list; they don't use scottycore and their no-op dispatches add noise. Estimated effort: 4 person-days.

**Sprint 1 critical-path item.** The single most important gate is serializing Phase 0's scottystrike migration against the active scottystrike session. The session is mid-Discover epic with mesh sync and learn-mode in flight. If any files under `scottycore/services/sync/` or `scottycore/services/backup/` are uncommitted in scottystrike when Phase 0 runs, the migration will drop those changes. The correct procedure: inspect `git status` on scottystrike, demand the session commit everything to Forgejo, then execute the migration last (scottybiz → scottylab-app → scottystrike).

**Top risk.** scottystrike active session conflict (Risk 1) is both most likely and most consequential. A botched migration loses in-flight work from an active session. The mitigation is pure sequencing: migrate scottystrike last, after all uncommitted work is pushed. If the active session cannot pause, defer scottystrike's migration; the other two apps (scottybiz, scottylab-app) can be migrated without touching scottystrike, and scottycore releases will begin working for those two immediately.

**v1 cut contents (12 weeks, 50 person-days).** ScottyDev v1.0.0 delivers: working pip-upgrade pipeline for all three scottycore-consumer apps; scottydev repo with full workspace isolation (per-workspace Postgres DB + Linux UID + subprocess connector runner), envelope encryption, install wizard, and WorkspaceService; four built-in connectors (ProxmoxSSH, DockerSSH, Systemd, ExternalRepo); rewritten connector-aware scottycore-init.py; web UI with workspace creation flow (enforcing AI backend config before connector wizard), project registry, deploy helper with log streaming, and feature flag management; read-only AI chat with `read_file`, `list_files`, `get_drift_report`, `run_health_check` tools; AI connector wizard for both Path A (existing automation repo) and Path B (guided built-in connector configuration); scottylab-app HTTP API as the first ExternalRepoConnector integration target; hardened `migrate-workspaces` CLI; and scottycore-patterns repo with initial pattern catalog.

What is explicitly NOT in v1: write AI chat tools, `scottydev_pending_confirmations` table, shared_core/sync_watcher/promote_scan/release_fanout/manager_agents feature flags, Docker connector isolation mode, scottylab_toolkit retirement, and any billing or self-service signup.

---
