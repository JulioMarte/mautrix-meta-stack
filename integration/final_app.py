"""Final production entrypoint: applies deployment-only guards before Matrix sync starts."""
import hmac
import os
import re
import threading
import time

from flask import abort, request
from werkzeug.middleware.proxy_fix import ProxyFix

# prod_app starts Matrix sync at import time. Suppress it until all final guards are installed.
_start_matrix_sync = os.getenv("START_MATRIX_SYNC", "true").lower() == "true"
os.environ["START_MATRIX_SYNC"] = "false"

import prod_app as prod  # noqa: E402

legacy = prod.legacy
application = prod.application
application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1, x_host=1)

if not re.fullmatch(r"[A-Za-z0-9._~-]{16,}", prod.PROXY_RESOLVER_SECRET):
    raise RuntimeError(
        "META_PROXY_RESOLVER_SECRET must be at least 16 URL-safe characters; use a long hex token"
    )


def internal_proxy_resolver():
    auth = request.authorization
    if not auth or auth.username != "mautrix" or not hmac.compare_digest(auth.password or "", prod.PROXY_RESOLVER_SECRET):
        abort(404)
    _, enabled, proxy = prod.effective_proxy()
    if not enabled:
        return {"proxy_url": ""}
    if not proxy:
        return {"error": "proxy enabled but not configured"}, 503
    return {"proxy_url": proxy}


# Reuse the original /internal/proxy route, but replace its unauthenticated implementation.
application.view_functions["proxy_resolver"] = internal_proxy_resolver
# Disable the transitional secret-in-path route so secrets never need to appear in URLs.
if "protected_proxy_resolver" in application.view_functions:
    application.view_functions["protected_proxy_resolver"] = lambda secret: abort(404)


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
