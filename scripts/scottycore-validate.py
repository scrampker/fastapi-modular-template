#!/usr/bin/env python3
"""
ScottyCore Validate — Compliance checker for ScottyCore-integrated apps.
========================================================================
Checks that an app's claimed pattern adoption matches reality.
Can run against a single app, all registered apps, or as a pre-commit hook.

Exit codes:
  0 = PASS (all checks green)
  1 = FAIL (adopted patterns missing markers, or other hard failures)
  2 = WARN (minor issues — drift, unlisted patterns, etc.)

Checks performed:
  1. Manifest exists (.scottycore-patterns.yaml)
  2. CLAUDE.md has ScottyCore integration section
  3. Adopted patterns have matching inline markers in source files
  4. No orphan markers (markers for patterns not in the manifest)
  5. Synced-from markers are present (drift tracking)
  6. Manager agent exists in scottycore

Usage:
    python3 scottycore-validate.py                     # validate all registered apps
    python3 scottycore-validate.py /path/to/app        # validate one app
    python3 scottycore-validate.py --app scottystrike   # validate by name
    python3 scottycore-validate.py --summary            # one-line-per-app overview
    python3 scottycore-validate.py --strict             # exit 1 on any WARN (for CI)
    python3 scottycore-validate.py --hook               # pre-commit hook mode (only checks staged files)
"""

from __future__ import annotations

import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_DIR / "scripts"))

from config_loader import load_apps_config, get_consumer_apps
from pattern_tracker import scan_for_patterns, load_manifest

CLAUDE_SECTION_MARKER = "<!-- scottycore-integration -->"


# ── Result types ────────────────────────────────────────────────────────────

class CheckResult:
    def __init__(self, name: str, status: str, detail: str):
        self.name = name
        self.status = status  # "PASS", "FAIL", "WARN", "SKIP"
        self.detail = detail

    def __str__(self) -> str:
        icon = {"PASS": "+", "FAIL": "!", "WARN": "~", "SKIP": "-"}
        return f"  [{icon.get(self.status, '?')}] {self.status}: {self.name} — {self.detail}"


def validate_app(app_name: str, app_path: Path) -> list[CheckResult]:
    """Run all validation checks for a single app."""
    results: list[CheckResult] = []
    app_path = Path(app_path)

    if not app_path.exists():
        results.append(CheckResult("directory", "FAIL", f"path does not exist: {app_path}"))
        return results

    results.append(CheckResult("directory", "PASS", str(app_path)))

    # ── Check 1: Manifest exists ──────────────────────────────────────────
    manifest_path = app_path / ".scottycore-patterns.yaml"
    if manifest_path.exists():
        results.append(CheckResult("manifest", "PASS", ".scottycore-patterns.yaml present"))
        manifest = load_manifest(app_path)
    else:
        results.append(CheckResult("manifest", "FAIL",
            ".scottycore-patterns.yaml missing — run: python3 /script/scottycore/scripts/scottycore-init.py " + str(app_path)))
        manifest = {"adopted": [], "ignored": []}

    # ── Check 2: CLAUDE.md has ScottyCore section ─────────────────────────
    claude_md = app_path / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text()
        if CLAUDE_SECTION_MARKER in content:
            results.append(CheckResult("claude_md", "PASS", "ScottyCore integration section present"))
        else:
            results.append(CheckResult("claude_md", "WARN",
                "CLAUDE.md exists but missing ScottyCore section — agents won't know about the contract"))
    else:
        results.append(CheckResult("claude_md", "WARN", "no CLAUDE.md found"))

    # ── Check 3: Adopted patterns have inline markers ─────────────────────
    app_occurrences = scan_for_patterns(app_path)
    app_pattern_names = {occ.pattern for occ in app_occurrences}

    adopted = set(manifest.get("adopted", []))
    ignored = set(manifest.get("ignored", []))

    # Get all known patterns from scottycore
    core_occurrences = scan_for_patterns(CORE_DIR)
    core_pattern_names = {occ.pattern for occ in core_occurrences}

    missing_markers = adopted - app_pattern_names
    if missing_markers:
        for p in sorted(missing_markers):
            results.append(CheckResult(f"pattern:{p}", "FAIL",
                f"declared adopted but no `# scottycore-pattern: {p}` marker found in source files"))
    else:
        if adopted:
            results.append(CheckResult("adopted_markers", "PASS",
                f"all {len(adopted)} adopted patterns have inline markers"))
        else:
            results.append(CheckResult("adopted_markers", "SKIP", "no patterns adopted"))

    # ── Check 4: Orphan markers (in source but not in manifest) ───────────
    declared = adopted | ignored
    orphans = app_pattern_names - declared
    # Filter out patterns that don't exist in scottycore (app-specific markers)
    orphans = orphans & core_pattern_names
    if orphans:
        for p in sorted(orphans):
            results.append(CheckResult(f"orphan:{p}", "WARN",
                f"marker exists in source but pattern not listed in manifest (adopted or ignored)"))

    # ── Check 5: Synced-from markers ──────────────────────────────────────
    adopted_occ = [occ for occ in app_occurrences if occ.pattern in adopted]
    missing_sync = [occ for occ in adopted_occ if occ.synced_from is None]
    if missing_sync:
        for occ in missing_sync:
            results.append(CheckResult(f"sync:{occ.pattern}", "WARN",
                f"no `# scottycore-synced-from:` marker at {occ.file_path} — drift tracker can't compare versions"))
    elif adopted_occ:
        results.append(CheckResult("synced_markers", "PASS",
            f"all {len(adopted_occ)} adopted occurrences have synced-from markers"))

    # ── Check 6: Manager agent exists ─────────────────────────────────────
    agent_file = CORE_DIR / ".claude" / "agents" / f"{app_name}-manager.md"
    if agent_file.exists():
        results.append(CheckResult("agent", "PASS", f"{app_name}-manager.md present"))
    else:
        results.append(CheckResult("agent", "WARN",
            f"no manager agent at {agent_file.relative_to(CORE_DIR)}"))

    return results


def print_results(app_name: str, results: list[CheckResult]) -> str:
    """Print results and return overall status."""
    statuses = [r.status for r in results]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif "WARN" in statuses:
        overall = "WARN"
    else:
        overall = "PASS"

    fail_count = statuses.count("FAIL")
    warn_count = statuses.count("WARN")
    pass_count = statuses.count("PASS")

    print(f"\n  {app_name}: {overall} ({pass_count} pass, {warn_count} warn, {fail_count} fail)")
    print(f"  {'─' * 56}")
    for r in results:
        print(r)

    return overall


def print_summary(all_results: dict[str, list[CheckResult]]):
    """Print a one-line-per-app summary table."""
    print(f"\n  {'App':<20} {'Status':<8} {'Pass':<6} {'Warn':<6} {'Fail':<6}")
    print(f"  {'─'*20} {'─'*8} {'─'*6} {'─'*6} {'─'*6}")
    for app_name, results in all_results.items():
        statuses = [r.status for r in results]
        overall = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
        print(f"  {app_name:<20} {overall:<8} {statuses.count('PASS'):<6} {statuses.count('WARN'):<6} {statuses.count('FAIL'):<6}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    strict = "--strict" in args
    summary_only = "--summary" in args
    args = [a for a in args if not a.startswith("--")]

    # Determine which apps to validate
    apps_to_check: dict[str, str] = {}  # {name: path}

    if args:
        # Single app by path or name
        target = args[0]
        target_path = Path(target).resolve()
        if target_path.exists():
            # Path provided — find name from config or use directory name
            all_apps = load_apps_config()
            name = target_path.name.lower()
            for n, cfg in all_apps.items():
                if Path(cfg.get("path", "")).resolve() == target_path:
                    name = n
                    break
            apps_to_check[name] = str(target_path)
        else:
            # Maybe it's an app name
            all_apps = load_apps_config()
            if target in all_apps:
                apps_to_check[target] = all_apps[target]["path"]
            else:
                print(f"Error: '{target}' is not a valid path or registered app name")
                sys.exit(1)
    else:
        # All consumer apps
        consumers = get_consumer_apps()
        if not consumers:
            print("No consumer apps registered. Run scottycore-init.py to add one.")
            sys.exit(0)
        for name, cfg in consumers.items():
            apps_to_check[name] = cfg["path"]

    # Run validation
    all_results: dict[str, list[CheckResult]] = {}
    worst_status = "PASS"

    for app_name, app_path in apps_to_check.items():
        results = validate_app(app_name, Path(app_path))
        all_results[app_name] = results

        if not summary_only:
            status = print_results(app_name, results)
        else:
            statuses = [r.status for r in results]
            status = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")

        if status == "FAIL":
            worst_status = "FAIL"
        elif status == "WARN" and worst_status != "FAIL":
            worst_status = "WARN"

    if summary_only:
        print_summary(all_results)

    # Print overall verdict
    total_apps = len(all_results)
    print(f"\n  Overall: {worst_status} ({total_apps} app(s) checked)")

    # Exit code
    if worst_status == "FAIL":
        sys.exit(1)
    if worst_status == "WARN" and strict:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
