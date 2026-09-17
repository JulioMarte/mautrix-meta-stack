"""Production NiceGUI entrypoint.

Import the complete existing app first so all routes/runtime guards are installed,
then patch the shared admin-v2 chrome, install the managed Meta compatibility
redirect, bounded history safety, conversation lifecycle synchronization and safe
runtime diagnostics.
"""
from __future__ import annotations

import nicegui_app
import meta_admin_patch
import meta_legacy_redirect
import history_safety_v9
import conversation_lifecycle_v10
import conversation_lifecycle_hardening_v11
import conversation_lifecycle_callback_v10
import chatwoot_target_guard_v11
import meta_debug_observability

meta_admin_patch.install()
meta_legacy_redirect.install()
history_safety_v9.install()
conversation_lifecycle_v10.install()
conversation_lifecycle_hardening_v11.install()
conversation_lifecycle_callback_v10.install()
chatwoot_target_guard_v11.install()
meta_debug_observability.install()


if __name__ in {"__main__", "__mp_main__"}:
    nicegui_app._legacy_ui.run()
