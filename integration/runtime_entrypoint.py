"""Production NiceGUI entrypoint.

Import the complete existing app first so all routes/runtime guards are installed,
then patch the shared admin-v2 chrome so managed Meta onboarding appears in the
same sidebar users actually see at /admin/basic.
"""
from __future__ import annotations

import nicegui_app
import meta_admin_patch

meta_admin_patch.install()


if __name__ in {"__main__", "__mp_main__"}:
    nicegui_app._legacy_ui.run()
