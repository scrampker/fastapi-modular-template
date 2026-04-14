# PRD: `/promote` skill + commit-time extraction detection

**Status:** Draft — addendum to [prd-core-sync-rework.md](./prd-core-sync-rework.md)
**Date:** 2026-04-14
**Precondition:** Phase 2 pilot proven end-to-end (scottystrike auto-merges scottycore upgrades, verified with v0.1.1 → PR #4 GREEN → merged in 32s)

## Problem

The Phase 1–2 pipeline handles the **downward** direction (scottycore cuts release → all apps auto-upgrade). The **upward** direction (work done in an app that belongs in scottycore) is still manual: I have to remember to port code, switch repos, cut a release, then come back. That's exactly the friction that made the old sync-watcher model drift in the first place.

We do NOT want to automate upward sync by removing version isolation (editable installs, shared in-place checkouts) — that breaks the discipline that makes Phase 1–2 work. What we want is a **smart, version-respecting promotion workflow** that feels instant while staying on rails.

## Goal

A `/promote` skill invokable from any app session. Given a file/function/module in the current app, it:

1. Extracts the code into scottycore (names stripped, API cleaned)
2. Cuts a new scottycore release (auto-bumped version from the conventional-commits type)
3. Waits for the wheel to publish (~30s)
4. Updates the current app's dependency pin, reinstalls, rewrites the app's local usage to import from scottycore, runs the app's tests
5. Reports green/red; on green, user commits the app-side change

Round-trip target: ~60 seconds of wall clock. Zero context switching. Full version isolation preserved (other devs/apps/CI see only the published semver tag).

## Non-goals

- Editable installs (`pip install -e`) — deliberately rejected. Version isolation is a feature.
- Auto-detect-and-promote without human confirmation. `/promote` is user-triggered (with commit-time nudges, not auto-execution).
- Draft/incubation branches. Always-release keeps the model simple; add a `--draft` flag later only if tag churn becomes painful.

## Design

### Where the skill lives

`/script/scottycore/.claude/skills/promote/SKILL.md` — Claude Code skills are shared across all apps via the global `.claude/` load path, so every app session can invoke `/promote` without per-app setup.

### Invocation

```
/promote <path>
```

`<path>` is relative to the current app repo. Can be a file, a directory, or `file.py::function_name` for finer-grained extraction.

### Flow

1. **Analyze the target.** Agent reads the file + adjacent context. Identifies:
   - App-specific names, imports, configuration references
   - Generic logic that can lift cleanly
   - Test files to port alongside the main code
   - Any dependencies not already in scottycore's `pyproject.toml`

2. **Propose extraction.** Agent shows two diffs:
   - **scottycore side:** new file(s) with names stripped, imports fixed, a test file, updated `__init__.py` re-exports if applicable
   - **app side:** the local file rewritten to `from scottycore.X import Y` (or deleted entirely if fully replaced)

   User confirms (`y`) or cancels.

3. **Propose version bump.** Agent determines semver bump from commit type:
   - `feat:` or `feat(...)` → minor bump (`v0.1.1` → `v0.2.0`)
   - `fix:` → patch (`v0.1.1` → `v0.1.2`)
   - `feat!:` or `BREAKING CHANGE` → major (`v0.1.1` → `v1.0.0` in this case the first major)
   - `perf:`, `refactor:` → patch
   - Extraction defaults to `feat:` (it's a new API in scottycore from scottycore's POV)

   Show proposed tag, user confirms.

4. **Commit + release scottycore.**
   - Commit with the `feat:` message on scottycore master
   - Push to Forgejo
   - Create + push tag (`git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`)
   - The existing release.yml fires automatically

5. **Wait for publish.** Poll Forgejo releases API until `vX.Y.Z` has an attached `scottycore-X.Y.Z-py3-none-any.whl` asset. Timeout 5 minutes, fail loud.

6. **App-side update.**
   - Rewrite `pyproject.toml` pin to the new tag
   - `pip install --upgrade scottycore @ git+https://forgejo.scotty.consulting/scotty/scottycore.git@vX.Y.Z` (or equivalent)
   - Apply the app-side diff proposed in step 2
   - Run the app's test suite (`pytest` or whatever the app uses)

7. **Report.**
   - Green: summary of what was extracted, new scottycore version, app-side changes ready to commit. User does the commit themselves (the skill does not auto-commit the app side — that's the user's judgment call).
   - Red: rollback the app-side edits, leave scottycore release intact (not worth reverting a published tag over an app-side failure; just document the issue and move on). User gets a failure report with next steps.

8. **Downward sync (automatic, unchanged).** The scottycore release in step 4 already dispatched upgrade PRs to all 5 apps, including the originating app. The originating app's dispatch PR will detect `changed=false` (pin already updated in step 6) and no-op. The other 4 apps get their standard GREEN/YELLOW/RED classification — nothing new here.

### Commit-time extraction nudges

Separate from the explicit `/promote` command. Goal: catch candidate extractions without requiring me to remember.

**Mechanism:** `.claude/hooks/pre-commit.sh` in each app runs the app's manager agent on the staged diff with a narrow prompt:

> Here is the staged diff. Is any of it generic enough to belong in scottycore? Look for: middleware, utility functions, schema patterns, config patterns, anything not naming app-specific domain concepts. Output one line: either `PROMOTE: <path> <reason>` or `KEEP: no extraction candidates`. Be strict — prefer KEEP over PROMOTE when unsure.

If the agent says PROMOTE, the hook prints a one-liner suggestion with a ready-to-run `/promote` invocation. The commit proceeds either way — this is a nudge, not a gate.

```
$ git commit -m "feat: add rate_limiter middleware"
[pre-commit] extraction candidate: src/scottystrike/middleware/rate_limit.py
             run: /promote src/scottystrike/middleware/rate_limit.py
[master abc1234] feat: add rate_limiter middleware
```

This should use the host's `claude -p` (same model used elsewhere — DGX later) to avoid per-commit latency. Should cache results so the same unchanged diff isn't re-evaluated.

### Failure modes and handling

| Failure | Behavior |
|---|---|
| User cancels at step 2 or 3 | No-op, no writes |
| Scottycore commit fails (e.g. merge conflict) | Abort, local changes untouched |
| Release workflow fails | Leave the tag; tell user to check Forgejo Actions. App side not touched. |
| Publish poll times out | Same as above |
| `pip install --upgrade` fails | Revert pyproject.toml in app, exit with error |
| App tests fail against new package | Leave everything in place, print test output, let user decide (fix forward or manually revert) |
| Downward sync PR conflicts in originating app | Caught by existing `changed=false` short-circuit |

### What the skill does NOT do

- Does not auto-commit on the app side (user judgment call — the extraction might require app-specific follow-up)
- Does not push app-side changes or open app PRs
- Does not touch `.scottycore-patterns.yaml` (will be retired in Phase 4 anyway)
- Does not run in CI (it's a dev-machine interactive skill)

## Implementation plan

### Step 1 — skeleton skill

- Create `/script/scottycore/.claude/skills/promote/SKILL.md`
- Implement extraction + diff preview + user confirmation only
- No scottycore commit yet, no release, no app-side changes
- Goal: user can see what *would* happen before we trust the rest

### Step 2 — scottycore side

- Add commit + tag + push logic to the skill
- Wait for release publish via Forgejo API polling
- Test end-to-end on a trivial extraction (e.g. a one-line utility) cut to v0.1.2

### Step 3 — app side

- Pyproject rewrite + pip install + local diff application
- Test runner invocation
- Rollback-on-failure logic

### Step 4 — pre-commit nudge

- Write `.claude/hooks/pre-commit.sh` template
- Install via `scottycore-init.py` for each app
- Cache diffs via SHA hash to avoid re-querying Claude on unchanged staging area

### Step 5 — first real extraction

- Pick a genuine shared pattern that currently exists in multiple apps (e.g. the `notify-hubitat` helper — it's in `sync-watcher.py` and `deploy/runner/notify-hubitat.sh`; could become `scottycore.notify.hubitat`)
- Run `/promote` on it in a real session
- Verify downward sync to the other 4 apps

## Phase 2 lessons applied

Documenting what we learned from Phase 2 so the skill doesn't repeat the same traps:

1. **Forgejo Actions defaults to `/bin/sh`, not bash.** Any step using bash-isms (`set -o pipefail`, `[[ ]]`, arrays) needs `shell: bash`. The skill's generated workflows will default to `shell: bash`.

2. **`claude -p --output-format json` wraps the response in an envelope** (`{type, result, cost_usd, session_id}`). Parsing must unwrap `.result` first, then extract the actual JSON. Parse defensively with a regex block-extractor + `json.loads(strict=False)`.

3. **Claude Code needs BOTH `~/.claude/` (dir) AND `~/.claude.json` (file).** Bind-mount both when running in a container. The skill won't do this directly but docs for running the skill on CI (if ever) must.

4. **Forgejo's API does NOT expose per-step logs.** The UI does; the REST API does not (as of 2026-04). When building features that need to diagnose workflow failures, pre-capture diagnostic artifacts *during* the workflow and upload them — don't rely on fetching logs after the fact.

5. **Forgejo PR-open responses occasionally contain raw control chars in JSON string fields.** Parse Forgejo responses with Python `json.loads(strict=False)`, not jq.

6. **Stale branches from failed re-runs break the next dispatch.** Every branch-creating step should force-delete the remote branch first (tolerating 404). Idempotency matters.

7. **Same-version re-dispatches are a valid case.** Guard bump steps with an explicit `changed=true/false` output and gate all downstream steps on it.
