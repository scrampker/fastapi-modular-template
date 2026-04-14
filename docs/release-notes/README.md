# Release Notes — Hand-Authored Adoption Guides

Auto-generated changelog (from conventional commits via `git-cliff`) covers the *what*. This directory is where you write the *how* — the adoption guide for any release that introduces a new feature, pattern, or breaking change substantial enough that the sentence-long commit message isn't enough.

## Convention

Create `vMAJOR.MINOR.PATCH.md` before tagging the release. The release workflow prepends its contents to the auto-generated changelog when dispatching the upgrade PR to each app.

**Skip the file entirely** if the release is small and the commit messages say everything — the auto-changelog alone is often enough.

## Template

```markdown
<!-- docs/release-notes/v0.2.0.md -->

## Highlights

One to three sentences framing the release for an app maintainer: what's in it, whether it's a must-adopt, whether it contains breaking changes.

## Adoption Guides

### <feature or pattern name>

**Before** (what the app is doing today):
\`\`\`python
# app's own hand-rolled version
\`\`\`

**After** (using the new scottycore API):
\`\`\`python
from scottycore.foo import NewThing
\`\`\`

**When to adopt:** ...
**When to skip:** ...

## Breaking Changes

### <thing>

- Removed: `old_api()`
- Replaced by: `new_api()`
- Migration: regex or grep the following in app code …
```

## Rationale

The per-app manager agent reads this file when classifying the upgrade PR. Rich adoption guides lead to better Yellow-classification decisions (auto-merge + open follow-up adoption issue) instead of everything either being Green (agent missed the opportunity) or Red (agent escalated because it was unsure).
