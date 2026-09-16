"""Disable the old Meta onboarding UI without removing compatibility code.

The legacy /admin/meta page is still defined in nicegui_app for compatibility and
existing tests, but it must not be user-facing now that browser-cookie onboarding
is the supported path. This middleware redirects direct visits to the new page.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import RedirectResponse

import nicegui_app


def install() -> None:
    """Redirect the old user-facing Meta page to the cookie-first UI."""

    @nicegui_app.app.middleware("http")
    async def redirect_legacy_meta_page(request: Request, call_next):
        if request.url.path.rstrip("/") == "/admin/meta":
            return RedirectResponse("/admin/meta-cookie", status_code=307)
        return await call_next(request)
