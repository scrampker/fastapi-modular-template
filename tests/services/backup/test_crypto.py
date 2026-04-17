"""Tests for backup passphrase crypto (GPG symmetric)."""

from __future__ import annotations

import pytest

from scottycore.services.backup.crypto import (
    CryptoError,
    decrypt_bundle,
    encrypt_bundle,
    fingerprint,
)


def test_fingerprint_is_stable_and_short() -> None:
    fp = fingerprint("horse-battery-staple")
    assert len(fp) == 8
    assert fp == fingerprint("horse-battery-staple")


def test_fingerprint_differs_between_passphrases() -> None:
    assert fingerprint("a") != fingerprint("b")


@pytest.mark.asyncio
async def test_round_trip() -> None:
    payload = b"hello, encrypted world" * 256
    ct = await encrypt_bundle(payload, "correct-horse")
    assert ct != payload
    pt = await decrypt_bundle(ct, "correct-horse")
    assert pt == payload


@pytest.mark.asyncio
async def test_wrong_passphrase_fails_decrypt() -> None:
    ct = await encrypt_bundle(b"secret", "right-key")
    with pytest.raises(CryptoError):
        await decrypt_bundle(ct, "wrong-key")


@pytest.mark.asyncio
async def test_empty_passphrase_rejected() -> None:
    with pytest.raises(CryptoError):
        await encrypt_bundle(b"x", "")
    with pytest.raises(CryptoError):
        await decrypt_bundle(b"x", "")


@pytest.mark.asyncio
async def test_ciphertext_is_opaque_to_substring() -> None:
    """A plaintext phrase must not appear unencrypted in the output."""
    ct = await encrypt_bundle(b"canary-PHRASE-12345", "pw")
    assert b"canary-PHRASE-12345" not in ct


@pytest.mark.asyncio
async def test_empty_plaintext_round_trips() -> None:
    ct = await encrypt_bundle(b"", "pw")
    assert await decrypt_bundle(ct, "pw") == b""
