"""Production NiceGUI entrypoint.

Import the complete existing app first so all routes/runtime guards are installed,
then patch the shared admin-v2 chrome, disable the old user-facing Meta onboarding
route, and register the cookie-first page used by the real deployment path.
"""
from __future__ import annotations

import nicegui_app
import meta_admin_patch
import meta_legacy_redirect

meta_admin_patch.install()
meta_legacy_redirect.install()

# Register after the chrome patch so the page uses the same native admin shell.
import meta_cookie_page  # noqa: E402,F401


if __name__ in {"__main__", "__mp_main__"}:
    nicegui_app._legacy_ui.run()
