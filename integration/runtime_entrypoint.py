"""Production NiceGUI entrypoint.

Import the complete existing app first so all routes/runtime guards are installed,
then patch the shared admin-v2 chrome and register the simple cookie-login page.
"""
from __future__ import annotations

import nicegui_app
import meta_admin_patch

meta_admin_patch.install()

# Import after nicegui_app so the existing runtime, authentication guards and
# provisioning helpers are fully initialized before the cookie page is added.
# The module also repoints the Facebook Messenger sidebar item to its stable
# cookie-based route.
import meta_cookie_admin  # noqa: F401,E402


if __name__ in {"__main__", "__mp_main__"}:
    nicegui_app._legacy_ui.run()
