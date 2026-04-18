"""Tests for systemd unit rendering from BrandConfig."""

from __future__ import annotations

from pathlib import Path

import pytest

from scottycore.core.brand import BrandConfig
from scottycore.services.auto_update.systemd_render import render_systemd_units


class TestRenderSystemdUnits:
    def test_renders_all_four_units_default_brand(
        self, tmp_path: Path
    ) -> None:
        files = render_systemd_units(tmp_path, brand=BrandConfig())
        names = sorted(p.name for p in files)
        assert names == [
            "scottycore-app-update-check@.service",
            "scottycore-app-update-check@.timer",
            "scottycore-update-check@.service",
            "scottycore-update-check@.timer",
        ]

    def test_renders_with_custom_brand(self, tmp_path: Path) -> None:
        brand = BrandConfig(
            framework_name="briancore", display_name="Brian"
        )
        files = render_systemd_units(tmp_path, brand=brand)
        names = sorted(p.name for p in files)
        assert names == [
            "briancore-app-update-check@.service",
            "briancore-app-update-check@.timer",
            "briancore-update-check@.service",
            "briancore-update-check@.timer",
        ]

    def test_substitutes_framework_name_in_unit_file(
        self, tmp_path: Path
    ) -> None:
        brand = BrandConfig(framework_name="briancore")
        render_systemd_units(tmp_path, brand=brand)
        service = (tmp_path / "briancore-update-check@.service").read_text()
        assert "briancore" in service
        assert "scottycore" not in service
        # Apps root should derive from framework
        assert "/opt/briancore" in service

    def test_substitutes_display_name_in_description(
        self, tmp_path: Path
    ) -> None:
        brand = BrandConfig(
            framework_name="briancore", display_name="Brian"
        )
        render_systemd_units(tmp_path, brand=brand)
        svc = (
            tmp_path / "briancore-app-update-check@.service"
        ).read_text()
        assert "Brian app generic git-pull" in svc

    def test_no_unreplaced_template_vars(self, tmp_path: Path) -> None:
        brand = BrandConfig(framework_name="briancore")
        files = render_systemd_units(tmp_path, brand=brand)
        for f in files:
            content = f.read_text()
            assert "{{" not in content, f"unreplaced var in {f.name}: {content}"
            assert "}}" not in content, f"unreplaced var in {f.name}"
