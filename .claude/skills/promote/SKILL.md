---
name: promote
description: |
  Extract code from the current app into the scottycore package, cut a new scottycore release, bump the app to it, reinstall, rewrite imports, and run the app's tests. Use when working in any Scotty-family app and a piece of code belongs in the shared scottycore library. Accepts a path (file, dir, or file::function).
---

# /promote

Extract code from the app you're currently working in into scottycore, release it as a new scottycore version, then update the current app to use the published package. Full round-trip in ~60 seconds. Version-disciplined — no editable installs, no shortcuts that hide drift.

## Invocation

```
/promote <path>
```

`<path>` is relative to the current app's repo root. Examples:

- `src/scottyscribe/middleware/rate_limit.py` — whole file
- `src/scottyscribe/util/ids.py::new_uuid` — single function
- `src/scottyscribe/middleware/` — whole directory

## Preconditions (verify before starting)

1. **You are in a Scotty-family app repo.** Run `grep -q 'scottycore @ git+' pyproject.toml` — must match. If not, abort: "not a scottycore-using app".
2. **Scottycore checkout exists at `/script/scottycore`.** Run `test -d /script/scottycore/.git`. If missing, abort.
3. **Both repos have clean working trees** for the files you're about to touch. Uncommitted changes elsewhere are fine, but the path being promoted must be committed or cleanly staged. If dirty on the path being promoted, ask the user to commit or stash first.
4. **User is authenticated to Forgejo.** `test -f ~/.config/forgejo-token`. If missing, abort with setup instructions.

## Steps

### 1. Analyze the target

Read the path the user specified. Also grep nearby files for:

- Usages of the target (callers, imports) — for the app-side rewrite later
- App-specific names (e.g., "scottyscribe", "scribe_", config keys, DB table names)
- Dependencies: which stdlib / third-party imports the code uses
- Co-located tests (if `tests/` has a test for this path, include it in the extraction)

### 2. Propose the extraction

Emit **two diffs** to the user:

**Scottycore side** (to be created):
- A new module path, typically `scottycore/<category>/<name>.py`. Categories: `middleware`, `util`, `notify`, `security`, `services/<domain>`. Pick the most natural one; ask the user if ambiguous.
- Names stripped of app-isms (rename `ScribeRateLimiter` → `RateLimiter`, etc.)
- Imports fixed to use scottycore's own modules where applicable
- Test file at `tests/<category>/test_<name>.py`
- `scottycore/<category>/__init__.py` updated with a re-export if there isn't one already

**App side** (to be modified):
- The original file either deleted or rewritten to `from scottycore.<category>.<name> import <Public>`
- Callers updated to use the new import path
- Local tests for this code deleted (tests live with the code in scottycore now)

Show both diffs clearly. Then ask:

> Proceed with extraction? (y/N)

If the user declines, abort — no writes anywhere.

### 3. Propose the version bump

Read `/script/scottycore/pyproject.toml` — find `version = "X.Y.Z"`.

Derive bump from the nature of the extraction:
- New public API in scottycore → **minor** (`feat:`): `0.1.1` → `0.2.0`
- Pure refactor / internal helper lift, no new public API → **patch** (`refactor:`): `0.1.1` → `0.1.2`
- Extraction removes or renames existing scottycore API → **major** (`feat!:`): `0.1.1` → `1.0.0`

Default to minor+`feat:` for extractions. Show the proposed commit message:

```
feat: add <name> (extracted from <app>)

<one-paragraph description of what was lifted and why>
```

Ask:

> Commit to scottycore master as vX.Y.Z? (y/N)

### 4. Commit + release scottycore

All commands run with explicit `-C /script/scottycore` so cwd doesn't matter.

```bash
# Write the extracted files (Write tool)
# Edit scottycore/<category>/__init__.py to add re-export (Edit tool)
# Bump scottycore/pyproject.toml version (Edit tool)
# Bump scottycore/scottycore/__init__.py __version__ (Edit tool)

git -C /script/scottycore add <the files you created/edited>
git -C /script/scottycore commit -m "<conventional-commits message>"
git -C /script/scottycore push origin master

git -C /script/scottycore tag -a vX.Y.Z -m "scottycore vX.Y.Z — <summary>"
git -C /script/scottycore push origin vX.Y.Z
```

The tag push fires `.forgejo/workflows/release.yml` automatically. The release workflow will also dispatch upgrade PRs to the 5 apps, including the originating one — that PR will no-op locally because we'll update the pin ourselves in step 6.

### 5. Wait for the release to publish

Poll the Forgejo releases API until the wheel is attached. Timeout 5 minutes.

```bash
TOKEN=$(cat ~/.config/forgejo-token)
for i in $(seq 1 60); do
  ASSET=$(curl -sS -H "Authorization: token $TOKEN" \
    "https://forgejo.scotty.consulting/api/v1/repos/scotty/scottycore/releases/tags/vX.Y.Z" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(next((a["name"] for a in d.get("assets",[]) if a["name"].endswith(".whl")),""))')
  if [ -n "$ASSET" ]; then echo "published: $ASSET"; break; fi
  sleep 5
done
```

If the loop exits without finding the asset, tell the user to check Forgejo Actions manually, and keep the app side untouched.

### 6. Bump the app's pin + reinstall

In the app's pyproject.toml:

```bash
python3 -c "
import re,pathlib
p=pathlib.Path('pyproject.toml')
t=p.read_text()
p.write_text(re.sub(
    r'(scottycore\s*@\s*git\+https://forgejo\.scotty\.consulting/scotty/scottycore\.git@)v[0-9]+\.[0-9]+\.[0-9]+',
    r'\g<1>vX.Y.Z', t))
"
pip install --upgrade "scottycore @ git+https://forgejo.scotty.consulting/scotty/scottycore.git@vX.Y.Z"
```

### 7. Apply the app-side diff

Apply the app-side changes proposed in step 2 using the Edit/Write tools. Delete the local file if it was fully replaced. Update callers.

### 8. Run the app's tests

```bash
pytest -q
```

Capture the result. If red: leave all files in place, show the test output to the user, and tell them to decide whether to fix forward or revert. Do NOT auto-revert — the user may want to inspect the failure.

### 9. Report

Green path — summarize concisely:

```
✓ scottycore vX.Y.Z published (<N>s)
✓ <app> pin updated, pip reinstalled
✓ app-side rewrite applied (<files changed>)
✓ app tests pass (<count>)

Next: review the app-side diff and commit when ready:
  git add <paths>
  git commit -m "refactor: use scottycore.<category>.<name>"

Other apps will receive upgrade PRs via the scottycore release workflow.
```

Do NOT auto-commit on the app side. The user owns that final commit.

## Failure modes

| Failure | Action |
|---|---|
| User declines step 2 or 3 | Abort. No writes. |
| Scottycore commit/push fails | Show error, abort. Local changes untouched. |
| Release workflow fails or wheel never publishes | Tell user, keep app side untouched. |
| `pip install --upgrade` fails | Revert pyproject.toml, abort. |
| App tests fail | Leave in place. Print failing tests. User decides next step. |
| Self-dispatch PR (scottycore → originating app) opens | Harmless — the bump-check no-ops because pin matches. Ignore. |

## Non-goals

- **No editable install.** Version discipline is why this exists.
- **No auto-commit on the app side.** User judgment.
- **No draft/pre-release.** Always a real semver tag.
- **No extraction without user confirmation.** The analysis/diff preview is mandatory.
