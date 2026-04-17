"""Chaos tests — passphrase edge cases.

Probe the crypto layer with hostile/unusual passphrase values:
empty string, very long, unicode, newlines, null bytes, shell metacharacters.
"""

from __future__ import annotations

import pytest

from scottycore.services.backup.crypto import (
    CryptoError,
    decrypt_bundle,
    encrypt_bundle,
    fingerprint,
)

PAYLOAD = b"top-secret-payload"


# ── Empty / whitespace ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_string_passphrase_encrypt_raises() -> None:
    """Empty passphrase must be rejected before invoking GPG."""
    with pytest.raises(CryptoError):
        await encrypt_bundle(PAYLOAD, "")


@pytest.mark.asyncio
async def test_empty_string_passphrase_decrypt_raises() -> None:
    with pytest.raises(CryptoError):
        await decrypt_bundle(b"\x00" * 32, "")


@pytest.mark.asyncio
async def test_whitespace_only_passphrase_round_trips() -> None:
    """A passphrase of all spaces should be accepted (not empty)."""
    ct = await encrypt_bundle(PAYLOAD, "     ")
    pt = await decrypt_bundle(ct, "     ")
    assert pt == PAYLOAD


# ── Very long passphrase ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_4096_char_passphrase_round_trips() -> None:
    pw = "A" * 4096
    ct = await encrypt_bundle(PAYLOAD, pw)
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


@pytest.mark.asyncio
async def test_16k_char_passphrase_round_trips() -> None:
    """Stress-test the passphrase file write path with a very large passphrase."""
    pw = "x" * 16_384
    ct = await encrypt_bundle(PAYLOAD, pw)
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


# ── Unicode ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unicode_emoji_passphrase_round_trips() -> None:
    pw = "🔐🦊🌍"
    ct = await encrypt_bundle(PAYLOAD, pw)
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


@pytest.mark.asyncio
async def test_unicode_cjk_passphrase_round_trips() -> None:
    pw = "密码很安全123"
    ct = await encrypt_bundle(PAYLOAD, pw)
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


@pytest.mark.asyncio
async def test_rtl_arabic_passphrase_round_trips() -> None:
    pw = "كلمة السر السرية"
    ct = await encrypt_bundle(PAYLOAD, pw)
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


# ── Newlines and control chars ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passphrase_with_newline_round_trips() -> None:
    """Newlines written to the passphrase file should be preserved, not truncated."""
    pw = "line1\nline2"
    ct = await encrypt_bundle(PAYLOAD, pw)
    # The same passphrase WITH the newline must decrypt successfully.
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


@pytest.mark.asyncio
async def test_passphrase_newline_normalised_to_stripped() -> None:
    """'pw\\n' and 'pw' produce the same GPG key because GPG strips trailing
    newlines from passphrase files.  The crypto layer now normalises explicitly
    so fingerprint and round-trip are both consistent.

    Specifically:
    - encrypt with 'pw\\n' must decrypt with 'pw' (same normalised key)
    - fingerprint('pw\\n') must equal fingerprint('pw')
    """
    ct = await encrypt_bundle(PAYLOAD, "pw\n")
    pt = await decrypt_bundle(ct, "pw")
    assert pt == PAYLOAD

    assert fingerprint("pw\n") == fingerprint("pw"), (
        "fingerprint mismatch: 'pw\\n' and 'pw' normalise to the same key but "
        "fingerprint disagrees — they would mislead the user about which key to use"
    )


@pytest.mark.asyncio
async def test_passphrase_with_null_byte() -> None:
    """Null byte in passphrase: GPG behaviour is undefined; we must not hang or crash."""
    pw = "pass\x00word"
    # Either round-trips (acceptable) or raises CryptoError (also acceptable).
    # What's NOT acceptable: process hang, silent data loss, or wrong plaintext.
    try:
        ct = await encrypt_bundle(PAYLOAD, pw)
        pt = await decrypt_bundle(ct, pw)
        assert pt == PAYLOAD
    except CryptoError:
        pass  # clean failure is fine


@pytest.mark.asyncio
async def test_passphrase_with_shell_metacharacters_round_trips() -> None:
    """Shell metacharacters in the passphrase must not be interpreted by the shell.

    The passphrase is passed via a file, not via argv or a shell command,
    so injection is not expected — but verify this is actually safe.
    """
    pw = r'$HOME; rm -rf / & `echo pwned` | cat <(ls) > /dev/null "quoted" '
    ct = await encrypt_bundle(PAYLOAD, pw)
    pt = await decrypt_bundle(ct, pw)
    assert pt == PAYLOAD


# ── Fingerprint edge cases ─────────────────────────────────────────────────


def test_fingerprint_null_byte_in_passphrase() -> None:
    """fingerprint() must not crash on a passphrase containing null bytes."""
    fp = fingerprint("pass\x00word")
    assert len(fp) == 8
    assert fp.isalnum()


def test_fingerprint_empty_string() -> None:
    """fingerprint() is called on whatever the user supplies before the guard;
    it should not raise even if the passphrase is empty."""
    fp = fingerprint("")
    assert len(fp) == 8
