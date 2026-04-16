"""Peer API key authentication for sync endpoints.

Reuses the same bcrypt prefix-lookup pattern as Tenant API keys:
  1. Extract prefix from bearer token (first 8 chars).
  2. SELECT candidate from peer_api_keys WHERE key_prefix = prefix AND is_active.
  3. bcrypt-verify full token against stored hash.
  4. Return the owning SyncPeer.

``authenticate_peer`` raises AuthenticationError on any mismatch so callers
can convert it to a 401 without leaking which part failed.
"""

from __future__ import annotations

import logging
import secrets

from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from scottycore.core.exceptions import AuthenticationError
from scottycore.services.sync import repository as repo
from scottycore.services.sync.models import SyncPeer

logger = logging.getLogger(__name__)

_KEY_PREFIX_LEN = 8
_KEY_TOTAL_LEN = 40

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def generate_peer_key() -> tuple[str, str, str]:
    """Generate a new peer API key.

    Returns ``(plaintext, hash, prefix)`` where:
      ``plaintext`` — the full key to hand to the peer (shown once).
      ``hash``      — bcrypt hash stored in our DB / SyncPeer columns.
      ``prefix``    — first ``_KEY_PREFIX_LEN`` chars used for O(1) lookup.
    """
    # Use a URL-safe alphabet so the key is safe in Authorization headers.
    plaintext = secrets.token_urlsafe(_KEY_TOTAL_LEN)[:_KEY_TOTAL_LEN]
    prefix = plaintext[:_KEY_PREFIX_LEN]
    hashed = _pwd_ctx.hash(plaintext)
    return plaintext, hashed, prefix


def hash_peer_key(plaintext: str) -> tuple[str, str]:
    """Hash an existing plaintext key.  Returns ``(hash, prefix)``."""
    prefix = plaintext[:_KEY_PREFIX_LEN]
    hashed = _pwd_ctx.hash(plaintext)
    return hashed, prefix


def verify_peer_key(plaintext: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plaintext, hashed)


async def authenticate_peer(bearer: str, session: AsyncSession) -> SyncPeer:
    """Resolve a Bearer token to the owning SyncPeer.

    Raises ``AuthenticationError`` if the token is invalid, unknown, or the
    owning peer is disabled.  The error message is intentionally generic to
    avoid oracle attacks.
    """
    if not bearer or len(bearer) < _KEY_PREFIX_LEN:
        raise AuthenticationError("Invalid peer credentials")

    prefix = bearer[:_KEY_PREFIX_LEN]

    # O(1) prefix lookup — may return None if prefix is unknown.
    candidate_key = await repo.get_active_key_by_prefix(session, prefix)
    if candidate_key is None:
        raise AuthenticationError("Invalid peer credentials")

    if not verify_peer_key(bearer, candidate_key.key_hash):
        raise AuthenticationError("Invalid peer credentials")

    peer = await repo.get_peer(session, candidate_key.peer_id)
    if peer is None or not peer.enabled:
        raise AuthenticationError("Peer not found or disabled")

    return peer
