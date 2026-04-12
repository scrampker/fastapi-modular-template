#!/usr/bin/env python3
"""
ScottyCore Sync Watcher — Autonomous Cross-App Fixer
=====================================================
Runs on a cron schedule (default: every 15 minutes).
Checks all Scotty repos for new commits since last check.
When new commits are found, launches a Claude Opus agent with full
write access to propagate fixes across all affected repos.

The agent:
1. Reads the new commits/diffs from the source repo
2. Determines which other repos need the same fix
3. Makes the changes, commits to a sync/* branch
4. You review and merge (or it auto-merges for low-risk fixes)

Reports are written to /script/scottycore/data/sync-reports/
Log tailed via sync-pane.sh in a tmux pane.

Usage:
    python3 sync-watcher.py                            # Normal run
    python3 sync-watcher.py --dry-run                  # Detect changes, don't invoke agent
    python3 sync-watcher.py --force                    # Re-baseline all repos
    python3 sync-watcher.py --tail                     # Tail reports (for tmux pane)
    python3 sync-watcher.py --report-only              # Analyze but don't make changes
    python3 sync-watcher.py --model sonnet             # Override model (default: opus)
    python3 sync-watcher.py --drift-report             # Print pattern drift across repos
    python3 sync-watcher.py --drift-report --write     # Write drift report to data/drift-reports/

Env vars:
    SYNC_TMUX_TARGET    tmux pane for notifications (e.g. "main:0.1")
    SYNC_CLAUDE_MODEL   Model override (default: opus)
    SYNC_MAX_BUDGET     USD cap per run (default: 2.00)
    SYNC_AUTO_MERGE     Set to "1" to auto-merge sync branches (default: on)
    SYNC_HUBITAT_CONFIG  Path to hubitat JSON config (default: ~/.config/scottycore-hubitat.json)
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

REPOS = {
    "scottycore": {
        "path": "/script/scottycore",
        "stack": "FastAPI (shared template)",
        "branch": "master",
    },
    "scottystrike": {
        "path": "/script/scottystrike",
        "stack": "FastAPI (from scottycore)",
        "branch": "master",
    },
    "scottyscribe": {
        "path": "/script/scottyscribe",
        "stack": "Flask + WhisperX + GPU",
        "branch": "main",
    },
    "scottyscan": {
        "path": "/script/ScottyScan",
        "stack": "PowerShell + webapp",
        "branch": "master",
    },
}

CORE_DIR = Path("/script/scottycore")
REPORTS_DIR = CORE_DIR / "data" / "sync-reports"
STATE_FILE = CORE_DIR / "data" / "sync-watcher-state.json"
LOG_FILE = CORE_DIR / "data" / "sync-watcher.log"

LOCK_FILE = CORE_DIR / "data" / "sync-watcher.lock"

TMUX_TARGET = os.environ.get("SYNC_TMUX_TARGET")
CLAUDE_MODEL = os.environ.get("SYNC_CLAUDE_MODEL", "opus")
MAX_BUDGET = os.environ.get("SYNC_MAX_BUDGET", "2.00")
AUTO_MERGE = os.environ.get("SYNC_AUTO_MERGE", "1") == "1"  # default ON
HUBITAT_CONFIG = Path(os.environ.get(
    "SYNC_HUBITAT_CONFIG",
    os.path.expanduser("~/.config/scottycore-hubitat.json"),
))


# ── Helpers ──────────────────────────────────────────────────────────────────

def acquire_lock() -> bool:
    """Prevent overlapping runs. Returns True if lock acquired."""
    if LOCK_FILE.exists():
        # Check if the PID is still alive
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # signal 0 = check existence
            return False  # process still running
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock, take over
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove lock file."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def git(repo_path: str, *args, timeout: int = 30) -> str | None:
    """Run a git command, return stdout or None on error."""
    cmd = ["git", "-C", repo_path] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log(f"  git error in {repo_path}: {r.stderr.strip()}")
            return None
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"  git timeout in {repo_path}")
        return None


def get_head(repo_path: str) -> str | None:
    return git(repo_path, "rev-parse", "HEAD")


def get_default_branch(repo_path: str) -> str:
    """Detect default branch name."""
    head_ref = git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD")
    if head_ref:
        return head_ref.split("/")[-1]
    # Fallback: check common names
    for branch in ["main", "master"]:
        if git(repo_path, "rev-parse", "--verify", f"refs/heads/{branch}") is not None:
            return branch
    return "master"


def get_commits_since(repo_path: str, since_sha: str) -> list[dict]:
    """Get commits after since_sha."""
    raw = git(repo_path, "log", "--format=%H|%s|%an|%aI", f"{since_sha}..HEAD")
    if not raw:
        return []
    commits = []
    for line in raw.split("\n"):
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "sha": parts[0],
                "subject": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return commits


def get_diff(repo_path: str, since_sha: str, stat_only: bool = False) -> str:
    """Get diff since last check."""
    args = ["diff", f"{since_sha}..HEAD", "--no-color"]
    if stat_only:
        args.insert(1, "--stat")
    result = git(repo_path, *args, timeout=60)
    if not result:
        return ""
    # Truncate large diffs for the prompt
    if not stat_only and len(result) > 15000:
        return result[:15000] + "\n\n... (truncated at 15000 chars — read full files as needed)"
    return result


def read_manifest_for_repo(repo_path: str) -> dict:
    """Load .scottycore-patterns.yaml from a repo (or return empty if missing)."""
    manifest_path = Path(repo_path) / ".scottycore-patterns.yaml"
    if not manifest_path.exists():
        return {"adopted": [], "ignored": []}
    adopted: list[str] = []
    ignored: list[str] = []
    current: list[str] | None = None
    for line in manifest_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s == "adopted:":
            current = adopted; continue
        if s == "ignored:":
            current = ignored; continue
        if s.startswith("- ") and current is not None:
            value = s[2:].split("#", 1)[0].strip()
            if value:
                current.append(value)
    return {"adopted": adopted, "ignored": ignored}


def build_agent_prompt(source_repos: list[str], changes: dict, report_only: bool) -> str:
    """Build the full prompt for the Claude agent."""

    # Describe all changes
    change_blocks = []
    for repo_name, info in changes.items():
        commits_text = "\n".join(
            f"  - `{c['sha'][:8]}` {c['subject']} ({c['author']}, {c['date']})"
            for c in info["commits"]
        )
        change_blocks.append(f"""### {repo_name} (`{info['path']}`)
Stack: {info['stack']}
**{len(info['commits'])} new commit(s):**
{commits_text}

**Files changed:**
```
{info['diff_stat']}
```

**Diff:**
```diff
{info['diff_content']}
```
""")

    # Build target repo descriptions — ONLY repos that DIDN'T change are targets
    # Source repos (the ones that changed) must NEVER be modified
    target_repos = []
    for name, config in REPOS.items():
        if name not in changes:
            manifest = read_manifest_for_repo(config["path"])
            adopted_str = (", ".join(manifest["adopted"]) or "(none declared)")
            ignored_str = (", ".join(manifest["ignored"]) or "(none)")
            target_repos.append(
                f"- **{name}** (`{config['path']}`) — {config['stack']}, branch: `{config['branch']}`\n"
                f"    - adopted patterns: {adopted_str}\n"
                f"    - ignored patterns: {ignored_str}"
            )

    if not target_repos:
        # All repos changed — each is both source and target for the others
        # This is complex; for safety, just report
        targets_text = "(all repos had changes simultaneously — report-only mode forced for safety)"
        report_only = True
    else:
        targets_text = "\n".join(target_repos)

    mode_instructions = ""
    if report_only:
        mode_instructions = """
## MODE: REPORT ONLY
Do NOT make any changes. Only analyze and write the sync report.
"""
    else:
        mode_instructions = f"""
## MODE: AUTONOMOUS FIX
You have full write access to all repos. When you identify a fix that should propagate:

1. **Read the target repo's relevant files** to understand their current state
2. **Create a sync branch**: `git -C <repo_path> checkout -b sync/<source>-<short_desc>-<date>`
3. **Make the fix** using Edit/Write tools — adapt to each repo's stack:
   - ScottyStrike: FastAPI, same patterns as ScottyCore
   - ScottyScribe: Flask, adapt conceptually (not copy-paste)
   - ScottyScan: PowerShell scanner + webapp, usually only webapp is relevant
4. **Commit**: `git -C <repo_path> add -A && git -C <repo_path> commit -m "sync: <description> (from <source>)"`
5. **Switch back**: `git -C <repo_path> checkout <default_branch>`
{'''6. **Auto-merge + push**: After committing on the sync branch, merge it into the default branch and push:
   ```
   git -C <repo_path> checkout <default_branch>
   git -C <repo_path> merge sync/<branch_name> --no-edit
   git -C <repo_path> branch -d sync/<branch_name>
   git -C <repo_path> push origin <default_branch>
   ```
   EXCEPTION: If the fix touches database schemas, API contracts, or you are uncertain — leave the sync branch unmerged and tag the report with `NEEDS_HUMAN: <reason>`. The watcher will send a notification.''' if AUTO_MERGE else "6. **Leave the branch** for the user to review and merge."}

PATTERN ADOPTION MANIFESTS:
Each target repo above lists `adopted` and `ignored` patterns from its `.scottycore-patterns.yaml`.
- If a fix relates to a pattern listed under that repo's `ignored:` list, **SKIP** that repo entirely for this fix and note it in the report.
- If a fix relates to a pattern listed under `adopted:`, the repo expects the fix — apply it.
- If a fix isn't tied to any tracked pattern, use your judgment as before.
- Pattern markers in source files look like `# scottycore-pattern: <name>`. When you sync a fix into a target repo, also add a `# scottycore-synced-from: <commit-sha>` line so drift tracking stays accurate.

CRITICAL RULES — VIOLATION OF THESE CAUSES INFINITE LOOPS:
- **SOURCE REPOS ARE READ-ONLY**: The following repos triggered this run and MUST NOT be modified: {", ".join(f"`{r}` (`{changes[r]['path']}`)" for r in source_repos)}
- You may ONLY create branches and commits in the TARGET repos listed above
- If you find yourself about to edit a file in {", ".join(f"`{changes[r]['path']}`" for r in source_repos)} — STOP. That is a source repo.
- NEVER force-push or rebase
- If you're unsure whether a fix applies, create the branch but note uncertainty in the commit message
- If the target repo's code has diverged significantly, skip and note it in the report
- Always switch back to the default branch when done with a repo
- All commit messages MUST start with `sync:` prefix
"""

    return f"""You are the ScottyCore autonomous sync agent. Your job is to propagate bug fixes, improvements, and patterns across the Scotty app family.

## Context

The Scotty apps share a common framework (ScottyCore). When a developer commits a fix to one app, the same bug often exists in the other apps. You detect these shared bugs and fix them.

**ScottyCore** (`/script/scottycore`) is the shared template — auth, tenants, users, audit, settings, service registry, middleware, deployment.

## Recent Changes Detected

{"".join(change_blocks)}

## Target Repos (check these for the same issues)

{targets_text}
{mode_instructions}

## Analysis Steps

1. **Understand the fix**: What bug was fixed? What pattern was improved? Is it domain-specific or generic?
2. **Categorize**:
   - `core-extract`: Generic improvement that belongs in ScottyCore
   - `cross-app-fix`: Bug that likely exists in other apps
   - `security-fix`: Security issue — highest priority, check all repos
   - `pattern-update`: Architectural pattern change (settings, auth, etc.)
   - `domain-specific`: Only relevant to the source app — skip
3. **For each target repo**: Read the equivalent code, determine if the same issue exists, and fix it
4. **Write a sync report** to `/script/scottycore/data/sync-reports/sync_{datetime.now().strftime("%Y%m%d_%H%M%S")}.md`

## Report Format

Write the report file with this structure:
```markdown
# Sync Report — <date>

## Trigger
<which repo, which commits>

## Fixes Applied
- [ ] <repo>: <description> (branch: sync/<name>)
- [ ] <repo>: <description> (branch: sync/<name>)

## Skipped
- <repo>: <reason>

## Notes
<any warnings, uncertainties, or manual steps needed>
```

## Safety Rules
- Domain-specific code (parser logic, transcription pipeline, PowerShell scanner plugins) does NOT propagate
- Auth, security, error handling, deployment, UI patterns, database patterns DO propagate
- When ScottyScribe (Flask) needs a fix from a FastAPI app, adapt the pattern — don't copy FastAPI code into Flask
- When ScottyScan needs a fix, usually only the `webapp/` directory is relevant
- If ScottyCore itself needs updating (pattern extraction), make changes there too

Now analyze the changes and take action."""


def run_agent(prompt: str, report_only: bool) -> tuple[str, int]:
    """Launch Claude agent with appropriate permissions."""

    allowed_tools = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]
    if report_only:
        allowed_tools = ["Read", "Grep", "Glob", "Bash"]

    # Use absolute path — cron has minimal PATH
    claude_bin = "/root/.local/bin/claude"
    cmd = [
        claude_bin, "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--max-budget-usd", MAX_BUDGET,
        "--allowedTools", ",".join(allowed_tools),
        "--add-dir", "/script/scottystrike",
        "--add-dir", "/script/scottyscribe",
        "--add-dir", "/script/ScottyScan",
        "--no-session-persistence",
    ]

    log(f"  Launching Claude {CLAUDE_MODEL} agent ({'report-only' if report_only else 'autonomous fix'})...")
    log(f"  Budget cap: ${MAX_BUDGET}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
            cwd=str(CORE_DIR),
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "AGENT_ERROR: Claude CLI timed out after 10 minutes", 1


def check_sync_branches() -> dict:
    """Check all repos for pending sync branches."""
    pending = {}
    for name, config in REPOS.items():
        branches = git(config["path"], "branch", "--list", "sync/*")
        if branches:
            pending[name] = [b.strip().lstrip("* ") for b in branches.split("\n") if b.strip()]
    return pending


def notify_tmux(message: str):
    if not TMUX_TARGET:
        return
    try:
        subprocess.run(
            ["tmux", "display-message", "-t", TMUX_TARGET, f"[SyncWatch] {message}"],
            timeout=5, capture_output=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def notify_hubitat(message: str, priority: str = "P4"):
    """Send push notification via Hubitat Maker API."""
    if not HUBITAT_CONFIG.exists():
        return
    try:
        config = json.loads(HUBITAT_CONFIG.read_text())
        url = f"{config['url']}?access_token={config['access_token']}"
        alarm = "true" if priority in ("P1", "P2") else "false"
        payload = json.dumps({
            "priority": priority,
            "destination": "text",
            "alarm": alarm,
            "message": f"CLAUDE: {message}",
        })
        proc = subprocess.run(
            ["curl", "-s", url, "-H", "Content-Type: application/json", "--data", payload],
            timeout=15, capture_output=True, text=True,
        )
        if proc.returncode == 0 and '"result":"OK"' in proc.stdout:
            log(f"  Hubitat notification sent")
        else:
            log(f"  Hubitat failed: {proc.stdout[:200]}")
    except (json.JSONDecodeError, KeyError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"  Hubitat error: {e}")


def create_github_issue(repo_path: str, title: str, body: str):
    """Create a GitHub issue in the affected repo with sync-agent label."""
    try:
        # Ensure label exists (idempotent)
        subprocess.run(
            ["gh", "label", "create", "sync-agent",
             "--description", "Created by ScottyCore sync watcher",
             "--color", "d93f0b", "--force"],
            cwd=repo_path, capture_output=True, text=True, timeout=15,
        )
        # Create issue
        result = subprocess.run(
            ["gh", "issue", "create",
             "--title", title,
             "--body", body,
             "--label", "sync-agent"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            log(f"  GitHub issue created: {issue_url}")
            return issue_url
        else:
            log(f"  gh issue create failed: {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"  GitHub issue error: {e}")
    return None


def notify_all(message: str, body: str = "", priority: str = "P4"):
    """Send notification via all configured channels."""
    notify_tmux(message)
    notify_hubitat(body or message, priority)


def tail_reports():
    """Tail mode for a dedicated tmux pane."""
    print("=" * 60)
    print("  ScottyCore Sync Watcher — Live Feed")
    print("=" * 60)
    print(f"  Reports: {REPORTS_DIR}")
    print(f"  Log:     {LOG_FILE}")
    print()

    # Show pending sync branches
    pending = check_sync_branches()
    if pending:
        print("  PENDING SYNC BRANCHES:")
        for repo, branches in pending.items():
            for b in branches:
                print(f"    {repo}: {b}")
        print()

    # Show last report
    reports = sorted(REPORTS_DIR.glob("sync_*.md"))
    if reports:
        print(f"--- Last report: {reports[-1].name} ---")
        print(reports[-1].read_text())
        print()
        print("--- Waiting for new activity... ---\n")

    # Tail log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.touch()
    try:
        subprocess.run(["tail", "-f", str(LOG_FILE)])
    except KeyboardInterrupt:
        print("\nStopped.")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_drift_report(write_to_file: bool):
    """Generate a pattern drift report across all repos."""
    # Import here to avoid hard dep at module load
    sys.path.insert(0, str(Path(__file__).parent))
    import pattern_tracker

    scottycore = Path("/script/scottycore")
    apps = {
        name: Path(cfg["path"])
        for name, cfg in REPOS.items()
        if name != "scottycore"
    }
    drift = pattern_tracker.compute_drift(scottycore, apps)
    core_patterns = sorted({
        occ.pattern for occ in pattern_tracker.scan_for_patterns(scottycore)
    })
    report = pattern_tracker.render_drift_report(drift, core_patterns)

    if write_to_file:
        out_dir = CORE_DIR / "data" / "drift-reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"drift_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        out_file.write_text(report)
        log(f"Drift report written: {out_file}")
        notify_all(f"Drift report ready: {out_file.name}")
    else:
        print(report)


def main():
    args = sys.argv[1:]

    if "--tail" in args:
        tail_reports()
        return

    if "--drift-report" in args:
        run_drift_report(write_to_file="--write" in args)
        return

    dry_run = "--dry-run" in args
    force = "--force" in args
    report_only = "--report-only" in args

    # CLI model override
    if "--model" in args:
        idx = args.index("--model")
        if idx + 1 < len(args):
            global CLAUDE_MODEL
            CLAUDE_MODEL = args[idx + 1]

    if not acquire_lock():
        log("Another sync-watcher is still running, skipping this cycle.")
        return

    try:
        _main_inner(args, dry_run, force, report_only)
    finally:
        release_lock()


def _main_inner(args: list, dry_run: bool, force: bool, report_only: bool):
    log("=" * 50)
    log("Sync watcher starting...")
    state = load_state() if not force else {}

    # ── Detect changes ───────────────────────────────────────────────────
    changes = {}
    new_state = {}

    for repo_name, config in REPOS.items():
        repo_path = config["path"]
        if not Path(repo_path).exists():
            log(f"  {repo_name}: directory not found, skipping")
            continue

        last_sha = state.get(repo_name)
        current_sha = get_head(repo_path)
        if not current_sha:
            continue

        new_state[repo_name] = current_sha

        if last_sha is None:
            log(f"  {repo_name}: first run, recording HEAD {current_sha[:8]}")
            continue

        if last_sha == current_sha:
            log(f"  {repo_name}: no new commits")
            continue

        commits = get_commits_since(repo_path, last_sha)
        if not commits:
            log(f"  {repo_name}: HEAD changed but no parseable commits (rebase?)")
            new_state[repo_name] = current_sha
            continue

        # Skip commits made by the sync agent (avoid infinite loops)
        # Match: "sync:" prefix, or authored by the headless agent
        def is_sync_commit(c: dict) -> bool:
            if c["subject"].startswith("sync:"):
                return True
            if c["subject"].startswith("sync("):
                return True
            # Catch any commit with "(from " in subject — agent convention
            if "(from scotty" in c["subject"].lower():
                return True
            return False

        non_sync_commits = [c for c in commits if not is_sync_commit(c)]
        if not non_sync_commits:
            log(f"  {repo_name}: {len(commits)} commit(s) but all are sync commits, skipping")
            continue

        changes[repo_name] = {
            "path": repo_path,
            "stack": config["stack"],
            "commits": non_sync_commits,
            "diff_stat": get_diff(repo_path, last_sha, stat_only=True),
            "diff_content": get_diff(repo_path, last_sha),
            "prev_sha": last_sha,
            "current_sha": current_sha,
        }
        log(f"  {repo_name}: {len(non_sync_commits)} new commit(s)")

    if not changes:
        log("No changes detected. Done.")
        save_state(new_state)
        return

    total = sum(len(info["commits"]) for info in changes.values())
    repos_list = ", ".join(changes.keys())
    log(f"Found {total} new commit(s) across: {repos_list}")

    if dry_run:
        log("DRY RUN — listing changes only:")
        for repo_name, info in changes.items():
            for c in info["commits"]:
                log(f"  {repo_name}: {c['sha'][:8]} {c['subject']}")
        save_state(new_state)
        return

    # ── Launch agent ─────────────────────────────────────────────────────
    prompt = build_agent_prompt(list(changes.keys()), changes, report_only)
    output, returncode = run_agent(prompt, report_only)

    if returncode != 0:
        log(f"  Agent exited with code {returncode}")
        log(f"  Output: {output[:500]}")
        save_state(new_state)
        notify_all(
            f"Sync agent error ({repos_list})",
            f"Sync agent failed (exit {returncode}) processing {total} commits in {repos_list}.\n\n{output[:500]}",
            priority="P2",
        )
        return

    log(f"Agent completed. Output length: {len(output)} chars")

    # ── Check if agent flagged anything as needing human intervention ────
    if "NEEDS_HUMAN" in output:
        needs_human_lines = [
            line.strip() for line in output.split("\n")
            if "NEEDS_HUMAN" in line
        ]
        reason = "\n".join(needs_human_lines) or "Agent flagged manual intervention needed"
        log(f"  NEEDS_HUMAN flagged: {reason}")

        # Create GitHub issues in affected repos
        issue_body = (
            f"## Sync Agent Needs Help\n\n"
            f"The automated sync watcher processed commits in **{repos_list}** "
            f"but couldn't complete the sync autonomously.\n\n"
            f"### What the agent said\n\n{reason}\n\n"
            f"### Source commits\n\n"
        )
        for repo_name, info in changes.items():
            for c in info["commits"]:
                issue_body += f"- `{repo_name}` {c['sha'][:8]} {c['subject']}\n"
        issue_body += (
            f"\n### What to do\n\n"
            f"Open a Claude Code session in the affected repo and resolve manually. "
            f"Close this issue when done.\n\n"
            f"---\n*Created by sync-watcher.py*"
        )

        # Create issue in scottycore (central) and any target repos that have pending branches
        issue_urls = []
        url = create_github_issue(str(CORE_DIR), f"sync-agent: needs human — {repos_list}", issue_body)
        if url:
            issue_urls.append(url)

        notify_all(
            f"Sync needs your attention ({repos_list})",
            f"Sync agent needs help with {repos_list}. {reason}. GitHub issue created.",
            priority="P2",
        )

    # ── Check for sync branches created ──────────────────────────────────
    pending = check_sync_branches()
    if pending:
        branch_count = sum(len(b) for b in pending.values())
        log(f"Sync branches created: {branch_count}")
        for repo, branches in pending.items():
            for b in branches:
                log(f"  {repo}: {b}")
        if AUTO_MERGE:
            notify_all(f"{branch_count} sync branch(es) pending — auto-merge is on but agent left branches (may need review)")
        else:
            notify_all(f"{branch_count} sync branch(es) ready for review")
    else:
        log("No sync branches created (changes applied directly or no cross-app fixes needed)")
        notify_all(f"Checked {total} commits — sync complete")

    # ── Ensure all repos are back on default branch ──────────────────────
    for repo_name, config in REPOS.items():
        current_branch = git(config["path"], "branch", "--show-current")
        if current_branch and current_branch.startswith("sync/"):
            log(f"  WARNING: {repo_name} still on sync branch {current_branch}, switching back")
            git(config["path"], "checkout", config["branch"])

    save_state(new_state)
    log("Done.")
    log("=" * 50)


if __name__ == "__main__":
    main()
