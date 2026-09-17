"""Compatibility redirect for the superseded manual Meta-cookie page.

Managed onboarding lives at /admin/meta. Older bookmarks that still point at the
short-lived manual cookie/cURL page are redirected to the supported flow instead
of exposing developer-tools instructions again.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import RedirectResponse

import nicegui_app


def install() -> None:
    """Redirect the deprecated cookie-first page to managed onboarding."""

    @nicegui_app.app.middleware("http")
    async def redirect_legacy_meta_page(request: Request, call_next):
        if request.url.path.rstrip("/") == "/admin/meta-cookie":
            return RedirectResponse("/admin/meta", status_code=307)
        return await call_next(request)
