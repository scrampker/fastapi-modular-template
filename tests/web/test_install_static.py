"""Tests for :func:`scottycore.web.install_static`."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scottycore.web import install_static


def _core_static_dir() -> str:
    import scottycore.web as web_pkg

    return os.path.join(os.path.dirname(web_pkg.__file__), "static")


@pytest.fixture
def fresh_app() -> FastAPI:
    return FastAPI()


def test_mounts_scottycore_static_at_default_path(fresh_app: FastAPI) -> None:
    """Default call mounts /static against scottycore's installed dir."""
    install_static(fresh_app)

    # A file known to ship with scottycore's web/static/
    # (verified present in the repo; loss would indicate a shipping regression)
    core_dir = _core_static_dir()
    assert os.path.isfile(os.path.join(core_dir, "manifest.json")), (
        "scottycore/web/static/manifest.json is missing from the install"
    )

    client = TestClient(fresh_app)
    r = client.get("/static/manifest.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(("application/", "text/"))


def test_default_call_does_not_create_overlay_mount(fresh_app: FastAPI) -> None:
    """Without overlay_dir, no overlay mount is registered."""
    install_static(fresh_app)

    mounted_paths = {
        route.path
        for route in fresh_app.routes
        if getattr(route, "name", "") in {"static", "static-app"}
    }
    assert "/static" in mounted_paths
    assert "/static-app" not in mounted_paths


def test_overlay_dir_mounts_additional_path(
    fresh_app: FastAPI, tmp_path
) -> None:
    """When overlay_dir is provided, it is mounted at /static-app."""
    overlay = tmp_path / "app-static"
    overlay.mkdir()
    (overlay / "app.js").write_text("console.log('overlay');", encoding="utf-8")

    install_static(fresh_app, overlay_dir=str(overlay))

    client = TestClient(fresh_app)
    r_core = client.get("/static/manifest.json")
    r_overlay = client.get("/static-app/app.js")

    assert r_core.status_code == 200
    assert r_overlay.status_code == 200
    assert "overlay" in r_overlay.text


def test_overlay_mount_path_empty_disables_overlay(
    fresh_app: FastAPI, tmp_path
) -> None:
    """overlay_mount_path='' skips overlay even if overlay_dir exists."""
    overlay = tmp_path / "app-static"
    overlay.mkdir()
    (overlay / "app.js").write_text("console.log('overlay');", encoding="utf-8")

    install_static(fresh_app, overlay_dir=str(overlay), overlay_mount_path="")

    client = TestClient(fresh_app)
    r_overlay = client.get("/static-app/app.js")
    assert r_overlay.status_code == 404


def test_custom_mount_path(fresh_app: FastAPI) -> None:
    """mount_path=/assets mounts at /assets instead of /static."""
    install_static(fresh_app, mount_path="/assets")

    client = TestClient(fresh_app)
    assert client.get("/static/manifest.json").status_code == 404
    assert client.get("/assets/manifest.json").status_code == 200


def test_missing_overlay_dir_is_silently_ignored(
    fresh_app: FastAPI, tmp_path
) -> None:
    """A non-existent overlay_dir does not raise; it's simply skipped."""
    missing = tmp_path / "does-not-exist"
    # Do NOT create the directory.

    install_static(fresh_app, overlay_dir=str(missing))

    client = TestClient(fresh_app)
    # Core mount still works.
    assert client.get("/static/manifest.json").status_code == 200
    # Overlay mount absent.
    assert client.get("/static-app/anything.js").status_code == 404
