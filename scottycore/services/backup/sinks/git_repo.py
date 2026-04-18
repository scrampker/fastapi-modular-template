"""GitRepoSink — push/pull backup bundles to any git remote using LFS.

Forge-agnostic: works against Forgejo, GitHub, GitLab, Azure DevOps, Gitea,
or any `git` server that supports LFS. Authentication is whatever git is
already configured for (credential helper, SSH keys) — this sink never
injects credentials of its own.

Layout (configurable via ``path_template``)::

    <path_template>/{timestamp}-{kind}.tar.gz.gpg
    <path_template>/{timestamp}-{kind}.meta.json

The default template mirrors LocalDiskSink::

    snapshots/{app_slug}/{scope}/[{tenant_slug}/]

Each ``put`` lands a commit; ``list_snapshots`` walks the working tree and
reads the ``.meta.json`` sidecars. ``delete`` lands a ``git rm`` commit.

Git LFS
-------
Enabled by default for ``*.gpg``, ``*.tar``, and ``*.tar.gz``. On first
use the sink writes a ``.gitattributes`` and runs ``git lfs install`` in
the clone. If the forge doesn't have LFS configured, the first push will
fail loudly — set ``lfs_enabled=False`` to store blobs as plain git objects
(fine for small deployments, eats repo storage at multi-MB scale).

Concurrency
-----------
An asyncio.Lock serialises put/delete within a single process. If two
processes push to the same repo concurrently, the second push will
conflict on non-fast-forward — the sink retries once after ``git pull
--rebase``.

Offline resilience
------------------
If ``git push`` fails (remote unreachable, auth failure, non-ff) the
commit stays local and the sink logs a warning. The next successful
operation attempts to push all pending local commits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkError,
    SinkNotFoundError,
    SinkWriteResult,
    SnapshotEntry,
    StorageSink,
)

_log = logging.getLogger(__name__)

_DEFAULT_BRANCH = "backups"
# Include tenant_slug by default so tenant-scoped backups don't collide
# with platform-scoped ones at the same path. Empty segments collapse
# automatically in :meth:`_render_path`.
_DEFAULT_PATH_TEMPLATE = "snapshots/{app_slug}/{scope}/{tenant_slug}"
_DEFAULT_LFS_PATTERNS = ("*.gpg", "*.tar", "*.tar.gz")


@dataclass(frozen=True)
class GitRepoSinkConfig:
    """Explicit config so callers can pass a single object or kwargs."""

    repo_url: str
    local_clone_dir: Path
    branch: str = _DEFAULT_BRANCH
    path_template: str = _DEFAULT_PATH_TEMPLATE
    lfs_enabled: bool = True
    # Commit identity for the bot that writes backup commits.
    commit_author_name: str = "scottycore-backup"
    commit_author_email: str = "backup@localhost"


class GitRepoSink(StorageSink):
    """Sink that commits + pushes snapshots to a git remote.

    Customers provide the git URL; this sink clones lazily on first use
    and keeps a working clone under ``local_clone_dir`` across calls.
    """

    sink_type: ClassVar[str] = "git_repo"

    def __init__(
        self,
        *,
        repo_url: str,
        local_clone_dir: str | Path,
        branch: str = _DEFAULT_BRANCH,
        path_template: str = _DEFAULT_PATH_TEMPLATE,
        lfs_enabled: bool = True,
        commit_author_name: str = "scottycore-backup",
        commit_author_email: str = "backup@localhost",
    ) -> None:
        if not repo_url:
            raise SinkError("GitRepoSink: repo_url is required")
        if "{app_slug}" not in path_template and "{scope}" not in path_template:
            # Either would be enough to disambiguate one app's bundles; both are ideal.
            _log.warning(
                "GitRepoSink: path_template %r lacks {app_slug}/{scope} — "
                "all apps will write to the same path and collide",
                path_template,
            )
        self._cfg = GitRepoSinkConfig(
            repo_url=repo_url,
            local_clone_dir=Path(local_clone_dir).expanduser().resolve(),
            branch=branch,
            path_template=path_template,
            lfs_enabled=lfs_enabled,
            commit_author_name=commit_author_name,
            commit_author_email=commit_author_email,
        )
        self._lock = asyncio.Lock()
        self._initialized = False

    # ── Public API ──────────────────────────────────────────────────────

    async def put(self, blob: BackupBlob) -> SinkWriteResult:
        async with self._lock:
            await self._ensure_ready()
            rel = self._render_path(blob)
            target = self._cfg.local_clone_dir / rel
            meta_path = target.with_suffix(target.suffix + ".meta.json")

            def _write() -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".part")
                tmp.write_bytes(blob.data)
                tmp.replace(target)
                meta_path.write_text(
                    json.dumps(
                        {
                            "sha256": blob.sha256,
                            "size": blob.size,
                            "app_slug": blob.app_slug,
                            "scope": blob.scope,
                            "kind": blob.kind,
                            "encrypted": blob.encrypted,
                            "key_fingerprint": blob.key_fingerprint,
                            "tenant_slug": blob.tenant_slug,
                            "created_at": blob.created_at.isoformat(),
                            "metadata": blob.metadata,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            await asyncio.to_thread(_write)
            msg = (
                f"backup: {blob.app_slug}/{blob.scope}"
                + (f"/{blob.tenant_slug}" if blob.tenant_slug else "")
                + f" {blob.kind} {blob.size}B"
            )
            await self._commit_and_push([str(rel), str(meta_path.relative_to(self._cfg.local_clone_dir))], msg)
            return SinkWriteResult(
                locator=str(rel),
                sink_type=self.sink_type,
                bytes_written=blob.size,
                created_at=blob.created_at,
            )

    async def get(self, locator: str) -> bytes:
        async with self._lock:
            await self._ensure_ready()
            await self._pull()
            path = self._resolve(locator)
            return await asyncio.to_thread(path.read_bytes)

    async def list_snapshots(
        self, *, app_slug: str | None = None, tenant_slug: str | None = None
    ) -> list[SnapshotEntry]:
        async with self._lock:
            await self._ensure_ready()
            await self._pull()
            root = self._cfg.local_clone_dir

            def _scan() -> list[SnapshotEntry]:
                out: list[SnapshotEntry] = []
                for sidecar in root.rglob("*.meta.json"):
                    # Skip .git internals just in case
                    if ".git" in sidecar.parts:
                        continue
                    try:
                        meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if app_slug and meta.get("app_slug") != app_slug:
                        continue
                    if tenant_slug and meta.get("tenant_slug") != tenant_slug:
                        continue
                    bundle_path = sidecar.with_name(
                        sidecar.name[: -len(".meta.json")]
                    )
                    if not bundle_path.exists():
                        continue
                    rel = str(bundle_path.relative_to(root))
                    out.append(
                        SnapshotEntry(
                            locator=rel,
                            app_slug=meta.get("app_slug", ""),
                            scope=meta.get("scope", ""),
                            kind=meta.get("kind", "full"),
                            size=int(meta.get("size", bundle_path.stat().st_size)),
                            created_at=_parse_ts(meta.get("created_at")),
                            encrypted=bool(meta.get("encrypted", False)),
                            sha256=meta.get("sha256"),
                            key_fingerprint=meta.get("key_fingerprint"),
                            tenant_slug=meta.get("tenant_slug"),
                        )
                    )
                out.sort(key=lambda e: e.created_at, reverse=True)
                return out

            return await asyncio.to_thread(_scan)

    async def delete(self, locator: str) -> None:
        async with self._lock:
            await self._ensure_ready()
            await self._pull()
            path = self._resolve(locator)
            rel = str(path.relative_to(self._cfg.local_clone_dir))
            sidecar = path.with_suffix(path.suffix + ".meta.json")

            paths_to_stage: list[str] = [rel]
            if sidecar.exists():
                paths_to_stage.append(
                    str(sidecar.relative_to(self._cfg.local_clone_dir))
                )
            # ``git rm`` both removes the working tree copy AND stages the
            # deletion — no subsequent ``git add`` is needed (doing so
            # would fail because the file is already gone).
            await self._git("rm", *paths_to_stage)

            status = await self._git_capture("status", "--porcelain")
            if not status.strip():
                return

            await self._git(
                "commit", "-m", f"backup: remove {rel}",
                "--author",
                f"{self._cfg.commit_author_name} <{self._cfg.commit_author_email}>",
            )
            await self._try_push()

    async def verify(self, locator: str, expected_sha256: str) -> bool:
        data = await self.get(locator)
        return hashlib.sha256(data).hexdigest() == expected_sha256

    # ── Git plumbing ────────────────────────────────────────────────────

    async def _ensure_ready(self) -> None:
        if self._initialized:
            return
        await asyncio.to_thread(self._sync_init)
        self._initialized = True

    def _sync_init(self) -> None:
        """Clone (or adopt an existing clone) + configure LFS + select branch.

        Runs in a worker thread so the event loop doesn't stall on a slow
        initial clone.
        """
        clone_dir = self._cfg.local_clone_dir
        clone_dir.mkdir(parents=True, exist_ok=True)

        if not (clone_dir / ".git").is_dir():
            _log.info(
                "GitRepoSink: cloning %s → %s", self._cfg.repo_url, clone_dir
            )
            try:
                subprocess.run(
                    ["git", "clone", self._cfg.repo_url, str(clone_dir)],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                # Empty remote: init as a fresh repo and push later.
                stderr = (exc.stderr or b"").decode("utf-8", "replace")
                if (
                    "remote branch" in stderr.lower()
                    or "not found" in stderr.lower()
                    or "does not appear" in stderr.lower()
                    or "warning: you appear" in stderr.lower()
                    or "empty repository" in stderr.lower()
                ):
                    _log.info(
                        "GitRepoSink: remote appears empty, initializing fresh clone"
                    )
                    subprocess.run(
                        ["git", "init", "-b", self._cfg.branch, str(clone_dir)],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        [
                            "git", "-C", str(clone_dir),
                            "remote", "add", "origin", self._cfg.repo_url,
                        ],
                        check=True,
                        capture_output=True,
                    )
                else:
                    raise SinkError(
                        f"GitRepoSink: clone failed: {stderr}"
                    ) from exc

        # Configure commit identity locally (doesn't affect the operator's global git config)
        self._run_git_sync(
            "config", "user.name", self._cfg.commit_author_name
        )
        self._run_git_sync(
            "config", "user.email", self._cfg.commit_author_email
        )

        # Make sure we're on the requested branch. Create it if missing.
        try:
            self._run_git_sync("checkout", self._cfg.branch)
        except SinkError:
            # Branch doesn't exist yet — create it from the current HEAD
            # (or as the first commit for an empty repo).
            try:
                self._run_git_sync("checkout", "-b", self._cfg.branch)
            except SinkError as exc:
                # Likely no commits yet — that's fine, we'll commit
                # to this branch below.
                _log.debug(
                    "GitRepoSink: initial branch %s checkout: %s",
                    self._cfg.branch,
                    exc,
                )

        if self._cfg.lfs_enabled:
            # Ensure git-lfs hooks are installed in this clone.
            try:
                self._run_git_sync("lfs", "install", "--local")
            except SinkError as exc:
                raise SinkError(
                    "GitRepoSink: lfs_enabled=True but 'git lfs install' "
                    f"failed — is git-lfs present? ({exc})"
                ) from exc

            # Write .gitattributes if missing or incomplete.
            ga = clone_dir / ".gitattributes"
            desired = {
                f"{pat} filter=lfs diff=lfs merge=lfs -text"
                for pat in _DEFAULT_LFS_PATTERNS
            }
            existing = set()
            if ga.exists():
                existing = {
                    ln.strip()
                    for ln in ga.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.startswith("#")
                }
            missing = desired - existing
            if missing:
                lines = sorted(existing | desired)
                ga.write_text(
                    "# Managed by scottycore GitRepoSink — LFS patterns for backup blobs\n"
                    + "\n".join(lines)
                    + "\n",
                    encoding="utf-8",
                )
                # Stage + commit the .gitattributes update; push when we
                # can.
                try:
                    self._run_git_sync("add", ".gitattributes")
                    self._run_git_sync(
                        "commit", "-m",
                        "chore(backup): configure git-lfs patterns",
                    )
                    self._try_push_sync()
                except SinkError as exc:
                    _log.debug(
                        "GitRepoSink: initial .gitattributes commit skipped: %s",
                        exc,
                    )

    async def _pull(self) -> None:
        """Best-effort fetch + fast-forward. Soft-fails offline."""
        try:
            await self._git("pull", "--ff-only", "origin", self._cfg.branch)
        except SinkError as exc:
            _log.warning(
                "GitRepoSink: pull failed (offline?): %s", exc
            )

    async def _commit_and_push(
        self, rel_paths: list[str], message: str
    ) -> None:
        await self._git("add", *rel_paths)

        # If ``git add`` didn't actually change anything (identical blob),
        # skip the commit — git commit would exit non-zero otherwise.
        status = await self._git_capture("status", "--porcelain")
        if not status.strip():
            _log.debug(
                "GitRepoSink: no-op commit skipped (content identical)"
            )
            return

        await self._git(
            "commit", "-m", message,
            "--author", f"{self._cfg.commit_author_name} <{self._cfg.commit_author_email}>",
        )
        await self._try_push()

    async def _try_push(self) -> None:
        try:
            await self._git("push", "origin", self._cfg.branch)
        except SinkError as exc:
            stderr = str(exc).lower()
            if "non-fast-forward" in stderr or "rejected" in stderr:
                # Try once with pull --rebase to catch up on concurrent writes.
                _log.info(
                    "GitRepoSink: non-ff push; pulling with rebase and retrying"
                )
                try:
                    await self._git(
                        "pull", "--rebase", "origin", self._cfg.branch
                    )
                    await self._git("push", "origin", self._cfg.branch)
                    return
                except SinkError as retry_exc:
                    _log.warning(
                        "GitRepoSink: push retry after rebase still failed; "
                        "commit is local only: %s",
                        retry_exc,
                    )
                    return
            _log.warning(
                "GitRepoSink: push failed; commit is local only: %s", exc
            )

    def _try_push_sync(self) -> None:
        """Synchronous variant used during init (before the event loop runs)."""
        try:
            self._run_git_sync("push", "origin", self._cfg.branch)
        except SinkError as exc:
            _log.warning(
                "GitRepoSink: initial push failed (remote may be empty): %s",
                exc,
            )

    # ── Path resolution ─────────────────────────────────────────────────

    def _render_path(self, blob: BackupBlob) -> str:
        """Compose the in-repo path for *blob* using ``path_template``.

        Timestamp carries microseconds so two puts within the same second
        don't collide on the same path.
        """
        ts = blob.created_at.astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        ext = ".tar.gz.gpg" if blob.encrypted else ".tar.gz"
        filename = f"{ts}-{blob.kind}{ext}"

        # Template vars that are always available.
        vars_ = {
            "app_slug": blob.app_slug or "unknown",
            "scope": blob.scope or "platform",
            "tenant_slug": blob.tenant_slug or "",
            "kind": blob.kind,
            "timestamp": ts,
        }
        try:
            prefix = self._cfg.path_template.format(**vars_)
        except KeyError as exc:
            raise SinkError(
                f"GitRepoSink: path_template {self._cfg.path_template!r} "
                f"uses unknown variable: {exc}"
            ) from exc
        # Collapse any accidental double slashes from empty template vars
        # (e.g. tenant_slug blank in a platform-scope path).
        prefix = "/".join(p for p in prefix.split("/") if p)
        return f"{prefix}/{filename}"

    def _resolve(self, locator: str) -> Path:
        target = (self._cfg.local_clone_dir / locator).resolve()
        try:
            target.relative_to(self._cfg.local_clone_dir)
        except ValueError as exc:
            raise SinkNotFoundError(
                f"locator escapes clone root: {locator}"
            ) from exc
        if not target.is_file():
            raise SinkNotFoundError(f"no snapshot at {locator}")
        return target

    # ── git subprocess wrappers ─────────────────────────────────────────

    async def _git(self, *args: str) -> None:
        def _run() -> None:
            self._run_git_sync(*args)

        await asyncio.to_thread(_run)

    async def _git_capture(self, *args: str) -> str:
        def _run() -> str:
            return self._run_git_sync_capture(*args)

        return await asyncio.to_thread(_run)

    def _run_git_sync(self, *args: str) -> None:
        """Run a git command in the clone; raise SinkError with stderr on failure."""
        proc = subprocess.run(
            ["git", "-C", str(self._cfg.local_clone_dir), *args],
            capture_output=True,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise SinkError(f"git {' '.join(args)} failed: {stderr}")

    def _run_git_sync_capture(self, *args: str) -> str:
        """Like :meth:`_run_git_sync` but returns stdout."""
        proc = subprocess.run(
            ["git", "-C", str(self._cfg.local_clone_dir), *args],
            capture_output=True,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise SinkError(f"git {' '.join(args)} failed: {stderr}")
        return proc.stdout.decode("utf-8", "replace")


# ── helpers ─────────────────────────────────────────────────────────────


def _parse_ts(raw: object) -> datetime:
    if not isinstance(raw, str):
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _git_available() -> bool:
    """Module-level guard for import-time git discovery."""
    return shutil.which("git") is not None
