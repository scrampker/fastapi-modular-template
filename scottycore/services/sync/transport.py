"""HTTP transport layer for the mesh sync protocol.

All calls include the peer API key as ``Authorization: Bearer <key>``.
Errors are wrapped in ``SyncTransportError`` so the engine can distinguish
network failures from application errors.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from scottycore.services.sync.schemas import NodeInfo, PullRequest, PushResponse

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_SYNC_SCHEMA_VERSION = "1"


class SyncTransportError(Exception):
    """Wraps network or protocol errors from peer communication."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _make_headers(bearer_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer_key}",
        "X-Sync-Schema-Version": _SYNC_SCHEMA_VERSION,
    }


async def call_peer_info(base_url: str, their_key_for_us: str) -> NodeInfo:
    """GET /sync/info on the remote peer.

    ``their_key_for_us`` is the raw API key string stored in our SyncPeer row.
    Raises ``SyncTransportError`` on any failure.
    """
    url = f"{base_url.rstrip('/')}/sync/info"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(url, headers=_make_headers(their_key_for_us))
    except httpx.RequestError as exc:
        raise SyncTransportError(f"Network error calling {url}: {exc}") from exc

    if resp.status_code != 200:
        raise SyncTransportError(
            f"GET {url} returned {resp.status_code}", status_code=resp.status_code
        )
    try:
        return NodeInfo.model_validate(resp.json())
    except Exception as exc:
        raise SyncTransportError(f"Invalid NodeInfo from {url}: {exc}") from exc


async def call_peer_pull(
    base_url: str,
    their_key_for_us: str,
    since: datetime | None,
    scope: dict,
    exclude_origins: list[str],
) -> bytes:
    """POST /sync/pull — fetch a delta bundle from the peer.

    Returns raw gzip bytes.  The caller is responsible for parsing the tarball.
    """
    url = f"{base_url.rstrip('/')}/sync/pull"
    body = PullRequest(
        since=since,
        scope=scope,
        exclude_origins=exclude_origins,
    )
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={**_make_headers(their_key_for_us), "Content-Type": "application/json"},
                content=body.model_dump_json(),
            )
    except httpx.RequestError as exc:
        raise SyncTransportError(f"Network error calling {url}: {exc}") from exc

    if resp.status_code != 200:
        raise SyncTransportError(
            f"POST {url} returned {resp.status_code}", status_code=resp.status_code
        )
    return resp.content


async def call_peer_push(
    base_url: str,
    their_key_for_us: str,
    bundle_bytes: bytes,
    our_node_id: str,
) -> PushResponse:
    """POST /sync/push — send our delta bundle to the peer.

    Bundle is transmitted as a multipart upload alongside origin metadata.
    """
    url = f"{base_url.rstrip('/')}/sync/push"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers=_make_headers(their_key_for_us),
                files={"bundle": ("sync_bundle.tar.gz", bundle_bytes, "application/gzip")},
                data={"origin_node_id": our_node_id},
            )
    except httpx.RequestError as exc:
        raise SyncTransportError(f"Network error calling {url}: {exc}") from exc

    if resp.status_code != 200:
        raise SyncTransportError(
            f"POST {url} returned {resp.status_code}", status_code=resp.status_code
        )
    try:
        return PushResponse.model_validate(resp.json())
    except Exception as exc:
        raise SyncTransportError(f"Invalid PushResponse from {url}: {exc}") from exc


async def call_peer_heartbeat(base_url: str, their_key_for_us: str) -> bool:
    """POST /sync/heartbeat — lightweight liveness check.

    Returns True on 200, False on any error (caller decides how to handle).
    """
    url = f"{base_url.rstrip('/')}/sync/heartbeat"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=_make_headers(their_key_for_us))
        return resp.status_code == 200
    except Exception as exc:
        logger.debug("Heartbeat to %s failed: %s", base_url, exc)
        return False
