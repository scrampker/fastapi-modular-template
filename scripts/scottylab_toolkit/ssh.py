"""Tiny subprocess wrappers for SSH. Never interactive — always BatchMode."""

import subprocess


def run(host: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run `cmd` on `host` via SSH with BatchMode. Returns CompletedProcess."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def read(host: str, path: str) -> str | None:
    """Read a file on a remote host over SSH. Returns contents or None on failure."""
    r = run(host, f"cat {path}", timeout=15)
    return r.stdout if r.returncode == 0 else None
