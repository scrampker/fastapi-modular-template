# scottycore-runner

Container image used by Forgejo Actions workflows across the Scotty app family. Contains everything the upgrade-review pipeline needs: Python build tooling, Node.js, Claude Code CLI, `gh`, `jq`, and a standalone Hubitat alert helper.

## Build

On the runner host (CT118 / `forgejo-runner-melbourne` at `192.168.150.231`):

```bash
cd /tmp && git clone https://forgejo.scotty.consulting/scotty/scottycore.git
cd scottycore/deploy/runner
docker build -t scottycore-runner:latest .
```

Rebuild on every Claude Code CLI bump or base-image security update. A scheduled Forgejo workflow will eventually automate this.

## Host prerequisites

The runner container relies on two bind mounts from CT118:

| Host path | Mount | Purpose |
|-----------|-------|---------|
| `/root/.config/scottycore-hubitat.json` | read-only | Hubitat alert config (lifted from `sync-watcher.py`) |
| `/root/.claude` | read-write | Claude Code sessions + credentials dir. rw so refreshed tokens persist. |
| `/root/.claude.json` | read-write | Claude Code global config (separate file — easy to miss). |

Initialize both by running `claude` interactively once on the host and completing the normal OAuth flow. Re-run whenever the session expires (~30 days).

## Secrets (Forgejo Actions)

Set on each repo at `…/settings/actions/secrets`:

| Secret | Purpose |
|--------|---------|
| `FORGEJO_TOKEN` | PAT with repo write (for opening PRs, merging, posting comments) |

No `ANTHROPIC_API_KEY` — Claude Code runs against the bind-mounted host session, which uses your existing Max subscription. Zero per-call billing.

## Use from a workflow

```yaml
# .forgejo/workflows/upgrade-review.yml
jobs:
  review:
    runs-on: [self-hosted, linux]
    container:
      image: scottycore-runner:latest
      options: >-
        -v /root/.claude:/root/.claude
        -v /root/.config/scottycore-hubitat.json:/root/.config/scottycore-hubitat.json:ro
    env:
      FORGEJO_TOKEN: ${{ secrets.FORGEJO_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - run: claude -p "review the scottycore upgrade PR…"
      - run: notify-hubitat P2 "PR needs review" || true
```

## What's on PATH

`git`, `python3`, `pip`, `node`, `npm`, `claude`, `gh`, `jq`, `curl`, `notify-hubitat`, `build`, `twine`.
