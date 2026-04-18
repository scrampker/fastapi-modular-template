"""scottycore.web — shared UI primitives for scottycore-based apps.

Public helpers exposed here:

- :func:`install_static` — mount scottycore's shipped static assets
  (JS / CSS / icons / manifest) on a consumer app's FastAPI instance,
  with optional consumer-side overlay.
- :func:`setup_jinja_brand` — inject the current :class:`BrandConfig`
  as Jinja globals (``brand``, ``brand_display_name``,
  ``orchestrator_display``) on a consumer-owned Jinja environment so
  every template can reference ``{{ brand.display_name }}`` etc.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from jinja2 import Environment

__all__ = ["install_static", "setup_jinja_brand"]


def setup_jinja_brand(env: "Environment") -> None:
    """Inject the current brand as Jinja globals on *env*.

    Templates rendered by *env* can then reference::

        {{ brand.display_name }}     → "Scotty" (or fork override)
        {{ brand.framework_name }}   → "scottycore"
        {{ brand.domain_root }}      → "scotty.consulting"
        {{ orchestrator_display }}   → "ScottyDev" — convenience label

    Idempotent: calling twice just rewrites the same globals. Use this
    when a consumer app builds its own ``Environment`` rather than
    reusing :data:`scottycore.web.router.templates` — that module
    already injects these globals on its own instance.
    """
    from scottycore.core.brand import get_brand

    brand = get_brand()
    orchestrator_display: str = (
        f"{brand.display_name}Dev"
        if brand.orchestrator_name == f"{brand.family_name}dev"
        else brand.orchestrator_name
    )
    env.globals["brand"] = brand
    env.globals["brand_display_name"] = brand.display_name
    env.globals["orchestrator_display"] = orchestrator_display


def install_static(
    app: "FastAPI",
    *,
    overlay_dir: str | None = None,
    mount_path: str = "/static",
    overlay_mount_path: str = "/static-app",
    name: str = "static",
) -> None:
    """Mount scottycore's shipped static assets on *app* at *mount_path*.

    The pip-installed scottycore package ships a ``web/static/`` directory
    containing the JavaScript, CSS, icons, and PWA manifest that scottycore's
    Jinja templates reference at ``/static/*``. Consumer apps that build
    their own FastAPI instance must expose these assets themselves —
    scottycore's own :func:`scottycore.main.create_app` does so internally,
    but consumer apps that mount ``scottycore.web.router.web_router`` without
    wiring scottycore's full application factory need to call this helper
    (or replicate its logic).

    Parameters
    ----------
    app:
        The consumer app's FastAPI instance.
    overlay_dir:
        Optional absolute path to a consumer-owned static directory. When
        provided (and the directory exists), it is mounted at
        *overlay_mount_path* so consumer apps can ship additional static
        files alongside scottycore's. Pass ``overlay_mount_path=""`` to skip
        the overlay mount even if *overlay_dir* is set.
    mount_path:
        URL prefix for scottycore's static mount. Defaults to ``"/static"``
        to match scottycore's own template references. Change only if you
        are intentionally namespacing.
    overlay_mount_path:
        URL prefix for the consumer overlay mount. Defaults to
        ``"/static-app"``. Set to ``""`` to disable.
    name:
        Starlette mount name for scottycore's mount. Defaults to ``"static"``.
        The overlay mount is registered as ``f"{name}-app"``.

    Notes
    -----
    This helper is idempotent at the filesystem level — it will simply no-op
    if scottycore's installed ``web/static`` directory is missing (which
    would indicate a broken install). The FastAPI mount itself is **not**
    idempotent; calling this helper twice with the same *mount_path* will
    raise a duplicate-mount error from Starlette.
    """
    from fastapi.staticfiles import StaticFiles

    core_static = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(core_static):
        app.mount(mount_path, StaticFiles(directory=core_static), name=name)

    if overlay_dir and overlay_mount_path and os.path.isdir(overlay_dir):
        app.mount(
            overlay_mount_path,
            StaticFiles(directory=overlay_dir),
            name=f"{name}-app",
        )
