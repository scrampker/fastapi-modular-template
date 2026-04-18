"""Tests for scottycore.core.brand — BrandConfig + env loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from scottycore.core.brand import BrandConfig, get_brand, reset_brand_cache


@pytest.fixture(autouse=True)
def _clear_brand_cache() -> None:
    """Ensure every test starts with a clean brand cache."""
    reset_brand_cache()
    yield
    reset_brand_cache()


class TestBrandDefaults:
    """Defaults preserve scotty behaviour for backwards compat."""

    def test_default_framework_name(self) -> None:
        brand = BrandConfig()
        assert brand.framework_name == "scottycore"

    def test_default_family_name(self) -> None:
        assert BrandConfig().family_name == "scotty"

    def test_default_orchestrator(self) -> None:
        assert BrandConfig().orchestrator_name == "scottydev"

    def test_default_infra_worker(self) -> None:
        assert BrandConfig().infra_worker_name == "scottylab"

    def test_default_infra_worker_url_is_empty(self) -> None:
        # Empty by default so solo-mode is the default stance
        assert BrandConfig().infra_worker_url == ""

    def test_default_domain_root(self) -> None:
        assert BrandConfig().domain_root == "scotty.consulting"

    def test_default_display_name(self) -> None:
        assert BrandConfig().display_name == "Scotty"


class TestBrandFromEnv:
    """``BrandConfig.from_env`` should honour BRAND_* overrides."""

    def test_override_framework(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "briancore")
        brand = BrandConfig.from_env()
        assert brand.framework_name == "briancore"

    def test_override_all_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "briancore")
        monkeypatch.setenv("BRAND_FAMILY_NAME", "brian")
        monkeypatch.setenv("BRAND_ORCHESTRATOR_NAME", "briandev")
        monkeypatch.setenv("BRAND_INFRA_WORKER_NAME", "brianlab")
        monkeypatch.setenv(
            "BRAND_INFRA_WORKER_URL", "https://brianlab.example.com"
        )
        monkeypatch.setenv("BRAND_DOMAIN_ROOT", "brian.example.com")
        monkeypatch.setenv(
            "BRAND_FRAMEWORK_REPO_URL",
            "https://github.com/brian/briancore.git",
        )
        monkeypatch.setenv("BRAND_DISPLAY_NAME", "Brian")

        brand = BrandConfig.from_env()
        assert brand.framework_name == "briancore"
        assert brand.family_name == "brian"
        assert brand.orchestrator_name == "briandev"
        assert brand.infra_worker_name == "brianlab"
        assert brand.infra_worker_url == "https://brianlab.example.com"
        assert brand.domain_root == "brian.example.com"
        assert (
            brand.framework_repo_url == "https://github.com/brian/briancore.git"
        )
        assert brand.display_name == "Brian"

    def test_empty_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "")
        brand = BrandConfig.from_env()
        assert brand.framework_name == "scottycore"

    def test_framework_name_lowercased(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "BRIANCORE")
        assert BrandConfig.from_env().framework_name == "briancore"

    def test_infra_worker_empty_means_solo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAND_INFRA_WORKER_URL", "")
        assert BrandConfig.from_env().has_infra_worker is False

    def test_infra_worker_url_set_means_non_solo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "BRAND_INFRA_WORKER_URL", "https://infra.example.com"
        )
        assert BrandConfig.from_env().has_infra_worker is True


class TestDerivedPaths:
    """Paths and unit names derive from framework_name."""

    def test_config_dir_default(self) -> None:
        assert BrandConfig().config_dir == Path("/etc/scottycore")

    def test_config_dir_overridden(self) -> None:
        brand = BrandConfig(framework_name="briancore")
        assert brand.config_dir == Path("/etc/briancore")

    def test_apps_root(self) -> None:
        assert BrandConfig().apps_root == Path("/opt/scottycore")
        assert (
            BrandConfig(framework_name="briancore").apps_root
            == Path("/opt/briancore")
        )

    def test_data_dir_env_var(self) -> None:
        assert BrandConfig().data_dir_env_var == "SCOTTYCORE_DATA_DIR"
        assert (
            BrandConfig(framework_name="briancore").data_dir_env_var
            == "BRIANCORE_DATA_DIR"
        )

    def test_systemd_unit_prefix(self) -> None:
        assert BrandConfig().systemd_unit_prefix == "scottycore-update-check"

    def test_systemd_app_unit_prefix(self) -> None:
        assert (
            BrandConfig().systemd_app_unit_prefix
            == "scottycore-app-update-check"
        )

    def test_update_mode_path(self) -> None:
        assert BrandConfig().update_mode_path == Path(
            "/etc/scottycore/update-mode"
        )

    def test_update_setting_keys(self) -> None:
        brand = BrandConfig()
        assert brand.update_setting_key_mode == "scottycore.update.mode"
        assert brand.update_setting_key_pending == "scottycore.update.pending"

    def test_rebuild_flag_filename(self) -> None:
        assert (
            BrandConfig().rebuild_flag_filename
            == ".scottycore-auto-update-requested"
        )

    def test_pin_pattern(self) -> None:
        # Full regex is built at use-site; here we just verify the tag
        # name is in the pattern.
        assert "scottycore" in BrandConfig().pin_pattern
        brand = BrandConfig(framework_name="briancore")
        assert "briancore" in brand.pin_pattern

    def test_infra_worker_fqdn_default(self) -> None:
        assert (
            BrandConfig().infra_worker_fqdn_default
            == "scottylab.scotty.consulting"
        )

    def test_orchestrator_fqdn_default(self) -> None:
        assert (
            BrandConfig().orchestrator_fqdn_default
            == "scottydev.scotty.consulting"
        )

    def test_fqdn_defaults_brand_override(self) -> None:
        brand = BrandConfig(
            family_name="brian",
            orchestrator_name="briandev",
            infra_worker_name="brianlab",
            domain_root="brian.example.com",
        )
        assert (
            brand.orchestrator_fqdn_default == "briandev.brian.example.com"
        )
        assert brand.infra_worker_fqdn_default == "brianlab.brian.example.com"


class TestImmutability:
    """BrandConfig is frozen — no in-place mutation allowed."""

    def test_cannot_mutate(self) -> None:
        brand = BrandConfig()
        with pytest.raises((AttributeError, Exception)):
            brand.framework_name = "briancore"  # type: ignore[misc]


class TestSingletonCache:
    """``get_brand`` caches; ``reset_brand_cache`` re-reads env."""

    def test_get_brand_caches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "first")
        first = get_brand()
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "second")
        # No reset → still the first
        assert get_brand() is first

    def test_reset_brand_cache_reloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "first")
        first = get_brand()
        monkeypatch.setenv("BRAND_FRAMEWORK_NAME", "second")
        reset_brand_cache()
        second = get_brand()
        assert second is not first
        assert second.framework_name == "second"
