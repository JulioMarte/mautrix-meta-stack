"""Production NiceGUI entrypoint.

Import the complete existing app first so all routes/runtime guards are installed,
then patch the shared admin-v2 chrome, disable the old user-facing Meta onboarding
route, install bounded history safety, register the cookie-first page, and start
safe Meta pipeline diagnostics.
"""
from __future__ import annotations

import nicegui_app
import meta_admin_patch
import meta_legacy_redirect
import history_safety_v9
import meta_debug_observability

meta_admin_patch.install()
meta_legacy_redirect.install()
history_safety_v9.install()
meta_debug_observability.install()

# Register after the chrome patch so the page uses the same native admin shell.
import meta_cookie_page  # noqa: E402,F401


if __name__ in {"__main__", "__mp_main__"}:
    nicegui_app._legacy_ui.run()
