"""ScottyDevSink unit tests against an in-process httpx MockTransport."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest

from scottycore.services.backup.sinks import (
    BackupBlob,
    ScottyDevSink,
    SinkError,
    SinkNotFoundError,
)


def _blob(data: bytes, *, app_slug: str = "demo") -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        app_slug=app_slug,
        scope="platform",
        kind="full",
        created_at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
    )


class _FakeServer:
    """Minimal in-memory backend matching ScottyDevSink's API shape."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.meta: dict[str, dict] = {}
        self.auth_seen: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth_seen.append(request.headers.get("authorization"))
        path = request.url.path
        method = request.method

        if path.startswith("/api/v1/backups/store/"):
            rel = path[len("/api/v1/backups/store/") :]
            if method == "PUT":
                if rel.endswith(".meta.json"):
                    self.meta[rel] = json.loads(request.content)
                else:
                    self.store[rel] = request.content
                return httpx.Response(204)
            if method == "GET":
                if rel not in self.store:
                    return httpx.Response(404)
                return httpx.Response(200, content=self.store[rel])
            if method == "DELETE":
                if rel not in self.store:
                    return httpx.Response(404)
                del self.store[rel]
                self.meta.pop(rel + ".meta.json", None)
                return httpx.Response(204)

        if path == "/api/v1/backups/index" and method == "GET":
            app_slug = request.url.params.get("app_slug")
            rows = []
            for rel, meta in self.meta.items():
                if app_slug and meta.get("app_slug") != app_slug:
                    continue
                locator = f"{meta['app_slug']}/{rel[: -len('.meta.json')]}"
                rows.append({"locator": locator, **meta})
            return httpx.Response(200, json=rows)

        return httpx.Response(404)


@pytest.fixture
def server_and_sink():
    server = _FakeServer()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(server.handler),
        headers={"authorization": "Bearer tok-123"},
    )
    sink = ScottyDevSink(base_url="http://scottydev.test", client=client)
    yield server, sink


@pytest.mark.asyncio
async def test_put_uploads_bundle_and_sidecar(server_and_sink) -> None:
    server, sink = server_and_sink
    blob = _blob(b"hello")

    result = await sink.put(blob)

    assert result.sink_type == "scottydev"
    assert result.bytes_written == 5
    # store has bundle, meta has sidecar
    stored_rel = next(iter(server.store))
    assert server.store[stored_rel] == b"hello"
    sidecar_rel = next(iter(server.meta))
    assert server.meta[sidecar_rel]["sha256"] == blob.sha256


@pytest.mark.asyncio
async def test_get_returns_stored_bytes(server_and_sink) -> None:
    server, sink = server_and_sink
    r = await sink.put(_blob(b"payload"))
    got = await sink.get(r.locator)
    assert got == b"payload"


@pytest.mark.asyncio
async def test_get_404_raises(server_and_sink) -> None:
    _, sink = server_and_sink
    with pytest.raises(SinkNotFoundError):
        await sink.get("demo/platform/nope.tar.gz")


@pytest.mark.asyncio
async def test_list_snapshots_filters(server_and_sink) -> None:
    _, sink = server_and_sink
    await sink.put(_blob(b"a", app_slug="demo"))
    await sink.put(_blob(b"b", app_slug="other"))

    demo = await sink.list_snapshots(app_slug="demo")
    assert len(demo) == 1
    assert demo[0].app_slug == "demo"


@pytest.mark.asyncio
async def test_delete_then_get_404(server_and_sink) -> None:
    _, sink = server_and_sink
    r = await sink.put(_blob(b"gone"))

    await sink.delete(r.locator)

    with pytest.raises(SinkNotFoundError):
        await sink.get(r.locator)


@pytest.mark.asyncio
async def test_delete_missing_raises(server_and_sink) -> None:
    _, sink = server_and_sink
    with pytest.raises(SinkNotFoundError):
        await sink.delete("demo/platform/nothing.tar.gz")


@pytest.mark.asyncio
async def test_put_surfaces_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ScottyDevSink(base_url="http://x", client=client)

    with pytest.raises(SinkError):
        await sink.put(_blob(b"x"))
