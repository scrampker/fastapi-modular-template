"""Symmetric passphrase-based encryption for backup bundles.

Uses ``gpg --symmetric`` with AES-256 and GPG's default S2K (iterated+salted
with SHA-512). That is strong enough for human-entered passphrases and is the
same mechanism Proxmox Backup, restic and borgbackup use under the hood.

Why shell out to gpg instead of using a Python crypto lib directly?
  * the output file format is self-describing and stable across GPG majors
  * it's the industry standard for "encrypted tarball you can restore later
    without our software" — users can decrypt a bundle with plain gpg
  * zero extra hard dependency (gpg is in our Docker base image already)

Key fingerprint
---------------
We don't want to log passphrases or derive the exact hash used by GPG's S2K.
Instead we record ``SHA256(passphrase.utf-8)[:4]`` as an 8-char hex
*fingerprint*. It's enough entropy to tell apart passphrases a user has on
file, but not enough to be useful for an offline attack.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path


class CryptoError(Exception):
    """GPG invocation failed or produced wrong output."""


def fingerprint(passphrase: str) -> str:
    """Stable, short identifier for a passphrase.

    First 4 bytes (8 hex chars) of SHA-256. Collision chance ≈ 2**-16 per
    pair, which is fine for a user-facing "which key was this?" hint.
    """
    h = hashlib.sha256(passphrase.encode("utf-8")).digest()
    return h[:4].hex()


async def encrypt_bundle(data: bytes, passphrase: str) -> bytes:
    """Encrypt *data* with GPG symmetric AES-256 + default S2K.

    Returns the ciphertext (OpenPGP packet stream, raw binary — no ASCII
    armor). Caller should write this to ``...tar.gz.gpg``.
    """
    if not passphrase:
        raise CryptoError("empty passphrase is not allowed")

    with tempfile.TemporaryDirectory(prefix="scotty-gpg-") as td:
        tdir = Path(td)
        passfile = tdir / "pw"
        passfile.write_text(passphrase, encoding="utf-8")
        passfile.chmod(0o600)

        proc = await asyncio.create_subprocess_exec(
            "gpg",
            "--batch",
            "--yes",
            "--quiet",
            "--no-tty",
            "--pinentry-mode",
            "loopback",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "--s2k-cipher-algo",
            "AES256",
            "--s2k-digest-algo",
            "SHA512",
            "--s2k-count",
            "65011712",  # max iteration count
            "--compress-algo",
            "none",
            "--passphrase-file",
            str(passfile),
            "--output",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GNUPGHOME": str(tdir)},
        )
        out, err = await proc.communicate(input=data)
        if proc.returncode != 0 or not out:
            raise CryptoError(
                f"gpg encrypt failed ({proc.returncode}): {err.decode(errors='replace')}"
            )
        return out


async def decrypt_bundle(data: bytes, passphrase: str) -> bytes:
    """Decrypt a blob previously produced by :func:`encrypt_bundle`."""
    if not passphrase:
        raise CryptoError("empty passphrase is not allowed")

    with tempfile.TemporaryDirectory(prefix="scotty-gpg-") as td:
        tdir = Path(td)
        passfile = tdir / "pw"
        passfile.write_text(passphrase, encoding="utf-8")
        passfile.chmod(0o600)

        proc = await asyncio.create_subprocess_exec(
            "gpg",
            "--batch",
            "--yes",
            "--quiet",
            "--no-tty",
            "--pinentry-mode",
            "loopback",
            "--decrypt",
            "--passphrase-file",
            str(passfile),
            "--output",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GNUPGHOME": str(tdir)},
        )
        out, err = await proc.communicate(input=data)
        if proc.returncode != 0:
            raise CryptoError(
                f"gpg decrypt failed ({proc.returncode}): {err.decode(errors='replace')}"
            )
        return out
