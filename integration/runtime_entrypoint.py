"""Production NiceGUI entrypoint.

Import the complete existing app first so all routes/runtime guards are installed.
The proven cookie-first Meta page is registered as the primary production path;
the newer managed onboarding at /admin/meta remains available for testing.
"""
from __future__ import annotations

import nicegui_app
import meta_cookie_page
import meta_admin_patch
import history_safety_v9
import conversation_lifecycle_v10
import conversation_lifecycle_hardening_v11
import conversation_lifecycle_callback_v10
import chatwoot_target_guard_v11
import meta_debug_observability
from db_connection_safety import install as install_db_connection_safety

# sqlite3.Connection's native context manager commits/rolls back but does not
# close the handle. Most of the legacy integration uses ``with legacy.db()`` and
# expects that scope to release resources, so enforce that behavior centrally in
# the production runtime before normal request/background work begins.
install_db_connection_safety(nicegui_app._legacy_ui.legacy)

meta_admin_patch.install()
history_safety_v9.install()
conversation_lifecycle_v10.install()
conversation_lifecycle_hardening_v11.install()
conversation_lifecycle_callback_v10.install()
chatwoot_target_guard_v11.install()
meta_debug_observability.install()


if __name__ in {"__main__", "__mp_main__"}:
    nicegui_app._legacy_ui.run()
