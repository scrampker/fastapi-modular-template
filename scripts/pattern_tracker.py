#!/usr/bin/env python3
"""
ScottyCore Pattern Tracker
==========================
Reads pattern provenance markers across all Scotty repos and reports drift.

Pattern markers are inline comments in source files:

    # scottycore-pattern: auth.session_version

scottycore is the canonical source. Apps that have adopted a pattern carry
both the pattern marker AND a synced-from line:

    # scottycore-pattern: auth.session_version
    # scottycore-synced-from: a1b2c3d4

Drift is computed by comparing the synced-from commit (in the app) against
the most recent commit that touched the tagged file in scottycore.

Each app may also carry a `.scottycore-patterns.yaml` adoption manifest:

    adopted:
      - auth.session_version
      - settings.kv_hierarchy
    ignored:
      - audit.phi_data_access  # not relevant — no PHI

The watcher and the agent use this manifest to decide what to propagate.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PATTERN_TAG_RE = re.compile(r"#\s*scottycore-pattern:\s*([a-zA-Z0-9_.\-]+)")
SYNCED_FROM_RE = re.compile(r"#\s*scottycore-synced-from:\s*([a-f0-9]{7,40})")

# File extensions to scan for patterns. Comment syntax is `#` so anything
# that uses # comments works (Python, shell, YAML, Dockerfile, PowerShell).
SCANNABLE_EXTS = {".py", ".sh", ".ps1", ".psm1", ".yaml", ".yml", ".toml"}

# Directories to skip when scanning
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "data", "uploads", "logs", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "dist", "build", ".tox",
}


@dataclass
class PatternOccurrence:
    """A single file that carries a pattern marker."""
    pattern: str
    file_path: Path
    synced_from: str | None  # commit sha if recorded, None if this IS the source


@dataclass
class PatternStatus:
    """Drift status for one pattern in one repo."""
    pattern: str
    repo: str
    status: str          # "in-sync" | "drift" | "missing" | "ignored" | "source"
    detail: str
    files: list[Path] = field(default_factory=list)


# ── Scanning ─────────────────────────────────────────────────────────────────

def scan_for_patterns(repo_path: Path) -> list[PatternOccurrence]:
    """Walk a repo and collect every pattern marker."""
    occurrences: list[PatternOccurrence] = []
    if not repo_path.exists():
        return occurrences

    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCANNABLE_EXTS:
            continue
        # Skip if any parent directory is in skip list
        if any(part in SKIP_DIRS for part in path.relative_to(repo_path).parts):
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue

        # Look for pattern tags
        for match in PATTERN_TAG_RE.finditer(content):
            pattern_name = match.group(1)
            # Look for a synced-from marker within the next ~5 lines
            tail = content[match.end():match.end() + 500]
            synced_match = SYNCED_FROM_RE.search(tail)
            synced_from = synced_match.group(1) if synced_match else None
            occurrences.append(PatternOccurrence(
                pattern=pattern_name,
                file_path=path.relative_to(repo_path),
                synced_from=synced_from,
            ))

    return occurrences


# ── Manifest loading ─────────────────────────────────────────────────────────

def load_manifest(repo_path: Path) -> dict:
    """Load .scottycore-patterns.yaml from a repo. Returns adopted/ignored lists."""
    manifest_path = repo_path / ".scottycore-patterns.yaml"
    if not manifest_path.exists():
        return {"adopted": [], "ignored": []}

    # Tiny hand-rolled YAML parser to avoid pulling in PyYAML for one file
    # Only supports the simple two-list structure documented above
    adopted: list[str] = []
    ignored: list[str] = []
    current: list[str] | None = None

    for line in manifest_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "adopted:":
            current = adopted
            continue
        if stripped == "ignored:":
            current = ignored
            continue
        if stripped.startswith("- ") and current is not None:
            # Strip inline comments
            value = stripped[2:].split("#", 1)[0].strip()
            if value:
                current.append(value)

    return {"adopted": adopted, "ignored": ignored}


# ── Versioning via git ───────────────────────────────────────────────────────

def get_file_last_commit(repo_path: Path, relative_file: Path) -> str | None:
    """Get the most recent commit hash that touched a file."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", "-1", "--format=%H", "--", str(relative_file)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha if sha else None
    except subprocess.TimeoutExpired:
        return None


def get_commit_short(sha: str) -> str:
    """First 8 chars of a commit sha."""
    return sha[:8] if sha else "unknown"


# ── Drift computation ────────────────────────────────────────────────────────

def compute_drift(
    scottycore_path: Path,
    apps: dict[str, Path],
) -> dict[str, list[PatternStatus]]:
    """
    For each pattern in scottycore, compute its status in each app.
    Returns: {repo_name: [PatternStatus, ...]}
    """
    # 1. Collect all patterns in scottycore (the canonical source)
    core_occurrences = scan_for_patterns(scottycore_path)

    # Group by pattern name → list of files
    core_patterns: dict[str, list[Path]] = {}
    for occ in core_occurrences:
        core_patterns.setdefault(occ.pattern, []).append(occ.file_path)

    # For each pattern, compute its current "version" — the latest commit
    # in scottycore that touched any of its tagged files
    pattern_versions: dict[str, str | None] = {}
    for pattern_name, files in core_patterns.items():
        latest = None
        for f in files:
            sha = get_file_last_commit(scottycore_path, f)
            if sha and (latest is None or sha > latest):  # lexicographic ok for sha comparison? No, need actual ordering
                latest = sha
        # Better: ask git for the most recent commit across all the files
        if files:
            try:
                result = subprocess.run(
                    ["git", "-C", str(scottycore_path), "log", "-1", "--format=%H", "--"] + [str(f) for f in files],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    latest = result.stdout.strip() or latest
            except subprocess.TimeoutExpired:
                pass
        pattern_versions[pattern_name] = latest

    # 2. For each app, compute drift status per pattern
    results: dict[str, list[PatternStatus]] = {}

    for app_name, app_path in apps.items():
        statuses: list[PatternStatus] = []
        manifest = load_manifest(app_path)
        ignored = set(manifest["ignored"])
        adopted = set(manifest["adopted"])

        # Scan app for pattern occurrences
        app_occurrences = scan_for_patterns(app_path)
        app_patterns: dict[str, list[PatternOccurrence]] = {}
        for occ in app_occurrences:
            app_patterns.setdefault(occ.pattern, []).append(occ)

        for pattern_name, core_files in core_patterns.items():
            core_version = pattern_versions.get(pattern_name)

            if pattern_name in ignored:
                statuses.append(PatternStatus(
                    pattern=pattern_name,
                    repo=app_name,
                    status="ignored",
                    detail=f"opted out via .scottycore-patterns.yaml",
                    files=core_files,
                ))
                continue

            app_occ_list = app_patterns.get(pattern_name, [])

            if not app_occ_list:
                # App doesn't have the pattern at all
                in_adopted = pattern_name in adopted
                statuses.append(PatternStatus(
                    pattern=pattern_name,
                    repo=app_name,
                    status="missing",
                    detail=("declared in adopted list but file not found"
                            if in_adopted else "not adopted"),
                    files=[],
                ))
                continue

            # App has the pattern — check synced-from
            synced_versions = {occ.synced_from for occ in app_occ_list if occ.synced_from}

            if not synced_versions:
                statuses.append(PatternStatus(
                    pattern=pattern_name,
                    repo=app_name,
                    status="drift",
                    detail="present but no synced-from marker (untracked copy)",
                    files=[occ.file_path for occ in app_occ_list],
                ))
                continue

            # Check if any synced-from matches the current core version
            if core_version and core_version[:8] in {v[:8] for v in synced_versions}:
                statuses.append(PatternStatus(
                    pattern=pattern_name,
                    repo=app_name,
                    status="in-sync",
                    detail=f"at scottycore@{get_commit_short(core_version)}",
                    files=[occ.file_path for occ in app_occ_list],
                ))
            else:
                synced_list = ", ".join(get_commit_short(v) for v in synced_versions)
                statuses.append(PatternStatus(
                    pattern=pattern_name,
                    repo=app_name,
                    status="drift",
                    detail=f"app at {synced_list}, scottycore at {get_commit_short(core_version) if core_version else 'unknown'}",
                    files=[occ.file_path for occ in app_occ_list],
                ))

        results[app_name] = statuses

    return results


# ── Reporting ────────────────────────────────────────────────────────────────

def render_drift_report(
    drift: dict[str, list[PatternStatus]],
    scottycore_patterns: list[str],
) -> str:
    """Render a markdown drift report."""
    from datetime import datetime

    lines = [
        f"# Pattern Drift Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"**Tracked patterns in scottycore:** {len(scottycore_patterns)}",
        "",
    ]

    if not scottycore_patterns:
        lines.append("_No patterns tagged in scottycore yet. Add `# scottycore-pattern: <name>` markers to files you want to track._")
        return "\n".join(lines)

    # Summary table — one row per pattern, one column per app
    lines.append("## Summary")
    lines.append("")
    apps = sorted(drift.keys())
    header = "| Pattern | " + " | ".join(apps) + " |"
    sep = "|---" * (len(apps) + 1) + "|"
    lines.append(header)
    lines.append(sep)

    status_glyph = {
        "in-sync": "OK",
        "drift": "DRIFT",
        "missing": "missing",
        "ignored": "ignored",
        "source": "source",
    }

    for pattern_name in sorted(scottycore_patterns):
        row = [pattern_name]
        for app in apps:
            status = next(
                (s for s in drift[app] if s.pattern == pattern_name),
                None,
            )
            row.append(status_glyph.get(status.status, "?") if status else "—")
        lines.append("| " + " | ".join(row) + " |")

    # Detail section
    lines.append("")
    lines.append("## Details")
    lines.append("")

    for app in apps:
        lines.append(f"### {app}")
        lines.append("")
        for status in drift[app]:
            lines.append(f"- **{status.pattern}** — `{status.status}` — {status.detail}")
            if status.files:
                for f in status.files:
                    lines.append(f"    - `{f}`")
        lines.append("")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    """CLI entry point — generate a drift report."""
    import sys

    scottycore = Path("/script/scottycore")
    apps = {
        "scottystrike": Path("/script/scottystrike"),
        "scottyscribe": Path("/script/scottyscribe"),
        "scottyscan": Path("/script/ScottyScan"),
    }

    drift = compute_drift(scottycore, apps)
    core_patterns = sorted({
        occ.pattern for occ in scan_for_patterns(scottycore)
    })
    report = render_drift_report(drift, core_patterns)

    if "--write" in sys.argv:
        out_dir = scottycore / "data" / "drift-reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        out_file = out_dir / f"drift_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        out_file.write_text(report)
        print(f"Wrote {out_file}")
    else:
        print(report)


if __name__ == "__main__":
    main()
