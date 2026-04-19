# Scottycore Compliance Contract

Every app in a scottycore-based fleet MUST satisfy the six requirements
below. Think of this as SOC2 for the Corpaholics stack: uniform auth,
audit, secrets, RBAC, TOTP, and backup across every service so the
trust boundary lives at a single known surface.

A fleet is only as trustworthy as its least-compliant app. An app that
bypasses scottycore's primitives in favour of its own auth or
plaintext-YAML secrets creates a seam where attackers, bugs, or audit
gaps slip through.

## The six requirements

### 1. Authentication

✅ JWT-backed user sessions via `scottycore.services.auth.service.AuthService`
✅ TOTP enforced for admin roles via `scottycore.core.middleware.register_totp_enforcement`
✅ Optional header-based trust for Cloudflare Zero Trust / Azure AD SSO via `settings.trusted_identity_providers`

❌ **Forbidden**: bespoke session cookies, shared API keys as the sole auth path, anonymous endpoints that mutate state

**Exception path**: machine-to-machine integrations MAY use scottycore's API-key mechanism (`AuthService.issue_api_key`) — but keys are per-user, scoped, revocable, and logged.

### 2. Audit

✅ Every state-changing operation writes an `audit_log` row via `scottycore.services.audit.service.AuditService`
✅ Audit records capture `tenant_id`, `user_id`, `action`, `target_type`, `target_id`, `detail`, `ip_address`, `timestamp`

❌ **Forbidden**: silent mutations, print()-based audit trails, logs-only audit, audit scoped to a single endpoint

**Coverage bar**: if an action would trigger a question in a compliance review ("who did X, when, from where?"), there must be an audit row for it.

### 3. Secrets

✅ API keys, tokens, OAuth refresh tokens, passwords, and credentials MUST live in `scottycore.services.secrets` (encrypted at rest, access-audited)
✅ `.env` files carry ONLY bootstrap values (`JWT_SECRET_KEY`, `DATABASE_URL`, `INIT_ADMIN_PASSWORD` for first-boot, brand config)

❌ **Forbidden**: plaintext tokens in YAML configs, hardcoded credentials in source, secrets in `settings` table (that table is not encrypted)

**Migration path**: existing apps with YAML-plaintext secrets (scottomation, scottysync) must move those secrets into the secrets service before they're considered compliant.

### 4. RBAC

✅ Every protected endpoint checks role membership via `scottycore.services.users.service.UsersService`
✅ Roles: `viewer`, `analyst`, `admin`, `superadmin` — shared vocabulary across apps
✅ Per-tenant role assignments enforce multi-tenancy isolation

❌ **Forbidden**: ad-hoc `if user.is_admin` checks, per-app role models, cross-tenant data leaks in shared endpoints

### 5. TOTP

✅ Enforced for all `admin` and `superadmin` roles
✅ Backup codes available for account recovery
✅ Rotation via `AuthService.rotate_totp_secret`

❌ **Forbidden**: password-only admin sessions, TOTP-optional for privileged roles, bypass flags in production

### 6. Backup

✅ Every app registers a `BackupContributor` in the `ServiceRegistry` so its state is included in fleet-wide backups
✅ Domain data (beyond scottycore's base tenants/users/settings) is serialisable via the contributor
✅ Supports all sinks: `local_disk`, `scottydev`, `remote_node`, `download`, `git_repo`
✅ Encrypted bundles via passphrase + GPG

❌ **Forbidden**: "my app doesn't need backup", standalone backup scripts that don't feed the fleet pipeline, unversioned backup formats

## Compliance levels

### Level 0 — non-compliant
No scottycore pin. App runs its own auth, audit, secrets, RBAC.
**Example**: scottyscribe (Flask, session auth, raw sqlite3).
**Action**: not allowed in production for SOC2-critical data flows.

### Level 1 — packaged
scottycore is pinned but the app uses its own primitives.
**Action**: not allowed — equivalent to Level 0 from a trust perspective.

### Level 2 — adopted
scottycore auth + audit + secrets + RBAC + TOTP are wired through every endpoint. Backup contributor registered.
**Action**: compliant. Passes fleet audit.

### Level 3 — uniform
Level 2 + uses scottycore's `ServiceRegistry` exclusively for domain services, no domain-specific duplication of scottycore functionality.
**Action**: exemplary.

## Audit checklist

For any app claiming compliance, verify:

- [ ] `pyproject.toml` pins `scottycore @ git+...@vX.Y.Z`
- [ ] No vendored `scottycore/` tree in the repo
- [ ] All routes go through FastAPI dependencies that call `AuthService.require_user()`
- [ ] Protected routes declare a role requirement via `Depends(require_role(...))`
- [ ] All mutations include `await audit.log(action=..., target=...)` calls
- [ ] No secrets in YAML, `.env` (beyond bootstrap), or source
- [ ] Admin users have TOTP enrolled (query `users.totp_enabled = True` for role `admin`+)
- [ ] `register_backup_contributor()` is called during app startup
- [ ] A scottycore-backup export produces a bundle including the app's domain data

## Current fleet status

| App | Level | Notes |
|---|---|---|
| scottydev | 2 | ✅ Compliant |
| scottybiz | 2 | ✅ Compliant |
| scottylab-app | 2 | ✅ Compliant |
| scottystrike | 2 | ✅ Compliant |
| scottyscan | 0 | ⏳ Migration in progress — vendored scottycore to delete, customers→tenants rename |
| scottyscribe | 0 | ⏳ Flask port pending |
| scottomation | 0 | ⏳ Anonymous-mode app, full migration pending |
| scottysync | 0 | ⏳ Shared-API-key app, full migration pending |

Target: all apps at Level 2 within the next few sprints.
