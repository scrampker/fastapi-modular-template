# Brief: Rebuild ScottyLab as a ScottyCore-based Application

> **For the sister Claude Code session opened in `/script/scottycore`.**
> Read this brief end-to-end before touching anything. The parent session
> (the one that wrote this) is paused pending your MVP delivery.

---

## What this brief is

The parent session and the user agreed to rebuild `/script/scottylab` from a
pile of Ansible playbooks + Terraform + Python toolkit into a scottycore-based
FastAPI application (call it "scottylab-app") before the broader ScottyDev
platform pivot happens. You are the session that does the rebuild.

Your job is to ship a **Phase 1 MVP** — a minimal viable scottylab-app that
covers the operations `scottycore-init.py` currently performs via the vendored
`scottylab_toolkit` package. No web UI. No topology viz. No PBS. No storage
backend management. Just the core REST API surface that replaces the Ansible
YAML-editing + SSH-exec + API-call operations.

---

## Where you are (context)

- You are running in `/script/scottycore`. This repo is the scottycore framework
  (FastAPI template + services). It is ALSO currently the home of
  `scripts/scottycore-init.py` (the homelab provisioning script) and
  `scripts/scottylab_toolkit/` (the vendored infra toolkit). Both of those
  migrate to a new `scottydev` repo later. You don't touch any of that. You
  treat the vendored toolkit as your **reference implementation** of what
  scottylab-app needs to do.
- The user's Scotty app family has six running apps (scottybiz, scottystrike,
  scottysync, scottomation, scottyscribe, scottyscan). All are deployed as
  docker-in-LXC on proxmox1.melbourne, reverse-proxied via nginx.melbourne,
  published via a Cloudflare tunnel. The whole homelab layout is in
  `/script/scottylab/automation/ansible/`.
- `/script/scottylab` itself is the **Ansible IaC source of truth** today. Its
  playbooks declare nginx vhosts, nginx certs, scottycore-apps deployment list,
  and proxmox inventory. The vendored toolkit in scottycore edits those YAMLs
  via stateful idempotent operations. **Leave `/script/scottylab` alone during
  Phase 1.** It remains the authoritative source while your new app is under
  construction.
- Relevant design documents to read:
  - `/script/scottycore/docs/scottydev-pivot-design.md` (v4) — the broader
    ScottyDev platform vision. You are building a piece of what ScottyDev will
    eventually orchestrate. Read Q14 (connectors) and Q15c (AI-required setup
    wizard) in particular.
  - `/script/scottycore/CLAUDE.md` — the scottycore framework reference.
    Explains the service module contract, RBAC model, 4-tier settings,
    audit log conventions, and AI backends.
  - `/script/scottycore/scripts/scottylab_toolkit/` — your Phase 1 starting
    point. Each module maps to a scottylab-app service roughly 1:1. Read all
    of them.

---

## Goal

Deliver `scottylab-app` MVP: a scottycore-based FastAPI app that exposes a
typed REST API for the six infrastructure operations that the vendored toolkit
performs today. Deploy it to its own LXC on the homelab via `scottycore-init.py`.
Verify it runs and serves `/health`. Port enough functionality that the
toolkit could be *replaced* by HTTP calls to scottylab-app (don't do the
replacement yet — that's post-MVP).

The eventual ScottyDev platform will consume this app as a first-party
"infrastructure connector" — see "Forward-planned ScottyDev interface
expectations" below.

---

## Phase 1 MVP scope — non-negotiable, everything else is deferred

Five service modules, in priority order. Implement them in this order; ship
module N only when N-1 passes tests.

### 1. `services/inventory/`
Source of truth for what exists in the homelab.

- SQL-backed (postgres in prod, sqlite in dev — scottycore's existing pattern).
- Alembic migration for initial schema.
- Entities: `ProxmoxNode`, `LXC`, `VM`, `NetworkZone`, `IPAllocation`.
- API: list, get by id/hostname, upsert, soft-delete.
- Seed migration that imports the current state from
  `/script/scottylab/automation/ansible/inventory/workloads.yml`. One-way
  seed at Alembic upgrade time. The app is authoritative after that.
- Audit log on every write (`AuditService.log_data_access` from scottycore).

### 2. `services/proxmox_lxc/`
Provision and destroy LXCs on the homelab proxmox cluster.

- Wrap the logic from `scottylab_toolkit/lxc.py`. Don't copy it — port it to
  scottycore's service pattern (schemas + service + repository + models).
- API: `POST /api/v1/lxc` (provision), `DELETE /api/v1/lxc/{vmid}` (destroy),
  `GET /api/v1/lxc/{vmid}` (status), `GET /api/v1/lxc/{vmid}/health`.
- Emits events on state transitions (see "Event stream requirement" below).
- Reuses `services/inventory` for LXC tracking.

### 3. `services/nginx_vhosts/`
Manage nginx reverse-proxy vhosts + cert apexes.

- Wrap `scottylab_toolkit/nginx.py`. Same porting rules as above.
- API: `POST /api/v1/nginx/vhost`, `DELETE /api/v1/nginx/vhost/{name}`,
  `GET /api/v1/nginx/vhosts`, `POST /api/v1/nginx/cert-apex`,
  `GET /api/v1/nginx/cert-zones` (returns zones in use — required by UniFi
  wildcard sync, Phase 1 keeps that operation outside scottylab-app for now).
- The underlying storage during Phase 1 is STILL the scottylab
  `nginx-vhosts.yml` and `nginx-certs.yml` files; scottylab-app edits those
  files via the same pattern the vendored toolkit uses. This keeps deploys
  working. Phase 2 migrates storage into the app's database and retires the
  YAMLs.

### 4. `services/cloudflare_dns/`
Manage Cloudflare DNS CNAMEs and tunnel ingress.

- Wrap `scottylab_toolkit/cloudflare.py`.
- API: `POST /api/v1/dns/cloudflare/cname`, `POST /api/v1/dns/cloudflare/tunnel-ingress`,
  `DELETE /api/v1/dns/cloudflare/cname/{fqdn}`.
- Credentials (CF token) stored via scottycore's 4-tier settings service as
  a tenant-scope setting. Encrypted. Never logged.

### 5. `services/ssh_exec/`
SSH command execution against homelab hosts (docker compose up, ansible-playbook,
etc.).

- Wrap `scottylab_toolkit/ssh.py` + `scottylab_toolkit/ansible_run.py`.
- API: `POST /api/v1/ssh/exec` (one-shot command), `POST /api/v1/ansible/run`
  (playbook invocation with structured result parsing).
- SSH key path from tenant settings.
- Every exec logged to audit with redacted command (strip secrets in args).

### What's out of scope for Phase 1 (defer all of these)

- UniFi DNS (`scottylab_toolkit/unifi.py`) — implement in Phase 2
- Cert issuance (LetsEncrypt) — Phase 2
- PBS backup management — Phase 3
- Storage backend management (Ceph, ZFS) — Phase 3
- Proxmox VM management (only LXC in Phase 1) — Phase 2
- Visual topology / dashboard / web UI beyond basic CRUD views — Phase 3
- WebSocket live updates (use SSE for Phase 1 — see below) — Phase 3

---

## Non-goals for Phase 1

- **No web UI beyond what scottycore's template gives you for free.** Basic
  `/scottylab/` pages via the existing Jinja2 + HTMX pattern are fine; a
  full topology visualization is Phase 3.
- **No ScottyDev integration.** ScottyDev doesn't exist yet. You are NOT
  building the connector that ScottyDev will consume. You are building the
  API surface that ScottyDev's connector will eventually target. Design with
  that future consumer in mind (see next section), but do not invoke any
  ScottyDev APIs or assume it exists.
- **Do not retire `/script/scottylab`.** The old Ansible repo stays in place,
  functional, and authoritative for YAML files scottylab-app doesn't own yet.
- **Do not modify `scottycore-init.py`** to call scottylab-app's API instead
  of the vendored toolkit. That swap is post-MVP.
- **Do not modify the vendored `scottylab_toolkit/` in scottycore.** You may
  READ it freely for reference; don't edit. It continues to serve
  scottycore-init.py's current consumers.

---

## Starting parameters

| | |
|---|---|
| **App name** | `scottylab-app` (so it doesn't collide with existing `scotty/scottylab` Forgejo repo) |
| **Directory** | `/script/scottylab-app` (new; don't touch `/script/scottylab`) |
| **FQDN** | `scottylab.scotty.consulting` (wildcard cert already covers it) |
| **Port** | `8102` (next free after 8099, 8100, 8101, 8443) |
| **Branch** | `master` |
| **Forgejo repo** | `scotty/scottylab-app` (new, created by init script) |
| **GitHub mirror** | `scrampker/scottylab-app` (new, created by init script) |
| **LXC** | Provisioned by init: new CT # on proxmox1.melbourne |
| **Stack description** | `"FastAPI (Scotty homelab management)"` |

Existing `scotty/scottylab` Forgejo repo and `scrampker/scottylab` GitHub repo
stay as the Ansible IaC repo. They are untouched. When scottylab-app reaches
feature parity post-MVP, the user can rename `scotty/scottylab` →
`scotty/scottylab-classic` and rename `scotty/scottylab-app` → `scotty/scottylab`
to claim the canonical name (same pattern the scottystrike rebuild used; see
session memory).

---

## Migration strategy

Use the same pattern the scottystrike rebuild used earlier today. Specifically:

1. Do NOT commit any changes to `/script/scottylab` in Phase 1. Leave the one
   currently-uncommitted edit (`templates/forgejo-runner.service.j2`) for a
   separate cleanup pass or for the user to deal with.
2. Scaffold `/script/scottylab-app` via the init script:
   ```bash
   python3 /script/scottycore/scripts/scottycore-init.py \
     /script/scottylab-app \
     --scaffold \
     --name scottylab-app \
     --stack "FastAPI (Scotty homelab management)" \
     --domain scottylab.scotty.consulting \
     --port 8102
   ```
   This provisions the LXC, nginx vhost, Cloudflare tunnel ingress, UniFi
   wildcard entry, and deploys a stock scottycore container that returns 200
   at `/health`. **Verify the health endpoint before writing any domain code.**
3. Port the five service modules in priority order (inventory → proxmox_lxc →
   nginx_vhosts → cloudflare_dns → ssh_exec). After each module lands:
   - Run scottycore's test suite + your new tests (80%+ coverage — required by
     `tdd-guide` agent pattern).
   - Redeploy to CT: `ssh <ct-ip> "cd /opt/scottycore/scottylab-app && git pull && docker compose up -d --build"`
   - Verify the public endpoints via curl.
4. When all five modules are green, write a **runbook**: `docs/RUNBOOK.md` in
   scottylab-app describing how scottycore-init.py could be modified to call
   scottylab-app's HTTP API instead of the vendored toolkit. **Don't make the
   swap — just document the path.** This is the handoff to the ScottyDev
   planner.

### Known gotchas from today's session

- The init script force-pushes on first onboard. Pre-create the GitHub repo is
  NOT needed because `scrampker/scottylab-app` doesn't exist (confirmed: `gh api
  repos/scrampker/scottylab-app` returns 404). The init script's rename-alias
  detection patch (commit b679d09) handles edge cases.
- The init script's nginx dedup keys on `name` — if the scaffold succeeds but
  the nginx vhost doesn't appear for `scottylab.scotty.consulting`, check the
  `name` field collision pattern from the scottomation onboarding earlier.
- Health endpoint convention: scottycore template exposes `/health`. Keep it.
  Don't move to `/api/health` (that's what scottysync did and it broke the
  top-level deploy check).

---

## API contracts — all endpoints must follow these rules

1. **Typed I/O**. Every endpoint takes and returns Pydantic schemas. No
   free-form dicts.
2. **OpenAPI-emitted**. FastAPI does this automatically — don't disable it.
   ScottyDev will introspect `/openapi.json` to auto-generate connector
   bindings later.
3. **Consistent envelope**. For collection endpoints, use scottycore's
   `PaginatedResponse[T]` from `scottycore/core/schemas.py`.
4. **Idempotent writes** where possible. A second `POST /nginx/vhost` with the
   same `name` should return 200 with the existing resource, not 409. The
   vendored toolkit is already mostly idempotent; preserve that property.
5. **RBAC on every endpoint**. Use scottycore's `require_role(RoleName.ADMIN)`
   for writes, `RoleName.VIEWER` for reads. Infrastructure ops default to
   admin-only.
6. **Audit everything**. Every write calls `AuditService.log_data_access`.
   Every read of sensitive data too.
7. **Errors are sanitized**. Use scottycore's `NotFoundError`, `ForbiddenError`,
   etc. Never leak internal paths, SSH command args, or credentials in error
   responses.

---

## Forward-planned ScottyDev interface expectations

ScottyDev's `ConnectorBase` (see `docs/scottydev-pivot-design.md` Q14) has
these six operations. Your API surface must make each operation straightforward
to implement by a ScottyDev connector that talks HTTP to scottylab-app:

| ConnectorBase op | scottylab-app endpoints that satisfy it |
|---|---|
| `provision(workspace, app_spec) -> resource_id` | `POST /api/v1/lxc` → returns `{vmid, ip, status}` |
| `deploy(workspace, resource_id, git_ref) -> deployment_id` | `POST /api/v1/ssh/exec` with docker-compose pull+up command targeting the LXC IP |
| `configure_dns(workspace, fqdn, target) -> record_id` | `POST /api/v1/dns/cloudflare/cname` + `POST /api/v1/dns/cloudflare/tunnel-ingress` |
| `configure_tls(workspace, fqdn, apex) -> cert_id` | Out of Phase 1 scope — returns 501 with "Deferred to Phase 2" for now |
| `destroy(workspace, resource_id) -> None` | `DELETE /api/v1/lxc/{vmid}` + cleanup of DNS/nginx |
| `health(resource_id) -> HealthStatus` | `GET /api/v1/lxc/{vmid}/health` |

**Design principle: every ConnectorBase operation should map to ONE or a small
number of scottylab-app API calls.** If you find yourself adding endpoints that
don't serve a ConnectorBase op AND don't serve the user's direct UI needs,
push back — the scope is too broad.

---

## Event stream requirement

One endpoint, one protocol, one event schema:

- `GET /api/v1/events` — Server-Sent Events stream (not WebSocket in Phase 1).
- Every resource state transition publishes one event.
- Event schema: `{"id": uuid, "timestamp": iso8601, "resource_type": str,
  "resource_id": str, "operation": str, "state": str, "workspace_id": int}`.
- ScottyDev will subscribe to this stream to maintain its own cached view of
  scottylab-app resource state.
- Authentication on SSE stream: same as REST endpoints (use scottycore's
  `get_current_user` dependency).

Pub/sub backend: in-process `asyncio.Queue` fan-out is fine for Phase 1 (single
scottylab-app instance). Don't add Redis. Don't add Celery. Don't add Kafka.

---

## What to preserve from `/script/scottylab`

- **The Ansible playbooks stay put and functional.** During Phase 1, they are
  still the authoritative source for deployment workflows the app doesn't own
  yet (cert issuance, UniFi wildcard sync, runner installs, PBS backups, etc.).
  Don't delete them. Don't move them.
- **The vendored `scottylab_toolkit` in scottycore is your reference.** Read it
  when porting. Port function-by-function to scottycore service modules. Don't
  just import-and-wrap — the service pattern requires typed schemas, repository
  separation, and audit integration that the toolkit doesn't have.
- **Preserve existing nginx-vhosts.yml file format during Phase 1.** The
  scottylab-app's `nginx_vhosts` service edits that YAML file directly (as the
  toolkit does today) so deploys continue working. Phase 2 migrates to DB
  storage + nginx-config-rendering-from-DB.

---

## Pre-flight checklist for you (sister session)

Before running the init script:

- [ ] You've read this entire brief
- [ ] You've read `/script/scottycore/docs/scottydev-pivot-design.md` v4,
      sections Q1, Q2, Q3, Q14, Q15c
- [ ] You've read `/script/scottycore/CLAUDE.md` fully
- [ ] You've browsed `/script/scottycore/scripts/scottylab_toolkit/` and
      understand what each module does
- [ ] You've confirmed `/script/scottylab-app` does NOT exist yet
- [ ] You've confirmed `scotty/scottylab-app` does NOT exist on Forgejo
      (`curl -s -H "Authorization: token $(cat ~/.config/forgejo-token)" https://forgejo.scotty.consulting/api/v1/repos/scotty/scottylab-app` returns 404)
- [ ] You've confirmed `scrampker/scottylab-app` does NOT exist on GitHub
      (`gh api repos/scrampker/scottylab-app` returns 404)
- [ ] You've confirmed port 8102 is not in use:
      `grep 'port: 8102' /script/scottycore/config/apps.yaml` returns empty

If any of those checks fail, stop and ask the user.

---

## Kickoff command (run exactly this)

```bash
cd /script/scottycore
python3 scripts/scottycore-init.py \
  /script/scottylab-app \
  --scaffold \
  --name scottylab-app \
  --stack "FastAPI (Scotty homelab management)" \
  --domain scottylab.scotty.consulting \
  --port 8102
```

After the scaffold finishes, verify:
```bash
curl -sSf https://scottylab.scotty.consulting/health | python3 -m json.tool
```

Then begin Phase 1 implementation in priority order.

---

## Success criteria for Phase 1 MVP

All five must be true:

1. `https://scottylab.scotty.consulting/health` returns `{"status": "ok", ...}`.
2. All five Phase 1 service modules ship with 80%+ test coverage.
3. OpenAPI docs at `/docs` show every endpoint from the "Forward-planned
   ScottyDev interface expectations" table above, with typed request/response
   schemas.
4. `GET /api/v1/events` SSE stream emits at least one event when you
   `POST /api/v1/lxc` via curl (end-to-end smoke test).
5. `docs/RUNBOOK.md` exists in scottylab-app and documents the path to replace
   scottycore-init.py's vendored-toolkit calls with HTTP calls to scottylab-app.

---

## Phase 2+ roadmap (context only — do not build in Phase 1)

| Phase | Scope | Dependencies |
|---|---|---|
| 2a | UniFi DNS service, LetsEncrypt cert service | Phase 1 complete |
| 2b | Migrate nginx-vhosts.yml + nginx-certs.yml storage from YAML files to DB; render configs on-demand | Phase 1 complete; Ansible playbook updates |
| 2c | Proxmox VM management (beyond LXC) | Phase 1 complete |
| 3a | Topology visualization (Proxmox nodes + LXCs + network zones) | Phase 2 complete |
| 3b | PBS backup management | Phase 2 complete |
| 3c | Storage backend (Ceph, ZFS) visibility + management | Phase 2 complete |
| 3d | WebSocket live updates (upgrade from SSE) | Phase 2 complete |
| 4 | Rename canonical repo: `scotty/scottylab` → `scotty/scottylab-classic`, `scotty/scottylab-app` → `scotty/scottylab`; rename `/script/scottylab-app` → `/script/scottylab`; retire old Ansible repo | All of Phase 3 complete + user green-light |
| 5 | ScottyDev integration: `scottycore-init.py` modified to call scottylab-app HTTP API instead of vendored toolkit; vendored `scripts/scottylab_toolkit/` deleted | Phase 4 complete + ScottyDev platform shipped |

---

## When you're done with Phase 1 MVP

1. Commit the `docs/RUNBOOK.md` with the swap path.
2. Post a comment on FJ scotty/scottycore#2 (the existing tracking issue for
   this work) linking the scottylab-app repo and noting MVP completion.
3. Ask the user to review the MVP. The parent session resumes after they
   approve.

---

## Things you will be tempted to do but should NOT do

- Don't rewrite `scottycore-init.py` to use your new HTTP API. That's Phase 5.
- Don't rename `/script/scottylab` to `-old` or move it. That's Phase 4.
- Don't touch the vendored `scripts/scottylab_toolkit/` in scottycore. Read-only.
- Don't start the dashboard or topology viz. That's Phase 3.
- Don't add UniFi support in Phase 1. That's Phase 2a.
- Don't worry about ScottyDev. That's a separate planning thread.
- Don't ship a WebSocket gateway. SSE is fine for Phase 1.
- Don't add Redis, Kafka, Celery, or any message queue. The in-process
  `asyncio.Queue` is enough.
- Don't try to make scottylab-app work without the Ansible playbooks under
  `/script/scottylab/`. During Phase 1, your app is explicitly a partial
  replacement — the Ansible layer still covers what your app doesn't.

---

Good luck. The parent session is tracking FJ scotty/scottycore#2 for this work
and is paused until your MVP lands or you hit a blocker worth escalating.
