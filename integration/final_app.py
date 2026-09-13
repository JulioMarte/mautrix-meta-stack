"""Final production entrypoint: applies deployment-only guards before Matrix sync starts."""
import os
import threading
import time

from werkzeug.middleware.proxy_fix import ProxyFix

# prod_app starts Matrix sync at import time. Suppress it until all final guards are installed.
_start_matrix_sync = os.getenv("START_MATRIX_SYNC", "true").lower() == "true"
os.environ["START_MATRIX_SYNC"] = "false"

import prod_app as prod  # noqa: E402

legacy = prod.legacy
application = prod.application
application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1, x_host=1)


def ensure_activation_boundary():
    if legacy.configured() and not legacy.get_setting("chatwoot_enabled_at_ms"):
        legacy.set_setting("chatwoot_enabled_at_ms", str(int(time.time() * 1000)))


base_matrix_event_to_chatwoot = prod.matrix_event_to_chatwoot


def guarded_matrix_event_to_chatwoot(room_id, event):
    cutoff_raw = legacy.get_setting("chatwoot_enabled_at_ms", "0")
    try:
        cutoff = int(cutoff_raw or 0)
        event_ts = int(event.get("origin_server_ts") or 0)
    except (TypeError, ValueError):
        cutoff = 0
        event_ts = 0
    if cutoff and event_ts and event_ts < cutoff:
        return
    return base_matrix_event_to_chatwoot(room_id, event)


legacy.matrix_event_to_chatwoot = guarded_matrix_event_to_chatwoot

base_save_settings = prod.save_settings


def save_settings_with_activation_boundary():
    response = base_save_settings()
    ensure_activation_boundary()
    return response


application.view_functions["save_settings"] = save_settings_with_activation_boundary

ensure_activation_boundary()
if _start_matrix_sync:
    threading.Thread(target=legacy.matrix_sync_loop, name="matrix-sync", daemon=True).start()
