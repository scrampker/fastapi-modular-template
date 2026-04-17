"""Backups admin page — smoke test that the template renders."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from scottycore.main import app


@pytest.mark.asyncio
async def test_admin_backups_page_renders() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        resp = await ac.get("/admin/backups")
    assert resp.status_code == 200
    body = resp.text
    # Core markers that the Jinja template rendered (not a 200 from an
    # unrelated route).
    assert "Backup" in body
    assert "backupsPage" in body
    assert "activeTab" in body
    assert "/api/v1/backups/runs" in body
