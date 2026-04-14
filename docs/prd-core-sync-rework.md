# PRD: ScottyCore Sync Rework — Package + Forgejo + Autonomous Review

**Status:** Draft
**Date:** 2026-04-13
**Owner:** Steven

## Problem

The current "template-as-suggestion" model (inline pattern markers, `.scottycore-patterns.yaml` manifests, a central `sync-watcher.py` producing drift reports, a `core-sync` agent porting fixes into each app) has produced drift. Apps fall behind scottycore and stay there because there's no bounded, mechanical moment that forces "look at what's new and decide."

Root cause: three (now five) independent repos expected to stay in sync via out-of-band watchers, without versioning, without a forcing function. This is the worst of both worlds — neither monorepo atomicity nor package-based versioning.

## Goal

Replace the sync-watcher model with a package-based distribution model, on Forgejo, with autonomous per-app upgrade review. Maximize reused code; minimize drift; keep each app independently maintainable.

## Non-goals

- Monorepo consolidation (apps stay as separate repos — user explicitly wants independent maintenance).
- Forcing every app onto the same scottycore version at the same time.
- Keeping GitHub as active CI target (GitHub becomes passive mirror only).

## End state

### Distribution

- **ScottyCore is a pip-installable Python package.** Released as semver tags (`v1.x.x`) on Forgejo.
- Apps pin it in `pyproject.toml`:
  ```toml
  dependencies = ["scottycore @ git+https://forgejo.scotty.consulting/scotty/scottycore.git@v0.2.0"]
  ```
- Apps `from scottycore.auth import AuthService` as normal Python imports.
- SSH deploy keys on runners and dev machines for auth; no URL tokens.

### Repos (Forgejo primary, GitHub mirror)

Forgejo host: **`https://forgejo.scotty.consulting`** (user: `scotty`). Token at `~/.config/forgejo-token` on dev/runner hosts. Dual-remote setup already wired by `scripts/scottycore-init.py` (origin fetch = Forgejo; origin push = Forgejo + GitHub).

1. `scottycore` — the framework package
2. `scottystrike` — app
3. `scottyscribe` — app
4. `scottyscan` — app (webapp only; agent stays separate)
5. `scottomation` — app
6. `scottysync` — app

No GitHub Actions; all CI on Forgejo.

### Release flow (scottycore side)

On tag push `v1.x.x`:

1. **Build** — `python -m build` produces wheel + sdist as pipeline artifacts.
2. **Changelog** — conventional-commits-based generator emits structured release notes with sections:
   - `## Breaking Changes` (with migration notes per item)
   - `## New Features` (with adoption-guide snippet: before/after, when-to-use)
   - `## New Patterns` (pattern name, what it does, adoption recommendation)
   - `## Security` (advisories and required actions)
   - `## Fixes` (bug fixes that may affect apps)
   - `## Internal` (apps can ignore)
3. **Dispatch** — fans out a Forgejo Actions workflow-dispatch to each of the 5 app repos with payload: `{ version, changelog_markdown, breaking_changes_json }`.

### Per-app bump flow (runs in each app repo)

On receiving the dispatch:

1. **Open bump PR** with:
   - `pyproject.toml` version bump
   - `SCOTTYCORE_UPGRADE_NOTES.md` at repo root with the changelog
2. **CI runs** against new version (tests, type-check, lint, the app's existing gates).
3. **Manager agent runs** on a Forgejo runner with Claude Code installed:
   - Reads changelog + CI result
   - Greps the app for call sites affected by breaking changes
   - Evaluates each new feature against existing code ("this app does X manually at these locations; the new feature would replace it")
   - Checks pattern relevance against app domain
   - Classifies the PR: **Green**, **Yellow**, **Red**
4. **Classification actions:**
   - **Green** (CI pass, no breaking changes touching this app, no high-value adoption opportunities missed) → agent auto-merges the bump PR. No notification.
   - **Yellow** (CI pass, but there's a clear adoption opportunity or a medium-impact change) → agent auto-merges the bump PR, opens a follow-up issue with the adoption proposal. No notification.
   - **Red** (CI fails, breaking change touches this app's code, ambiguous semantic call, or security advisory requiring action) → agent leaves PR open, posts analysis comment, fires Hubitat alert via existing `notify_hubitat()` helper (config at `~/.config/scottycore-hubitat.json`, priority P2 for Red, message includes PR URL).
5. **User review on Red** — user opens a Claude Code session in the app repo, says "review PR #N", the manager agent walks them through its analysis and proposed resolution.

### Retired

- `scripts/sync-watcher.py`
- `data/drift-reports/`
- `.scottycore-patterns.yaml` manifests (distribution is now "you depend on this version or you don't" — no per-pattern opt-in needed)
- Inline `# scottycore-pattern:` and `# scottycore-synced-from:` markers
- `core-sync` agent
- Weekly cron drift report

What survives: per-app manager agents (`scottystrike-manager`, etc.), but with a new responsibility (upgrade PR review) replacing the old one (ingesting drift reports).

## Design decisions

### Why git+https URL pins and not a private PyPI registry

Forgejo has a PyPI-compatible package registry built in, and it's strictly better at scale. For 5 apps pulling one library, `git+https` with version tags + the existing `~/.config/forgejo-token` is zero extra infra and works identically from pip's perspective. Migration path: swap the dependency URL later; app code unchanged.

### Why classification and not "always notify"

The whole point of autonomous review is to not interrupt the user for green and yellow upgrades. If every PR pings Hubitat, the system is just the old watcher with extra steps. The classifier threshold lives in the manager agent's prompt and is tuned based on which Red alerts were actually worth the user's attention.

### Why follow-up issues for Yellow adoptions and not in-PR changes

Bundling "bump scottycore 1.4 → 1.5" with "refactor 8 files to use the new `ai_backends` pattern" creates an unreviewable PR. The bump PR stays small and mechanical; adoption is a separate, scoped PR the agent opens later (or opens as an issue for the user to trigger).

### ScottyScan's two components

Only `webapp/` is in scope for scottycore adoption. The `agent/` is a separate deployable (thin remote polling service) and stays out of the sync pipeline for now. Future option: a companion `scottycore-remote` package for thin remote agents (logging, config, heartbeat, request helpers) — deferred until a second remote-agent use case appears.

### Changelog authoring

Conventional commits (`feat:`, `fix:`, `feat!:` for breaking, etc.) drive section routing. For features/patterns that need adoption guidance beyond the commit message, the release-cutting workflow reads `docs/release-notes/v1.x.x.md` if present and appends it to the auto-generated changelog. This lets the scottycore maintainer (you) write rich adoption guides when the change warrants it.

### Agent runner

Forgejo Actions runner on a homelab box with Claude Code installed and `claude -p` authenticated. Runner has SSH deploy keys for all repos. Secret management via Forgejo Actions secrets.

## Phased implementation plan

### Phase 0 — Infrastructure (mostly already in place)

Confirmed existing:
- Forgejo at `https://forgejo.scotty.consulting` (user `scotty`), token at `~/.config/forgejo-token`
- Forgejo Actions runner on **CT118 on proxmox1.melbourne**
- Dual-remote wiring (origin → Forgejo + GitHub) already handled by `scottycore-init.py`
- Hubitat alert helper (`notify_hubitat()` in `sync-watcher.py`), config at `~/.config/scottycore-hubitat.json`

Remaining work:
- [ ] Verify CT118 has `claude -p` authenticated under the runner user (or add it)
- [ ] Confirm CT118 has Python 3.10+, pip, git, build tooling for `python -m build`
- [ ] Confirm runner has access to `~/.config/forgejo-token` and `~/.config/scottycore-hubitat.json` (or equivalent via Forgejo Actions secrets)
- [ ] Lift the `notify_hubitat()` helper out of `sync-watcher.py` into a reusable script the runner can call (it will outlive the watcher)

### Phase 1 — ScottyCore as a package

- [ ] Audit `app/` — confirm it's import-safe as a library (no side effects on import, no implicit `main` assumptions)
- [ ] Restructure: `app/` → `src/scottycore/` (or configure `pyproject.toml` to expose `app` as `scottycore`)
- [ ] `pyproject.toml`: package metadata, version field, `build-system`, declare public API surface via `__init__.py` re-exports
- [ ] Conventional-commits changelog tooling (git-cliff or similar) + `docs/release-notes/` convention
- [ ] Forgejo Actions workflow: on tag → build → generate changelog → dispatch to each app repo
- [ ] Cut `v0.1.0` from current main (pre-1.0; signals API may shift during migration)

### Phase 2 — Pilot migration (pick one app)

Recommend **scottystrike** — newest, cleanest adoption, user is already familiar.

- [ ] Replace vendored/inline scottycore code with `scottycore @ git+https://forgejo.scotty.consulting/scotty/scottycore.git@v0.1.0` dependency
- [ ] Delete app's copy of shared services; import from `scottycore`
- [ ] CI passes on Forgejo
- [ ] `scottystrike-manager` agent updated with upgrade-PR review prompt (classifier logic, Hubitat call, grep-based breaking-change analysis)
- [ ] Forgejo Action in app repo: receives dispatch, opens bump PR, runs agent, classifies
- [ ] End-to-end test: cut a `v0.1.1` on scottycore with a trivial change, verify scottystrike gets a Green PR and auto-merges

### Phase 3 — Roll to remaining apps

In order of simplicity: scottyscribe, scottomation, scottysync, scottyscan (webapp only; agent stays separate).

For each:
- [ ] Migrate to package dependency
- [ ] Update manager agent
- [ ] Wire Forgejo Action
- [ ] Cut a test release on scottycore; verify flow

### Phase 4 — Retire old machinery

- [ ] Delete `scripts/sync-watcher.py` and weekly cron
- [ ] Delete `data/drift-reports/`
- [ ] Delete `core-sync` agent
- [ ] Strip inline `# scottycore-pattern:` and `# scottycore-synced-from:` markers from scottycore source
- [ ] Delete `.scottycore-patterns.yaml` from each app
- [ ] Update scottycore `CLAUDE.md`: new flow, new roster, new agent responsibilities
- [ ] Update `scottycore-init.py` to generate apps wired for the package model

### Phase 5 — Refinement

- [ ] Tune classifier thresholds after ~5-10 real upgrades
- [ ] Add metrics: merge rate by color, time-from-tag-to-merge per app, alert frequency
- [ ] If Forgejo PyPI registry proves useful, migrate from git+ssh to registry URLs

## Resolved design decisions

- **Forgejo host:** `https://forgejo.scotty.consulting` (user `scotty`), already in use
- **Runner:** CT118 on `proxmox1.melbourne`, already stood up
- **Hubitat:** existing `notify_hubitat()` helper + `~/.config/scottycore-hubitat.json`; P2 for Red alerts
- **Package name:** rename `app/` → `scottycore/` in Phase 1
- **Version seed:** `v0.1.0` (pre-1.0 during migration; bump to `v1.0.0` when all 5 apps are on the package model and the API is stable)
- **Per-app agent briefing:** dedicated session per app after Phase 2 pilot proves out

## Success criteria

- Cutting a scottycore release automatically produces a PR in every app within 5 minutes
- 80%+ of PRs merge without user intervention (Green/Yellow)
- User receives at most 1 Hubitat alert per scottycore release (Red only)
- No app is more than 2 scottycore minor versions behind after 1 month
- `sync-watcher.py` and associated machinery are gone
