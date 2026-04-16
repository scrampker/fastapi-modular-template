#!/usr/bin/env python3
"""
ScottyCore Init — Onboard a new app into the ScottyCore ecosystem.
===================================================================
Wires a new app into the Phase 3 pipeline: version-pinned scottycore
dependency, server-side promote-scan + downward scottycore-upgrade
workflows, pre-commit extraction-candidate nudge hook, manager agent
with GREEN/YELLOW/RED classification responsibility.

What it does:
  1. Creates Forgejo repo (via API) + GitHub repo (via `gh`) + dual-remote push
  2. Pushes FORGEJO_TOKEN as a repo-scoped Actions secret (via API)
  3. Adds the app to config/apps.yaml
  4. Scaffolds a manager agent in .claude/agents/<app>-manager.md
  5. Injects a ScottyCore Pipeline section into the app's CLAUDE.md
  6. Installs the pre-commit /promote nudge hook
  7. Installs .forgejo/workflows/promote-scan.yml (upward pipeline)
  8. Installs .forgejo/workflows/scottycore-upgrade.yml (downward pipeline)
  9. Adds the app to scottycore's release.yml APPS fan-out list
 10. Commits + pushes app repo + scottycore-side changes

Usage:
    python3 scripts/scottycore-init.py /path/to/app                # interactive
    python3 scripts/scottycore-init.py /path/to/app --name myapp   # explicit name
    python3 scripts/scottycore-init.py /path/to/app --stack "Flask" # set stack description
    python3 scripts/scottycore-init.py /path/to/app --skip-forgejo # skip Forgejo repo creation
    python3 scripts/scottycore-init.py /path/to/app --github-repo scrampker/MyApp  # explicit GitHub repo
    python3 scripts/scottycore-init.py /path/to/new-app --scaffold   # scaffold a new app dir from scottycore
    python3 scripts/scottycore-init.py --list                       # list registered apps

If the target path does not exist, pass --scaffold (or answer 'y' at the
prompt) to create it by cloning scottycore as a template, stripping the
git history, and initializing a fresh repo with a first commit.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_DIR / "scripts"))

# Toolkit — generic infrastructure primitives extracted to scottylab.
# No pip install; we add automation/ to sys.path and import as scottylab_toolkit.
sys.path.insert(0, "/script/scottylab/automation")

from config_loader import CONFIG_FILE, load_apps_config
from scottylab_toolkit import (
    ansible_run, cloudflare, inventory as sl_inventory, lxc, nginx as sl_nginx,
    unifi,
)
from scottylab_toolkit.paths import (
    NGINX_HOST, PROXMOX_NODE_NAME,
    SCOTTYLAB_DIR, SCOTTYLAB_WORKLOADS,
)
from scottylab_toolkit.yaml_inserts import append_to_yaml_list

# ── ScottyCore-specific constants ────────────────────────────────────────────

FORGEJO_BASE = "https://forgejo.scotty.consulting"
FORGEJO_USER = "scotty"
FORGEJO_TOKEN_PATH = Path.home() / ".config" / "forgejo-token"

GITHUB_USER = "scrampker"

PORT_BASE = 8100  # first scottybiz-era port; scan apps.yaml for next free

# Default cert apex for new Scotty apps. Override with --domain <fqdn>.
DEFAULT_DOMAIN_APEX = "corpaholics.com"


# ── Step 1: Forgejo repo + dual-remote ───────────────────────────────────────


def _read_forgejo_token() -> str | None:
    if FORGEJO_TOKEN_PATH.exists():
        return FORGEJO_TOKEN_PATH.read_text().strip()
    return None


def create_forgejo_repo(app_name: str, description: str, default_branch: str) -> str | None:
    """Create a repo on Forgejo. Returns the clone URL or None on failure."""
    token = _read_forgejo_token()
    if not token:
        print(f"  No Forgejo token at {FORGEJO_TOKEN_PATH} — skipping repo creation")
        return None

    payload = json.dumps({
        "name": app_name,
        "description": description,
        "private": False,
        "default_branch": default_branch,
    }).encode()

    req = urllib.request.Request(
        f"{FORGEJO_BASE}/api/v1/user/repos",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            url = data.get("clone_url", "")
            print(f"  Created Forgejo repo: {data.get('html_url', url)}")
            return url
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if "already exists" in body.lower():
            url = f"{FORGEJO_BASE}/{FORGEJO_USER}/{app_name}.git"
            print(f"  Forgejo repo already exists: {url}")
            return url
        print(f"  Forgejo API error ({e.code}): {body[:200]}")
        return None
    except Exception as e:
        print(f"  Forgejo connection error: {e}")
        return None


def set_forgejo_secret(app_name: str, secret_name: str, secret_value: str) -> bool:
    """Set a repo-scoped Actions secret on the new Forgejo repo.

    Uses PUT /api/v1/repos/{owner}/{repo}/actions/secrets/{name} which
    upserts. Required so the promote-scan + scottycore-upgrade workflows
    can call back into Forgejo (create PRs, dispatch workflows, comment).
    """
    token = _read_forgejo_token()
    if not token:
        print(f"  No Forgejo token — cannot set {secret_name} secret")
        return False

    payload = json.dumps({"data": secret_value}).encode()
    req = urllib.request.Request(
        f"{FORGEJO_BASE}/api/v1/repos/{FORGEJO_USER}/{app_name}/actions/secrets/{secret_name}",
        data=payload,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.status
        if code in (201, 204):
            print(f"  Set repo secret {secret_name} on Forgejo")
            return True
        print(f"  Unexpected response setting {secret_name}: HTTP {code}")
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Forgejo secrets API error ({e.code}): {body[:200]}")
        return False
    except Exception as e:
        print(f"  Forgejo secrets connection error: {e}")
        return False


def create_github_repo(app_name: str, description: str, private: bool = True) -> bool:
    """Create a GitHub repo via `gh repo create`. Idempotent: treats
    'already exists' as success. Requires `gh` authenticated.

    Guards against GitHub's rename-alias trap: when a repo A is renamed to B,
    the API keeps responding to A with B's data forever. A naive probe would
    think A exists as a concrete repo and skip creation, then the caller's
    first push would force-push over B's history. Here we fetch the canonical
    full_name and only treat the probe as a hit when it matches the requested
    name case-insensitively. A mismatch means A is a redirect alias and the
    name is still free to claim.
    """
    requested = f"{GITHUB_USER}/{app_name}"
    probe = subprocess.run(
        ["gh", "api", f"repos/{requested}", "--jq", ".full_name"],
        capture_output=True, text=True, timeout=15,
    )
    if probe.returncode == 0:
        canonical = probe.stdout.strip()
        if canonical.lower() == requested.lower():
            print(f"  GitHub repo already exists: github.com/{requested}")
            return True
        print(f"  GitHub: {requested} is a rename-alias to {canonical}; "
              f"claiming name with a fresh repo")

    visibility = "--private" if private else "--public"
    result = subprocess.run(
        ["gh", "repo", "create", f"{GITHUB_USER}/{app_name}",
         visibility, "--description", description],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        print(f"  Created GitHub repo: github.com/{GITHUB_USER}/{app_name}")
        return True
    err = result.stderr.strip()
    if "already exists" in err.lower():
        print(f"  GitHub repo already exists: github.com/{GITHUB_USER}/{app_name}")
        return True
    print(f"  gh repo create failed: {err[:200]}")
    return False


def setup_dual_remote(app_path: Path, forgejo_url: str, github_url: str, branch: str) -> bool:
    """Configure dual-remote: origin fetches from Forgejo, pushes to both."""

    def git(*args: str) -> bool:
        r = subprocess.run(["git", "-C", str(app_path)] + list(args),
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0

    # Check current state
    result = subprocess.run(
        ["git", "-C", str(app_path), "remote", "-v"],
        capture_output=True, text=True, timeout=10,
    )
    remotes = result.stdout

    if forgejo_url in remotes:
        print(f"  Dual-remote already configured")
        return True

    # Ensure origin exists (add if missing, set-url if already configured)
    if "origin" in remotes:
        git("remote", "set-url", "origin", forgejo_url)
    else:
        git("remote", "add", "origin", forgejo_url)
    # Set origin push to both Forgejo and GitHub
    git("remote", "set-url", "--push", "origin", forgejo_url)
    git("remote", "set-url", "--add", "--push", "origin", github_url)
    # Add github as a separate remote for convenience (no-op if already present)
    if "github" not in remotes:
        git("remote", "add", "github", github_url)

    print(f"  Configured dual-remote:")
    print(f"    origin fetch: {forgejo_url}")
    print(f"    origin push:  {forgejo_url} + {github_url}")

    return True


def detect_github_remote(app_path: Path) -> str | None:
    """Try to find an existing GitHub remote URL."""
    result = subprocess.run(
        ["git", "-C", str(app_path), "remote", "-v"],
        capture_output=True, text=True, timeout=10,
    )
    for line in result.stdout.splitlines():
        if "github.com" in line and "(push)" in line:
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


# ── Step 2: Register in config/apps.yaml ─────────────────────────────────────


def pick_next_port(existing: dict) -> int:
    """Pick the next free port starting from PORT_BASE, skipping any port
    already recorded on a registered app. Dev apps on 192.168.150.6 and
    containerized apps share the same port namespace so they stay unique.
    """
    used = {
        int(cfg["port"])
        for cfg in existing.values()
        if isinstance(cfg, dict) and str(cfg.get("port", "")).isdigit()
    }
    port = PORT_BASE
    while port in used:
        port += 1
    return port


def register_app(app_path: Path, app_name: str, stack: str, branch: str,
                 port: int, fqdn: str) -> bool:
    """Add app to config/apps.yaml. Returns True if added, False if already present."""
    existing = load_apps_config()
    if app_name in existing:
        print(f"  '{app_name}' is already registered in config/apps.yaml")
        return False

    entry = f"""
{app_name}:
  path: {app_path}
  stack: {stack}
  branch: {branch}
  port: {port}
  fqdn: {fqdn}
"""
    with open(CONFIG_FILE, "a") as f:
        f.write(entry)

    print(f"  Added '{app_name}' to config/apps.yaml (port={port}, fqdn={fqdn})")
    return True


# ── docker-compose.yml in the scaffolded app ────────────────────────────────


def install_docker_compose(app_path: Path, app_name: str, port: int) -> bool:
    """Render docker-compose.yml.template into the new app with the assigned port."""
    src = CORE_DIR / "docker-compose.yml.template"
    dest = app_path / "docker-compose.yml"
    if not src.exists():
        print(f"  docker-compose template missing at {src}")
        return False
    if dest.exists():
        print(f"  docker-compose.yml already present in {app_name}")
        return False
    rendered = (src.read_text()
                .replace("__APP_NAME__", app_name)
                .replace("__APP_PORT__", str(port)))
    dest.write_text(rendered)
    print(f"  Rendered docker-compose.yml (port {port}) into {app_name}")
    return True


# ── scottylab infra declarations (nginx cert + vhost + scottycore-apps) ─────


def add_scottycore_app(app_name: str, target_host: str, port: int,
                       repo_url: str, branch: str) -> bool:
    """Append an entry to scottycore-apps.yml (scottycore-specific — kept here
    because scottycore-apps.yml is how the deploy playbook finds apps).
    Uses the toolkit's indent-aware YAML appender for the actual insert.
    """
    apps_yml = SCOTTYLAB_WORKLOADS / "scottycore-apps.yml"
    if not apps_yml.exists():
        print(f"  scottycore-apps.yml not found (expected template to be checked in)")
        return False

    text = apps_yml.read_text()
    if re.search(rf"^\s*-\s*name:\s*{re.escape(app_name)}\s*$", text, re.M):
        print(f"  scottycore-apps entry '{app_name}' already present")
        return False

    entry_lines = [
        f"      - name: {app_name}",
        f"        host: {target_host}",
        f"        port: {port}",
        f"        repo: {repo_url}",
        f"        branch: {branch}",
    ]
    if not append_to_yaml_list(apps_yml, "scottycore_apps", entry_lines):
        print(f"  Could not locate scottycore_apps: block")
        return False
    print(f"  Added scottycore-apps entry '{app_name}' -> {target_host}")
    return True



def commit_scottylab_changes(app_name: str) -> bool:
    """Commit + push scottylab infra declarations."""
    if not SCOTTYLAB_DIR.exists():
        print(f"  scottylab not found at {SCOTTYLAB_DIR} — skipping commit")
        return False

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(SCOTTYLAB_DIR)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    git("add",
        "automation/ansible/playbooks/workloads/nginx-certs.yml",
        "automation/ansible/playbooks/workloads/nginx-vhosts.yml",
        "automation/ansible/playbooks/workloads/scottycore-apps.yml",
        "automation/ansible/inventory/workloads.yml")

    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No scottylab changes to commit")
        return False

    r = git("commit", "-m", f"feat: wire {app_name} into nginx + scottycore-apps")
    if r.returncode != 0:
        print(f"  scottylab commit failed: {r.stderr.strip()[:200]}")
        return False
    push = git("push")
    if push.returncode == 0:
        print(f"  Committed and pushed scottylab changes")
    else:
        print(f"  Committed locally but push failed: {push.stderr.strip()[:200]}")
    return True


# ── Step 3: Scaffold manager agent ──────────────────────────────────────────


def scaffold_agent(app_name: str, app_path: Path, stack: str) -> bool:
    """Create .claude/agents/<app>-manager.md in scottycore."""
    agents_dir = CORE_DIR / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{app_name}-manager.md"

    if agent_file.exists():
        print(f"  Agent already exists: {agent_file.name}")
        return False

    content = f"""# {app_name} Manager Agent

You are the dedicated manager agent for **{app_name}** (`{app_path}`).
Stack: {stack}.

## Pipeline state

| Direction | Workflow | Status |
|---|---|---|
| Upward (contribute to scottycore) | `.forgejo/workflows/promote-scan.yml` | Installed |
| Downward (receive scottycore bumps) | `.forgejo/workflows/scottycore-upgrade.yml` | Installed if app has root `pyproject.toml`, else deferred |

## Your Responsibilities

### 1. Autonomous scottycore upgrade-PR review

When invoked from `.forgejo/workflows/scottycore-upgrade.yml` on a bump PR:
classify GREEN / YELLOW / RED based on CI + breaking-change impact. Emit a
single JSON object:
```json
{{"classification":"GREEN|YELLOW|RED","comment":"...","bump":"patch|minor|major","follow_up_issue":""}}
```

Bias rules:
- Prefer GREEN when genuinely uneventful
- Prefer RED over YELLOW when unsure (false RED costs 2 min; false GREEN may ship break)
- Never fabricate breaking-change impact — if grep finds no hits, the change doesn't touch this app

### 2. Interactive /promote review

When the user invokes `/promote <path>` or asks whether code belongs in scottycore,
analyze the target and output the same JSON schema. Be strict — prefer RED on any
app-specific concept leakage.

### 3. Feature implementation

- Check scottycore first (`/script/scottycore/scottycore/`) for modules covering your need
- Prefer consuming scottycore over re-implementing
- Stack-specific concerns stay local

### 4. Session-start hygiene

On session open:
1. `git fetch --quiet origin`
2. Query Forgejo for scottycore-bot comments on the last 5 commits
3. Report any pipeline activity the user missed

See the app's `CLAUDE.md` for the concrete snippet.

## Key directories
- App root: `{app_path}`
- ScottyCore: `/script/scottycore`

## What you do NOT do

- Port fixes by hand from other Scotty apps (old core-sync model is retired)
- Reference `.scottycore-patterns.yaml` (retired in Phase 4)
- Edit scottycore directly — use `/promote` or push a commit the server-side classifier will pick up
- Block commits on the pre-commit nudge (advisory only)
"""

    agent_file.write_text(content)
    print(f"  Created agent: {agent_file.name}")
    return True


# ── Step 5: Inject ScottyCore section into app CLAUDE.md ────────────────────

CLAUDE_SECTION_MARKER = "<!-- scottycore-integration -->"
# Written at the top of every scaffolded CLAUDE.md so re-runs can detect and
# replace a stale header without stomping on hand-authored content.
APP_HEADER_MARKER = "<!-- scottycore-app-header -->"


def _render_app_claude_md(app_name: str, stack: str, fqdn: str, port: int,
                          lxc_vmid: int | None, lxc_ip: str | None) -> str:
    """Build a fact-rich CLAUDE.md for a freshly-scaffolded scottycore app.

    Bakes in the live deployment coordinates (LXC VMID + IP, container port,
    public FQDN, nginx upstream) so future sessions have full context without
    spelunking through scripts. A short `## Domain` stub nudges the first
    human session to fill in what the app actually does.
    """
    vmid_str = f"CT {lxc_vmid}" if lxc_vmid is not None else "_not yet provisioned_"
    ip_str = lxc_ip if lxc_ip else "_not yet provisioned_"
    upstream_str = (f"http://{lxc_ip}:{port}" if lxc_ip
                    else f"http://{app_name}.melbourne:{port} (placeholder)")

    header = f"""# {app_name}

{APP_HEADER_MARKER}
> **Status: freshly scaffolded from scottycore. Domain logic is empty; the
> container runs the unmodified framework and responds to `/health`.**
>
> First session move: fill in the `## Domain` section below with what this
> app is actually supposed to do, then start building service modules under
> `scottycore/services/`. Until then, every request beyond `/health` will
> 404 — that's expected.

## What this is

A FastAPI app scaffolded from [scottycore](/script/scottycore). Inherits the
full framework: auth (Cloudflare / Azure / JWT / API key), multi-tenant RBAC,
4-tier settings KV, audit log, ai_backends multi-provider routing, admin API,
request logging, TOTP. See `/script/scottycore/CLAUDE.md` for the framework
reference; everything here is additive on top of that.

## Live deployment

| | |
|---|---|
| **Public URL** | https://{fqdn} |
| **Nginx upstream** | {upstream_str} |
| **LXC** | {vmid_str} on proxmox1 ({ip_str}) |
| **Host port (Docker)** | `{port}` (container listens on 8000 internally) |
| **Container image** | `{app_name}:latest` (built locally via `docker compose`) |
| **Forgejo** | https://forgejo.scotty.consulting/scotty/{app_name} |
| **GitHub (mirror)** | https://github.com/scrampker/{app_name} |

## Deployment paths

- **Dev (fast iterate)** — run directly on `192.168.150.6` via `launch.py`.
  Listens on `:{port}` like the container does. Hot-reload via `--reload` if
  you add it.
- **Prod-in-a-box (normal)** — the LXC above, Docker via `docker-compose.yml`.
  Redeploy with `ssh {ip_str or "<lxc-ip>"} 'cd /opt/scottycore/{app_name} \\
  && git pull && docker compose up -d --build'`, or run
  `ansible-playbook workloads/scottycore-apps.yml -l {app_name}.melbourne`
  from `/script/scottylab/automation/ansible/`.

## DNS path (for reference)

```
scottybiz.corpaholics.com
     │
     ├─ external (internet)    → Cloudflare proxy → melbourne tunnel → nginx vhost → upstream
     └─ LAN (behind UniFi)     → UniFi local DNS  → nginx directly   → vhost       → upstream
```

The `*.corpaholics.com` UniFi wildcard + CF tunnel ingress were installed by
`scottycore-init.py` and are idempotently reconciled on every re-run. Nothing
to do here unless you're changing how requests route.

## Domain

_TBD — replace this section with what this app actually does, which service
modules it needs under `scottycore/services/`, and any non-scottycore
dependencies (GPU, Redis, external APIs, etc.)._

## Useful commands

```bash
# Health check (external)
curl -sSf https://{fqdn}/health

# Health check (direct to LXC)
curl -sSf http://{ip_str or "<lxc-ip>"}:{port}/health

# Container logs
ssh {ip_str or "<lxc-ip>"} 'docker logs scottybiz --tail 50'

# Rebuild & restart container
ssh {ip_str or "<lxc-ip>"} 'cd /opt/scottycore/{app_name} && docker compose up -d --build'

# Reconcile everything (idempotent — heals any drift)
python3 /script/scottycore/scripts/scottycore-init.py /script/{app_name} --name {app_name}
```
<!-- /scottycore-app-header -->

"""

    pipeline = f"""
<!-- scottycore-integration -->
## ScottyCore Pipeline

This app depends on the [scottycore](/script/scottycore) package (pinned in
`pyproject.toml` via
`git+https://forgejo.scotty.consulting/scotty/scottycore.git@vX.Y.Z`).

### Automated pipeline

**Upward — `.forgejo/workflows/promote-scan.yml`**: every push to master runs
a `claude -p` classifier on the Python diff. If code looks like shared-library
material, it dispatches scottycore's `promote-receive.yml` for autonomous
extraction + release.

**Downward — `.forgejo/workflows/scottycore-upgrade.yml`**: when scottycore
cuts a release, this app receives a workflow_dispatch that bumps the pin,
runs CI, and has the manager agent classify the PR as GREEN / YELLOW / RED.

### `/promote` skill

To proactively move code to scottycore without waiting for the next push,
invoke `/promote <path>` from a Claude Code session.

### Kill switches

- `export SCOTTYCORE_SKIP_PROMOTE_NUDGE=1` — disables local pre-commit nudge
- Remove `.forgejo/workflows/promote-scan.yml` — disables server-side scan
<!-- /scottycore-integration -->

## Session-start: check pipeline feedback

```bash
git fetch --quiet origin
TOKEN=$(cat ~/.config/forgejo-token 2>/dev/null)
[ -n "$TOKEN" ] && for sha in $(git log -5 --format='%H' origin/master); do
    COMMENTS=$(curl -s -H "Authorization: token $TOKEN" \\
      "https://forgejo.scotty.consulting/api/v1/repos/scotty/{app_name}/commits/$sha/comments")
    if echo "$COMMENTS" | grep -q '"body"'; then
        echo "=== $sha ==="
        echo "$COMMENTS" | python3 -c "import json,sys;[print(c['body']) for c in json.load(sys.stdin)]"
    fi
done
```
Report any scottycore-bot comments before starting work.
"""
    return header + pipeline


def write_app_claude_md(app_path: Path, app_name: str, stack: str, fqdn: str,
                       port: int, lxc_vmid: int | None,
                       lxc_ip: str | None) -> bool:
    """Render a fact-rich CLAUDE.md for the app.

    Idempotent — rewrites the auto-generated header (everything between the
    two app-header markers) on every run so deployment facts stay fresh, but
    preserves any content *after* the header markers that humans added.
    """
    claude_md = app_path / "CLAUDE.md"
    rendered = _render_app_claude_md(
        app_name, stack, fqdn, port, lxc_vmid, lxc_ip,
    )

    if not claude_md.exists():
        claude_md.write_text(rendered)
        print(f"  Wrote CLAUDE.md (fresh)")
        return True

    existing = claude_md.read_text()

    # Scaffold copied scottycore's CLAUDE.md — recognize by its H1 and replace
    # wholesale. Safe: a real app's CLAUDE.md starts with `# <app_name>`.
    if existing.lstrip().startswith("# ScottyCore"):
        claude_md.write_text(rendered)
        print(f"  Replaced scottycore template CLAUDE.md with app-specific version")
        return True

    # Subsequent runs: replace the auto-generated header block in place.
    if APP_HEADER_MARKER in existing:
        start = existing.find(APP_HEADER_MARKER)
        end_marker = "<!-- /scottycore-app-header -->"
        end = existing.find(end_marker)
        if start != -1 and end != -1:
            # Find the H1 line that precedes the start marker so we refresh it too
            h1_start = existing.rfind("\n# ", 0, start)
            h1_start = 0 if h1_start == -1 else h1_start + 1
            tail = existing[end + len(end_marker):].lstrip("\n")
            claude_md.write_text(rendered + tail)
            print(f"  Refreshed CLAUDE.md deployment facts")
            return True

    # First run against an unrecognized CLAUDE.md: prepend app header, keep rest.
    if CLAUDE_SECTION_MARKER in existing:
        # Already has pipeline section but no app header — inject header at top
        # (after the first heading if present) and leave the rest alone.
        claude_md.write_text(rendered.split("<!-- /scottycore-app-header -->")[0]
                             + "<!-- /scottycore-app-header -->\n\n"
                             + existing)
        print(f"  Prepended app header to existing CLAUDE.md")
        return True

    claude_md.write_text(rendered)
    print(f"  Wrote CLAUDE.md (replaced unknown content)")
    return True


# ── Step 6: Commit + push ───────────────────────────────────────────────────


def install_promote_scan_workflow(app_path: Path, app_name: str) -> bool:
    """Install the server-side promote-scan.yml workflow in the app repo.

    Runs on every push to master, classifies the diff for scottycore-worthy
    extraction candidates, and dispatches scottycore's promote-receive.yml
    when appropriate. The authoritative path for upward sync — the local
    pre-commit nudge is only an early warning.
    """
    src = CORE_DIR / ".claude" / "templates" / "promote-scan.yml"
    workflows_dir = app_path / ".forgejo" / "workflows"
    dest = workflows_dir / "promote-scan.yml"

    if not src.exists():
        print(f"  template missing at {src}")
        return False
    workflows_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    print(f"  installed .forgejo/workflows/promote-scan.yml in {app_name}")
    return True


def install_scottycore_upgrade_workflow(app_path: Path, app_name: str, branch: str) -> bool:
    """Install .forgejo/workflows/scottycore-upgrade.yml in the app repo.

    This is the downward half of the pipeline — receives workflow_dispatch
    from scottycore's release.yml on a tagged release, bumps the pin, opens
    a PR, and has the manager agent classify it GREEN/YELLOW/RED.

    Template placeholders: __APP_NAME__, __BRANCH__.
    Only installed if the app has a root pyproject.toml — without one, the
    pin-bump step has nothing to edit.
    """
    if not (app_path / "pyproject.toml").exists():
        print(f"  No root pyproject.toml in {app_name} — deferring scottycore-upgrade.yml")
        return False

    src = CORE_DIR / ".claude" / "templates" / "scottycore-upgrade.yml"
    if not src.exists():
        print(f"  template missing at {src}")
        return False

    workflows_dir = app_path / ".forgejo" / "workflows"
    dest = workflows_dir / "scottycore-upgrade.yml"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    rendered = src.read_text().replace("__APP_NAME__", app_name).replace("__BRANCH__", branch)
    dest.write_text(rendered)
    print(f"  installed .forgejo/workflows/scottycore-upgrade.yml in {app_name}")
    return True


def add_to_release_apps(app_name: str) -> bool:
    """Add app to scottycore's .forgejo/workflows/release.yml APPS fan-out list.

    The release workflow dispatches `scottycore-upgrade` to every app in
    this list on every tagged release. Idempotent — no-op if already present.
    """
    release_yml = CORE_DIR / ".forgejo" / "workflows" / "release.yml"
    if not release_yml.exists():
        print(f"  release.yml not found at {release_yml}")
        return False

    text = release_yml.read_text()

    m = re.search(r"(APPS:\s*>-\n)((?:[ \t]+\S+\n)+)", text)
    if not m:
        print(f"  Could not locate APPS: block in release.yml")
        return False

    block = m.group(2)
    tokens = block.split()
    if app_name in tokens:
        print(f"  {app_name} already in release.yml APPS")
        return False

    first_line = block.splitlines()[0]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    new_block = block.rstrip() + f"\n{indent}{app_name}\n"
    text = text[: m.start(2)] + new_block + text[m.end(2) :]

    release_yml.write_text(text)
    print(f"  Added {app_name} to release.yml APPS fan-out list")
    return True


def install_promote_nudge_hook(app_path: Path, app_name: str) -> bool:
    """Install the pre-commit /promote nudge hook into the app's .git/hooks.

    The hook runs `claude -p` on staged diffs and suggests /promote
    invocations when code looks extractable. Never blocks commits.
    Idempotent — overwrites an existing hook if we previously installed one.
    """
    src = CORE_DIR / ".claude" / "hooks" / "pre-commit-promote-nudge.sh"
    hooks_dir = app_path / ".git" / "hooks"
    dest = hooks_dir / "pre-commit"

    if not src.exists():
        print(f"  hook source missing at {src}")
        return False
    if not hooks_dir.exists():
        print(f"  {hooks_dir} does not exist — skipping hook install")
        return False

    # If an existing hook is present and wasn't installed by us, back it up
    marker = "pre-commit-promote-nudge"
    if dest.exists():
        existing = dest.read_text(errors="replace")
        if marker not in existing:
            backup = hooks_dir / "pre-commit.pre-scottycore.bak"
            dest.rename(backup)
            print(f"  existing pre-commit hook backed up to {backup.name}")

    # Write a wrapper that calls our hook and preserves the user's ability
    # to add more logic later (symlink would be brittle if the user edits it).
    wrapper = f"""#!/usr/bin/env bash
# pre-commit (installed by scottycore-init.py — {marker})
# Calls the shared nudge hook from scottycore. Never blocks the commit.
{src}
exit 0
"""
    dest.write_text(wrapper)
    dest.chmod(0o755)
    print(f"  installed pre-commit /promote nudge hook in {app_name}")
    return True


def commit_app_changes(app_path: Path, app_name: str) -> bool:
    """Commit the generated files in the app repo."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(app_path)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    # Stage the files we generated
    git("add", "CLAUDE.md",
        ".forgejo/workflows/promote-scan.yml",
        ".forgejo/workflows/scottycore-upgrade.yml",
        "docker-compose.yml",
        "pyproject.toml")

    # Check if there's anything to commit
    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No changes to commit in {app_name}")
        return False

    result = git("commit", "-m", f"feat: wire {app_name} into ScottyCore pipeline\n\n"
                 f"Added CLAUDE.md Pipeline section, promote-scan.yml workflow,\n"
                 f"and scottycore dependency pin. Generated by scottycore-init.py.")

    if result.returncode == 0:
        print(f"  Committed in {app_name}")
        return True
    else:
        print(f"  Commit failed: {result.stderr.strip()[:200]}")
        return False


def push_app(app_path: Path, branch: str, force: bool = False) -> bool:
    """Push app repo to all remotes. `force` is used on first onboarding
    to overwrite any stale history on freshly-created remotes (e.g. a
    reused GitHub repo that held commits from a prior attempt).
    """
    cmd = ["git", "-C", str(app_path), "push", "-u", "origin", branch]
    if force:
        cmd.insert(cmd.index("push") + 1, "--force")
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        print(f"  Pushed to all remotes")
        return True
    else:
        print(f"  Push failed: {result.stderr.strip()[:200]}")
        return False


def commit_scottycore_changes(app_name: str) -> bool:
    """Commit the scottycore-side changes (apps.yaml, agent file)."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(CORE_DIR)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    git("add", "config/apps.yaml",
        f".claude/agents/{app_name}-manager.md",
        ".forgejo/workflows/release.yml")

    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No scottycore changes to commit")
        return False

    result = git("commit", "-m",
                 f"feat: register {app_name} in app registry + manager agent")

    if result.returncode == 0:
        push = subprocess.run(
            ["git", "-C", str(CORE_DIR), "push"],
            capture_output=True, text=True, timeout=60,
        )
        if push.returncode == 0:
            print(f"  Committed and pushed scottycore changes")
        else:
            print(f"  Committed but push failed: {push.stderr.strip()[:200]}")
        return True
    else:
        print(f"  ScottyCore commit failed: {result.stderr.strip()[:200]}")
        return False


# ── Scaffold new app from scottycore template ───────────────────────────────


def scaffold_new_app(app_path: Path, app_name: str) -> bool:
    """Create a new app directory by cloning scottycore as a template.

    Mirrors the manual steps from scottycore/CLAUDE.md:
      1. Copy scottycore tree (minus .git, venv, caches, data, db files)
      2. Rename pyproject.toml project name to app_name
      3. `git init` + initial commit
    """
    import shutil

    if app_path.exists():
        print(f"  {app_path} already exists — skipping scaffold")
        return False

    app_path.parent.mkdir(parents=True, exist_ok=True)

    ignore_names = {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "node_modules", "data",
        "app.db", "app.db-journal", ".env",
    }

    def _ignore(_dir, names):
        return [n for n in names if n in ignore_names or n.endswith(".pyc")]

    print(f"  Copying scottycore template to {app_path}")
    shutil.copytree(CORE_DIR, app_path, ignore=_ignore)

    # Update pyproject.toml project name if present
    pyproject = app_path / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        updated = re.sub(
            r'(?m)^name\s*=\s*"[^"]+"',
            f'name = "{app_name}"',
            text,
            count=1,
        )
        if updated != text:
            pyproject.write_text(updated)
            print(f"  Set pyproject.toml name = \"{app_name}\"")

    # Fresh git repo
    subprocess.run(["git", "-C", str(app_path), "init", "-b", "master"],
                   capture_output=True, text=True, timeout=15)
    subprocess.run(["git", "-C", str(app_path), "add", "-A"],
                   capture_output=True, text=True, timeout=30)
    r = subprocess.run(
        ["git", "-C", str(app_path), "commit", "-m",
         f"chore: scaffold {app_name} from scottycore template"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        print(f"  Initialized git repo with scaffold commit")
    else:
        print(f"  git init/commit warning: {r.stderr.strip()[:200]}")
    return True


# ── Interactive pattern selection ────────────────────────────────────────────


# ── Helpers ──────────────────────────────────────────────────────────────────


def detect_branch(app_path: Path) -> str:
    """Detect the current branch of a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(app_path), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "master"


def detect_stack(app_path: Path) -> str:
    """Try to detect the app's stack from common files."""
    if (app_path / "pyproject.toml").exists():
        content = (app_path / "pyproject.toml").read_text()
        if "fastapi" in content.lower():
            return "FastAPI"
        if "flask" in content.lower():
            return "Flask"
        if "django" in content.lower():
            return "Django"
        return "Python"
    if (app_path / "requirements.txt").exists():
        content = (app_path / "requirements.txt").read_text().lower()
        if "fastapi" in content:
            return "FastAPI"
        if "flask" in content:
            return "Flask"
        return "Python"
    if (app_path / "package.json").exists():
        return "Node.js"
    if (app_path / "Cargo.toml").exists():
        return "Rust"
    if (app_path / "go.mod").exists():
        return "Go"
    ps_files = list(app_path.glob("*.ps1")) + list(app_path.glob("*.psm1"))
    if ps_files:
        return "PowerShell"
    if (app_path / "Dockerfile").exists() or (app_path / "docker-compose.yml").exists():
        return "Docker"
    return "unknown"


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]

    if "--list" in args:
        existing = load_apps_config()
        print(f"\nRegistered apps ({len(existing)}):")
        for name, cfg in existing.items():
            core_tag = " [CORE]" if cfg.get("is_core") else ""
            print(f"  {name}: {cfg.get('path', '?')} ({cfg.get('stack', '?')}){core_tag}")
        return

    if not args or args[0].startswith("--"):
        print(__doc__)
        return

    app_path = Path(args[0]).resolve()

    # Parse flags
    app_name = None
    stack = None
    github_repo = None
    domain = None
    port_override = None
    skip_forgejo = "--skip-forgejo" in args
    skip_infra = "--skip-infra" in args
    scaffold_flag = "--scaffold" in args

    for i, arg in enumerate(args):
        if arg == "--name" and i + 1 < len(args):
            app_name = args[i + 1]
        if arg == "--stack" and i + 1 < len(args):
            stack = args[i + 1]
        if arg == "--github-repo" and i + 1 < len(args):
            github_repo = args[i + 1]
        if arg == "--domain" and i + 1 < len(args):
            domain = args[i + 1]
        if arg == "--port" and i + 1 < len(args):
            port_override = int(args[i + 1])

    if app_name is None:
        app_name = app_path.name.lower().replace(" ", "-")

    # Scaffold the app directory if it doesn't exist yet
    if not app_path.exists():
        if scaffold_flag:
            do_scaffold = True
        elif sys.stdin.isatty():
            ans = input(
                f"Path does not exist: {app_path}\n"
                f"Scaffold a new app from scottycore template? [Y/n]: "
            ).strip().lower()
            do_scaffold = ans in ("", "y", "yes")
        else:
            print(f"Error: path does not exist: {app_path} "
                  f"(pass --scaffold to create it)")
            sys.exit(1)

        if not do_scaffold:
            print("Aborted — no changes made.")
            sys.exit(1)

        print(f"\nStep 0: Scaffolding new app at {app_path}")
        scaffold_new_app(app_path, app_name)

    if stack is None:
        stack = detect_stack(app_path)

    branch = detect_branch(app_path)

    print(f"\n{'='*60}")
    print(f"  ScottyCore Init — Onboarding '{app_name}'")
    print(f"{'='*60}")
    print(f"  Path:    {app_path}")
    print(f"  Stack:   {stack}")
    print(f"  Branch:  {branch}")
    print()

    # Step 1: Forgejo repo + dual-remote
    print("Step 1: Git remotes (Forgejo + GitHub)")
    github_url = detect_github_remote(app_path)
    if github_url:
        print(f"  Found GitHub remote: {github_url}")
    elif github_repo:
        github_url = f"https://github.com/{github_repo}.git"
        print(f"  Using provided GitHub repo: {github_url}")
    else:
        github_url = f"https://github.com/{GITHUB_USER}/{app_name}.git"
        print(f"  Assuming GitHub repo: {github_url}")

    # GitHub repo (idempotent; needed before first dual-push)
    if not detect_github_remote(app_path):
        create_github_repo(app_name, description=f"Scotty app: {stack}")

    if not skip_forgejo:
        forgejo_url = create_forgejo_repo(
            app_name,
            description=f"Scotty app: {stack}",
            default_branch=branch,
        )
        if forgejo_url:
            setup_dual_remote(app_path, forgejo_url, github_url, branch)
            # Push FORGEJO_TOKEN to the new repo so its workflows can
            # create PRs, dispatch workflows, and post commit comments.
            token = _read_forgejo_token()
            if token:
                set_forgejo_secret(app_name, "FORGEJO_TOKEN", token)
    else:
        print(f"  --skip-forgejo: skipping Forgejo repo creation")

    # Port assignment + FQDN. Reuse existing port on re-runs so idempotency
    # doesn't shift ports out from under the deployed container.
    existing_apps = load_apps_config()
    if port_override is not None:
        port = port_override
    elif app_name in existing_apps and "port" in existing_apps[app_name]:
        port = int(existing_apps[app_name]["port"])
    else:
        port = pick_next_port(existing_apps)
    if domain:
        fqdn = domain
    elif app_name in existing_apps and "fqdn" in existing_apps[app_name]:
        fqdn = existing_apps[app_name]["fqdn"]
    else:
        fqdn = f"{app_name}.{DEFAULT_DOMAIN_APEX}"
    apex = fqdn.split(".", 1)[1] if "." in fqdn else fqdn

    # Step 2: Register
    print(f"\nStep 2: Register in config/apps.yaml")
    register_app(app_path, app_name, stack, branch, port, fqdn)

    # Step 2b: Emit docker-compose.yml into the scaffolded app
    print(f"\nStep 2b: Render docker-compose.yml into app")
    install_docker_compose(app_path, app_name, port)

    # Step 3: Manager agent
    print(f"\nStep 3: Scaffold manager agent")
    scaffold_agent(app_name, app_path, stack)

    # Step 4: CLAUDE.md is written AFTER provisioning (Step 5e2) so the
    # fact-rich header can bake in the real LXC VMID + IP. See below.

    # Step 5a: Install pre-commit nudge hook (early-warning, local)
    print(f"\nStep 5a: Install pre-commit /promote nudge hook")
    install_promote_nudge_hook(app_path, app_name)

    # Step 5b: Install server-side promote-scan workflow (authoritative upward)
    print(f"\nStep 5b: Install promote-scan.yml workflow (upward pipeline)")
    install_promote_scan_workflow(app_path, app_name)

    # Step 5c: Install scottycore-upgrade workflow (downward pipeline)
    print(f"\nStep 5c: Install scottycore-upgrade.yml workflow (downward pipeline)")
    upgrade_installed = install_scottycore_upgrade_workflow(app_path, app_name, branch)

    # Step 5d: Add to scottycore release.yml APPS fan-out (only if upgrade.yml installed)
    if upgrade_installed:
        print(f"\nStep 5d: Add {app_name} to scottycore release.yml APPS")
        add_to_release_apps(app_name)

    # Step 5e: Provision LXC on proxmox1 (idempotent — reuses if hostname matches)
    lxc_ip = None
    lxc_vmid = None
    if not skip_infra:
        print(f"\nStep 5e: Provision LXC on {PROXMOX_NODE_NAME}")
        prov = lxc.provision(app_name)
        if prov:
            lxc_vmid, lxc_ip = prov

    # Step 5e2: Write fact-rich CLAUDE.md with live deployment coords baked in
    print(f"\nStep 5e2: Write app CLAUDE.md with live deployment facts")
    write_app_claude_md(app_path, app_name, stack, fqdn, port,
                        lxc_vmid, lxc_ip)

    # Step 5f: Declare infra in scottylab (cert apex, nginx vhost, scottycore-apps, inventory)
    if not skip_infra:
        print(f"\nStep 5f: Declare infra in scottylab")
        target_host = f"{app_name}.melbourne"
        repo_url = f"{FORGEJO_BASE}/{FORGEJO_USER}/{app_name}.git"
        upstream = lxc_ip or target_host  # real IP if we have it; else placeholder
        sl_nginx.add_cert_apex(apex)
        sl_nginx.add_vhost(app_name, fqdn, apex, upstream, port)
        add_scottycore_app(app_name, target_host, port, repo_url, branch)
        if lxc_ip and lxc_vmid is not None:
            sl_inventory.register(target_host, lxc_ip, lxc_vmid,
                                  note=f"scottycore app — {stack}")
        commit_scottylab_changes(app_name)

    # Step 5g: Publish via Cloudflare (DNS CNAME + tunnel ingress)
    if not skip_infra:
        print(f"\nStep 5g: Publish {fqdn} via Cloudflare tunnel")
        cloudflare.ensure_cname(fqdn, apex)
        cloudflare.ensure_tunnel_ingress(fqdn)

    # Step 5g2: UniFi local DNS — point LAN clients straight at nginx for fqdn.
    # Skipped silently if secrets/unifi.yml is missing or still a placeholder.
    # Stateful: no-op if an existing *.<parent> wildcard already covers fqdn.
    if not skip_infra:
        print(f"\nStep 5g2: UniFi gateway DNS for {fqdn}")
        unifi.ensure_dns(fqdn, a_value=NGINX_HOST)

    # Step 5g3: Reconcile UniFi wildcards against nginx-certs.yml — every zone
    # nginx serves should have a matching *.<zone> A+AAAA pair on the gateway.
    # Declarative: deletions on the gateway get healed on any re-run.
    if not skip_infra:
        print(f"\nStep 5g3: UniFi wildcard sync (zones from nginx-certs.yml)")
        zones = sl_nginx.cert_zones()
        if zones:
            unifi.sync_wildcards(zones, NGINX_HOST)


    # Step 6: Commit + push app changes
    print(f"\nStep 6: Commit and push app changes")
    committed = commit_app_changes(app_path, app_name)
    if committed:
        # Force on first onboarding: the remotes were just provisioned
        # (or reused), and the scaffold is the authoritative starting point.
        push_app(app_path, branch, force=True)

    # Step 7: Commit + push scottycore changes
    print(f"\nStep 7: Commit and push scottycore changes")
    commit_scottycore_changes(app_name)

    # Step 8: Run ansible — install Docker on new LXC, issue cert, deploy, publish vhost
    if not skip_infra and lxc_ip:
        print(f"\nStep 8: Run ansible end-to-end")
        target_host = f"{app_name}.melbourne"
        ansible_run.install_docker(target_host)
        ansible_run.issue_certs()
        ansible_run.deploy_scottycore_app(target_host)
        ansible_run.publish_vhost()

    print(f"\n{'='*60}")
    print(f"  Done! '{app_name}' is now part of the ScottyCore ecosystem.")
    print(f"{'='*60}")
    print(f"\n  What happened:")
    print(f"  - Forgejo repo created at {FORGEJO_BASE}/{FORGEJO_USER}/{app_name}")
    print(f"  - Dual-remote configured (push to Forgejo + GitHub)")
    print(f"  - Registered in config/apps.yaml")
    print(f"  - Manager agent created in scottycore/.claude/agents/")
    print(f"  - ScottyCore Pipeline section injected into CLAUDE.md")
    print(f"  - Pre-commit /promote nudge hook installed")
    print(f"  - .forgejo/workflows/promote-scan.yml installed (upward)")
    print(f"  - .forgejo/workflows/scottycore-upgrade.yml installed (downward)")
    print(f"  - FORGEJO_TOKEN set as repo-scoped Actions secret")
    print(f"  - {app_name} added to scottycore release.yml APPS fan-out")
    print(f"  - docker-compose.yml rendered (port {port})")
    if not skip_infra:
        if lxc_ip and lxc_vmid is not None:
            print(f"  - Provisioned LXC CT {lxc_vmid} on {PROXMOX_NODE_NAME} at {lxc_ip}")
            print(f"  - Registered {app_name}.melbourne in scottylab inventory")
        print(f"  - scottylab: {apex} added to nginx-certs.yml (if new apex)")
        print(f"  - scottylab: {fqdn} -> http://{lxc_ip or app_name + '.melbourne'}:{port} in nginx-vhosts.yml")
        print(f"  - scottylab: {app_name} added to scottycore-apps.yml deploy list")
        print(f"  - Cloudflare: {fqdn} CNAME -> melbourne tunnel + ingress rule")
        print(f"  - UniFi: {fqdn} A/AAAA (or wildcard coverage confirmed)")
        print(f"  - UniFi: wildcard *.<zone> A+AAAA for every nginx-certs zone")
        print(f"  - Ansible: docker installed, cert issued, container deployed, vhost published")
    print(f"  - All changes committed and pushed")
    print(f"\n  Verify:  curl -sSf https://{fqdn}/health")
    print()


if __name__ == "__main__":
    main()
