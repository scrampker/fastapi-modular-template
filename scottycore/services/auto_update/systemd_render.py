"""Render systemd unit templates from BrandConfig.

The ``systemd/*.j2`` files are simple ``{{ var }}`` templates — we don't
pull in Jinja2 for this; a minimal str.replace is enough and keeps the
install path dep-free.
"""

from __future__ import annotations

from pathlib import Path

from scottycore.core.brand import BrandConfig, get_brand

_TEMPLATE_DIR = Path(__file__).resolve().parent / "systemd"


def render_systemd_units(
    output_dir: Path,
    brand: BrandConfig | None = None,
) -> list[Path]:
    """Render all four unit templates with brand substitutions.

    Returns the list of files written. Output filenames use the brand's
    framework name (e.g. ``scottycore-update-check@.service``,
    ``briancore-update-check@.timer``).
    """
    brand = brand or get_brand()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    substitutions = {
        "framework_name": brand.framework_name,
        "apps_root": str(brand.apps_root),
        "display_name": brand.display_name,
    }

    mapping = {
        "update-check@.service.j2": (
            f"{brand.framework_name}-update-check@.service"
        ),
        "update-check@.timer.j2": (
            f"{brand.framework_name}-update-check@.timer"
        ),
        "app-update-check@.service.j2": (
            f"{brand.framework_name}-app-update-check@.service"
        ),
        "app-update-check@.timer.j2": (
            f"{brand.framework_name}-app-update-check@.timer"
        ),
    }

    written: list[Path] = []
    for tmpl_name, out_name in mapping.items():
        tmpl = _TEMPLATE_DIR / tmpl_name
        content = tmpl.read_text(encoding="utf-8")
        for key, value in substitutions.items():
            content = content.replace("{{ " + key + " }}", value)
        out_path = output_dir / out_name
        out_path.write_text(content, encoding="utf-8")
        written.append(out_path)
    return written
