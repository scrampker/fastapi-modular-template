#!/usr/bin/env python3
"""
ScottyCore Init — Onboard a new app into the ScottyCore ecosystem.
===================================================================
Wires a new app into the Phase 3 pipeline: version-pinned scottycore
dependency, server-side promote-scan + downward scottycore-upgrade
workflows, pre-commit extraction-candidate nudge hook, manager agent
with GREEN/YELLOW/RED classification responsibility.

What it does:
  1. Creates Forgejo repo + sets up dual-remote push (Forgejo + GitHub)
  2. Adds the app to config/apps.yaml
  3. Scaffolds a manager agent in .claude/agents/<app>-manager.md
  4. Injects a ScottyCore Pipeline section into the app's CLAUDE.md
  5. Installs the pre-commit /promote nudge hook
  6. Installs .forgejo/workflows/promote-scan.yml
  7. Commits the generated files in the app repo
  8. Pushes to both remotes
  9. Commits the scottycore-side changes (apps.yaml, agent)

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

from config_loader import CONFIG_FILE, load_apps_config

# ── Infrastructure constants ──────────────────────────────────────────────────

FORGEJO_BASE = "https://forgejo.scotty.consulting"
FORGEJO_USER = "scotty"
FORGEJO_TOKEN_PATH = Path.home() / ".config" / "forgejo-token"

GITHUB_USER = "scrampker"


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

    # Set origin to Forgejo (fetch)
    git("remote", "set-url", "origin", forgejo_url)
    # Set origin push to both Forgejo and GitHub
    git("remote", "set-url", "--push", "origin", forgejo_url)
    git("remote", "set-url", "--add", "--push", "origin", github_url)
    # Add github as a separate remote for convenience
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


def register_app(app_path: Path, app_name: str, stack: str, branch: str) -> bool:
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
"""
    with open(CONFIG_FILE, "a") as f:
        f.write(entry)

    print(f"  Added '{app_name}' to config/apps.yaml")
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

CLAUDE_TEMPLATE = """
<!-- scottycore-integration -->
## ScottyCore Pipeline

This app is part of the **Scotty app family** and depends on the
[scottycore](/script/scottycore) package (pinned in `pyproject.toml` via
`git+https://forgejo.scotty.consulting/scotty/scottycore.git@vX.Y.Z`).

### Automated pipeline

**Upward — `.forgejo/workflows/promote-scan.yml`**: every push to master runs
a `claude -p` classifier on the Python diff. If code looks like shared-library
material, it dispatches scottycore's `promote-receive.yml` for autonomous
extraction + release.

**Downward — `.forgejo/workflows/scottycore-upgrade.yml`** (if installed):
when scottycore cuts a release, this app receives a workflow_dispatch that
bumps the pin, runs CI, and has the manager agent classify the PR as
GREEN / YELLOW / RED.

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
      "https://forgejo.scotty.consulting/api/v1/repos/scotty/APP_NAME/commits/$sha/comments")
    if echo "$COMMENTS" | grep -q '"body"'; then
        echo "=== $sha ==="
        echo "$COMMENTS" | python3 -c "import json,sys;[print(c['body']) for c in json.load(sys.stdin)]"
    fi
done
```
Report any scottycore-bot comments before starting work.
"""


def inject_claude_section(app_path: Path) -> bool:
    """Add a ScottyCore section to the app's CLAUDE.md."""
    claude_md = app_path / "CLAUDE.md"

    if not claude_md.exists():
        print(f"  No CLAUDE.md found — creating one with ScottyCore section")
        claude_md.write_text(f"# {app_path.name}\n{CLAUDE_TEMPLATE.replace('{app_path}', str(app_path))}")
        return True

    content = claude_md.read_text()

    if CLAUDE_SECTION_MARKER in content:
        print(f"  ScottyCore section already present in CLAUDE.md")
        return False

    section = CLAUDE_TEMPLATE.replace("{app_path}", str(app_path))
    content = content.rstrip() + "\n" + section
    claude_md.write_text(content)
    print(f"  Injected ScottyCore section into CLAUDE.md")
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
    git("add", "CLAUDE.md", ".forgejo/workflows/promote-scan.yml", "pyproject.toml")

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


def push_app(app_path: Path, branch: str) -> bool:
    """Push app repo to all remotes."""
    result = subprocess.run(
        ["git", "-C", str(app_path), "push", "-u", "origin", branch],
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

    git("add", "config/apps.yaml", f".claude/agents/{app_name}-manager.md")

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
    skip_forgejo = "--skip-forgejo" in args
    scaffold_flag = "--scaffold" in args

    for i, arg in enumerate(args):
        if arg == "--name" and i + 1 < len(args):
            app_name = args[i + 1]
        if arg == "--stack" and i + 1 < len(args):
            stack = args[i + 1]
        if arg == "--github-repo" and i + 1 < len(args):
            github_repo = args[i + 1]

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

    if not skip_forgejo:
        forgejo_url = create_forgejo_repo(
            app_name,
            description=f"Scotty app: {stack}",
            default_branch=branch,
        )
        if forgejo_url:
            setup_dual_remote(app_path, forgejo_url, github_url, branch)
    else:
        print(f"  --skip-forgejo: skipping Forgejo repo creation")

    # Step 2: Register
    print(f"\nStep 2: Register in config/apps.yaml")
    register_app(app_path, app_name, stack, branch)

    # Step 3: Manager agent
    print(f"\nStep 3: Scaffold manager agent")
    scaffold_agent(app_name, app_path, stack)

    # Step 4: CLAUDE.md injection
    print(f"\nStep 4: Inject ScottyCore Pipeline section into CLAUDE.md")
    inject_claude_section(app_path)

    # Step 5a: Install pre-commit nudge hook (early-warning, local)
    print(f"\nStep 5a: Install pre-commit /promote nudge hook")
    install_promote_nudge_hook(app_path, app_name)

    # Step 5b: Install server-side promote-scan workflow (authoritative)
    print(f"\nStep 5b: Install promote-scan.yml workflow")
    install_promote_scan_workflow(app_path, app_name)

    # Step 6: Commit + push app changes
    print(f"\nStep 6: Commit and push app changes")
    committed = commit_app_changes(app_path, app_name)
    if committed:
        push_app(app_path, branch)

    # Step 7: Commit + push scottycore changes
    print(f"\nStep 7: Commit and push scottycore changes")
    commit_scottycore_changes(app_name)

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
    print(f"  - .forgejo/workflows/promote-scan.yml installed")
    print(f"  - All changes committed and pushed")
    print(f"\n  Next steps (manual):")
    print(f"  - Set FORGEJO_TOKEN secret at:")
    print(f"      {FORGEJO_BASE}/{FORGEJO_USER}/{app_name}/settings/actions/secrets")
    print(f"  - If app has root pyproject.toml, install scottycore-upgrade.yml")
    print(f"      and add {app_name} to scottycore/.forgejo/workflows/release.yml APPS")
    print()


if __name__ == "__main__":
    main()
