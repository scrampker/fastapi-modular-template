#!/usr/bin/env python3
"""
ScottyCore Init — Onboard a new app into the ScottyCore ecosystem.
===================================================================
Full onboarding pipeline. Creates everything an app needs to participate
in cross-app sync, pattern tracking, drift detection, and validation.

What it does:
  1. Creates Forgejo repo + sets up dual-remote push (Forgejo + GitHub)
  2. Adds the app to config/apps.yaml
  3. Generates .scottycore-patterns.yaml (interactive pattern selection)
  4. Scaffolds a manager agent in .claude/agents/<app>-manager.md
  5. Injects a ScottyCore section into the app's CLAUDE.md
  6. Commits the generated files in the app repo
  7. Pushes to both remotes
  8. Commits the scottycore-side changes (apps.yaml, agent)
  9. Runs scottycore-validate.py to show initial compliance status

Usage:
    python3 scripts/scottycore-init.py /path/to/app                # interactive
    python3 scripts/scottycore-init.py /path/to/app --name myapp   # explicit name
    python3 scripts/scottycore-init.py /path/to/app --adopt-all    # adopt all patterns
    python3 scripts/scottycore-init.py /path/to/app --adopt-none   # start with empty manifest
    python3 scripts/scottycore-init.py /path/to/app --stack "Flask" # set stack description
    python3 scripts/scottycore-init.py /path/to/app --skip-forgejo # skip Forgejo repo creation
    python3 scripts/scottycore-init.py /path/to/app --github-repo scrampker/MyApp  # explicit GitHub repo
    python3 scripts/scottycore-init.py --list                       # list registered apps
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
from pattern_tracker import scan_for_patterns

# ── Infrastructure constants ──────────────────────────────────────────────────

FORGEJO_BASE = "https://forgejo.scotty.consulting"
FORGEJO_USER = "scotty"
FORGEJO_TOKEN_PATH = Path.home() / ".config" / "forgejo-token"

GITHUB_USER = "scrampker"

# ── All known patterns from scottycore ────────────────────────────────────────


def discover_core_patterns() -> list[str]:
    """Scan scottycore for all pattern markers and return sorted unique names."""
    occurrences = scan_for_patterns(CORE_DIR)
    return sorted({occ.pattern for occ in occurrences})


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


# ── Step 3: Generate .scottycore-patterns.yaml ───────────────────────────────


def generate_manifest(
    app_path: Path,
    patterns: list[str],
    adopted: list[str],
    ignored: list[str],
) -> bool:
    """Write .scottycore-patterns.yaml to the app root."""
    manifest_path = app_path / ".scottycore-patterns.yaml"
    if manifest_path.exists():
        print(f"  .scottycore-patterns.yaml already exists — skipping")
        return False

    app_name = app_path.name
    lines = [
        f"# ScottyCore Pattern Adoption Manifest — {app_name}",
        f"# Generated by scottycore-init.py",
        "",
        "adopted:",
    ]
    for p in adopted:
        lines.append(f"  - {p}")

    lines.append("")
    lines.append("ignored:")
    for p in ignored:
        lines.append(f"  - {p}")

    unlisted = [p for p in patterns if p not in adopted and p not in ignored]
    if unlisted:
        lines.append("")
        lines.append("# Unlisted patterns (decide later):")
        for p in unlisted:
            lines.append(f"#   - {p}")

    manifest_path.write_text("\n".join(lines) + "\n")
    print(f"  Generated {manifest_path}")
    return True


# ── Step 4: Scaffold manager agent ──────────────────────────────────────────


def scaffold_agent(app_name: str, app_path: Path, stack: str) -> bool:
    """Create .claude/agents/<app>-manager.md in scottycore."""
    agents_dir = CORE_DIR / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{app_name}-manager.md"

    if agent_file.exists():
        print(f"  Agent already exists: {agent_file.name}")
        return False

    content = f"""# {app_name} Manager Agent

## Role
Dedicated manager for **{app_name}** (`{app_path}`).
Stack: {stack}.

## Responsibilities

### 1. Bug Fix Relevance Assessment
When a bug is fixed in another Scotty app or in scottycore:
- Check if {app_name} has similar patterns in its codebase
- Report RELEVANT (with specific files/lines) or NOT RELEVANT (with reasoning)
- Core template code (auth, tenants, users, audit, settings, service registry) is almost certainly relevant if {app_name} adopts those patterns

### 2. Core Sync Assessment
When scottycore changes:
- Compare changed core files against {app_name}'s versions
- Respect the `.scottycore-patterns.yaml` manifest — skip ignored patterns
- Flag safe vs. conflicting changes
- Produce a sync plan

### 3. Feature Implementation
When implementing features in {app_name}:
- Check scottycore for existing pattern coverage first
- Follow the app's existing architecture and idioms
- Delegate to sub-agents as needed:
  - UX sub-agent for frontend/UI work
  - PM sub-agent for GitHub Issues + milestones
  - DEV sub-agent for backend implementation

## Key Directories
- App root: `{app_path}`
- ScottyCore: `/script/scottycore`
- Patterns manifest: `{app_path}/.scottycore-patterns.yaml`

## Rules
- NEVER modify scottycore from this agent — changes flow the other direction
- Always check the patterns manifest before propagating a fix
- Adapt patterns to {app_name}'s stack ({stack}), don't copy-paste from FastAPI if the app uses a different framework
"""

    agent_file.write_text(content)
    print(f"  Created agent: {agent_file.name}")
    return True


# ── Step 5: Inject ScottyCore section into app CLAUDE.md ────────────────────

CLAUDE_SECTION_MARKER = "<!-- scottycore-integration -->"

CLAUDE_TEMPLATE = """
<!-- scottycore-integration -->
## ScottyCore Integration

This app is part of the **Scotty app family** and adopts patterns from
[ScottyCore](/script/scottycore), the shared core framework.

### What This Means for You (the Agent)

- **Check `.scottycore-patterns.yaml`** in this repo's root before modifying
  code that implements a ScottyCore pattern. It lists which patterns this app
  has adopted vs. explicitly opted out of.
- **Pattern markers** in source files look like `# scottycore-pattern: <name>`.
  If you modify code near a marker, update the `# scottycore-synced-from: <sha>`
  line to reflect the commit.
- **Don't reinvent shared infrastructure.** If auth, audit, settings, or
  middleware already exists in scottycore, adopt or adapt it rather than
  building from scratch.
- **Generic improvements go upstream.** If you build something that would
  benefit other Scotty apps, flag it for extraction to scottycore rather than
  keeping it app-specific.

### Validation

Run from the scottycore directory to check this app's compliance:

```bash
python3 /script/scottycore/scripts/scottycore-validate.py {app_path}
```

### Cross-App Sync

The sync watcher runs every 15 minutes. When commits land in any Scotty repo,
it launches an agent to propagate relevant fixes to the other apps. The agent
reads this app's `.scottycore-patterns.yaml` to decide what to sync.
<!-- /scottycore-integration -->
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


def commit_app_changes(app_path: Path, app_name: str) -> bool:
    """Commit the generated files in the app repo."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(app_path)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    # Stage the files we generated
    git("add", ".scottycore-patterns.yaml", "CLAUDE.md")

    # Check if there's anything to commit
    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No changes to commit in {app_name}")
        return False

    result = git("commit", "-m", f"feat: integrate with ScottyCore ecosystem\n\n"
                 f"Added .scottycore-patterns.yaml and ScottyCore integration\n"
                 f"section to CLAUDE.md. Generated by scottycore-init.py.")

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


# ── Interactive pattern selection ────────────────────────────────────────────


def select_patterns_interactive(patterns: list[str]) -> tuple[list[str], list[str]]:
    """Ask the user which patterns to adopt."""
    print("\n  Available ScottyCore patterns:")
    for i, p in enumerate(patterns, 1):
        print(f"    {i:2d}. {p}")

    print("\n  For each pattern, enter:")
    print("    a = adopt (sync watcher will keep it in sync)")
    print("    i = ignore (explicitly opt out)")
    print("    s = skip (decide later)")
    print()

    adopted = []
    ignored = []
    for p in patterns:
        while True:
            choice = input(f"    {p} [a/i/s]: ").strip().lower()
            if choice in ("a", "i", "s", ""):
                break
            print("      Invalid choice. Enter a, i, or s.")
        if choice == "a":
            adopted.append(p)
        elif choice == "i":
            ignored.append(p)

    return adopted, ignored


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
    if not app_path.exists():
        print(f"Error: path does not exist: {app_path}")
        sys.exit(1)

    # Parse flags
    app_name = None
    stack = None
    github_repo = None
    adopt_all = "--adopt-all" in args
    adopt_none = "--adopt-none" in args
    skip_forgejo = "--skip-forgejo" in args

    for i, arg in enumerate(args):
        if arg == "--name" and i + 1 < len(args):
            app_name = args[i + 1]
        if arg == "--stack" and i + 1 < len(args):
            stack = args[i + 1]
        if arg == "--github-repo" and i + 1 < len(args):
            github_repo = args[i + 1]

    if app_name is None:
        app_name = app_path.name.lower().replace(" ", "-")
    if stack is None:
        stack = detect_stack(app_path)

    branch = detect_branch(app_path)
    patterns = discover_core_patterns()

    print(f"\n{'='*60}")
    print(f"  ScottyCore Init — Onboarding '{app_name}'")
    print(f"{'='*60}")
    print(f"  Path:    {app_path}")
    print(f"  Stack:   {stack}")
    print(f"  Branch:  {branch}")
    print(f"  Patterns available: {len(patterns)}")
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

    # Step 3: Pattern selection + manifest
    print(f"\nStep 3: Generate .scottycore-patterns.yaml")
    if adopt_all:
        adopted, ignored = patterns[:], []
        print(f"  --adopt-all: adopting all {len(patterns)} patterns")
    elif adopt_none:
        adopted, ignored = [], []
        print(f"  --adopt-none: starting with empty manifest")
    else:
        adopted, ignored = select_patterns_interactive(patterns)

    generate_manifest(app_path, patterns, adopted, ignored)

    # Step 4: Manager agent
    print(f"\nStep 4: Scaffold manager agent")
    scaffold_agent(app_name, app_path, stack)

    # Step 5: CLAUDE.md injection
    print(f"\nStep 5: Inject ScottyCore section into CLAUDE.md")
    inject_claude_section(app_path)

    # Step 6: Commit + push app changes
    print(f"\nStep 6: Commit and push app changes")
    committed = commit_app_changes(app_path, app_name)
    if committed:
        push_app(app_path, branch)

    # Step 7: Commit + push scottycore changes
    print(f"\nStep 7: Commit and push scottycore changes")
    commit_scottycore_changes(app_name)

    # Step 8: Validation
    print(f"\nStep 8: Initial compliance check")
    validate_script = CORE_DIR / "scripts" / "scottycore-validate.py"
    if validate_script.exists():
        subprocess.run([sys.executable, str(validate_script), str(app_path)])
    else:
        print(f"  (scottycore-validate.py not found yet — run manually later)")

    print(f"\n{'='*60}")
    print(f"  Done! '{app_name}' is now part of the ScottyCore ecosystem.")
    print(f"{'='*60}")
    print(f"\n  What happened:")
    print(f"  - Forgejo repo created at {FORGEJO_BASE}/{FORGEJO_USER}/{app_name}")
    print(f"  - Dual-remote configured (push to Forgejo + GitHub)")
    print(f"  - Registered in config/apps.yaml")
    print(f"  - .scottycore-patterns.yaml generated in app root")
    print(f"  - Manager agent created in scottycore")
    print(f"  - ScottyCore section injected into CLAUDE.md")
    print(f"  - All changes committed and pushed")
    print(f"\n  The sync watcher will start tracking this app automatically.")
    print()


if __name__ == "__main__":
    main()
