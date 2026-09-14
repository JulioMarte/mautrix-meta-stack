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

    # New Matrix rooms are allowed through: prod.ensure_room_link creates their
    # Chatwoot conversation in the configured inbox. Existing links are rechecked
    # on every message so moving a conversation to another inbox immediately
    # stops Meta -> Chatwoot delivery as well as Chatwoot -> Meta replies.
    if event.get("type") == "m.room.message" and _linked_conversation_id(room_id) is not None:
        matches, conversation_id, actual_inbox_id = room_matches_configured_chatwoot_inbox(room_id)
        if not matches:
            configured_inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
            print(
                "matrix delivery ignored "
                f"conversation={conversation_id} actual_inbox={actual_inbox_id} "
                f"configured_inbox={configured_inbox_id}",
                flush=True,
            )
            return
    return base_matrix_event_to_chatwoot(room_id, event)


legacy.matrix_event_to_chatwoot = guarded_matrix_event_to_chatwoot


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _conversation_inbox_id(data):
    """Extract a Chatwoot inbox ID from supported conversation response shapes."""
    if not isinstance(data, dict):
        return None

    direct = _int_or_none(data.get("inbox_id"))
    if direct is not None:
        return direct

    inbox = data.get("inbox")
    if isinstance(inbox, dict):
        nested = _int_or_none(inbox.get("id"))
        if nested is not None:
            return nested

    contact_inbox = data.get("contact_inbox")
    if isinstance(contact_inbox, dict):
        nested = _int_or_none(contact_inbox.get("inbox_id"))
        if nested is not None:
            return nested

    for key in ("conversation", "payload"):
        nested = data.get(key)
        if isinstance(nested, dict):
            result = _conversation_inbox_id(nested)
            if result is not None:
                return result
    return None


def _linked_conversation_id(room_id):
    with legacy.db() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM room_links WHERE room_id = ?",
            (room_id,),
        ).fetchone()
    return int(row["conversation_id"]) if row else None


def room_matches_configured_chatwoot_inbox(room_id):
    """Fail closed unless the linked conversation currently belongs to the configured inbox."""
    if not legacy.configured():
        raise RuntimeError("Chatwoot is not configured; refusing Chatwoot-to-Matrix delivery")

    conversation_id = _linked_conversation_id(room_id)
    if conversation_id is None:
        raise RuntimeError("Matrix room has no linked Chatwoot conversation; refusing delivery")

    account_id = int(legacy.get_setting("chatwoot_account_id"))
    configured_inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    conversation = prod.cw_get(
        f"/api/v1/accounts/{account_id}/conversations/{conversation_id}"
    )
    actual_inbox_id = _conversation_inbox_id(conversation)
    if actual_inbox_id is None:
        raise RuntimeError(
            "Chatwoot conversation response did not expose an inbox ID; refusing delivery"
        )
    return actual_inbox_id == configured_inbox_id, conversation_id, actual_inbox_id


base_send_matrix_message = legacy.send_matrix_message


def guarded_send_matrix_message(room_id, content, txn_id):
    """Deliver Chatwoot replies only when the conversation is still in the configured inbox."""
    matches, conversation_id, actual_inbox_id = room_matches_configured_chatwoot_inbox(room_id)
    if not matches:
        configured_inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
        print(
            "chatwoot delivery ignored "
            f"conversation={conversation_id} actual_inbox={actual_inbox_id} "
            f"configured_inbox={configured_inbox_id}",
            flush=True,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "outside_configured_chatwoot_inbox",
        }
    return base_send_matrix_message(room_id, content, txn_id)


legacy.send_matrix_message = guarded_send_matrix_message

base_save_settings = prod.save_settings


def save_settings_with_activation_boundary():
    response = base_save_settings()
    ensure_activation_boundary()
    return response


application.view_functions["save_settings"] = save_settings_with_activation_boundary

ensure_activation_boundary()
if _start_matrix_sync:
    threading.Thread(target=legacy.matrix_sync_loop, name="matrix-sync", daemon=True).start()
