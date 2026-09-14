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


def _unlink_room_conversation(room_id, conversation_id):
    with legacy.db() as conn:
        conn.execute(
            "DELETE FROM room_links WHERE room_id = ? AND conversation_id = ?",
            (room_id, conversation_id),
        )


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

    # Every supported Meta portal is allowed into Chatwoot. New rooms are created
    # in the configured inbox by prod.ensure_room_link. If an existing Chatwoot
    # conversation was moved to another inbox, detach the stale local mapping so
    # the next Meta message creates a fresh conversation in the configured inbox
    # instead of silently following the moved conversation elsewhere.
    if event.get("type") == "m.room.message":
        existing_conversation_id = _linked_conversation_id(room_id)
        if existing_conversation_id is not None:
            matches, conversation_id, actual_inbox_id = room_matches_configured_chatwoot_inbox(room_id)
            if not matches:
                configured_inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
                _unlink_room_conversation(room_id, conversation_id)
                print(
                    "matrix conversation relinked to configured inbox "
                    f"old_conversation={conversation_id} actual_inbox={actual_inbox_id} "
                    f"configured_inbox={configured_inbox_id}",
                    flush=True,
                )
    return base_matrix_event_to_chatwoot(room_id, event)


legacy.matrix_event_to_chatwoot = guarded_matrix_event_to_chatwoot

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


def _chatwoot_target():
    """Return the persisted destination identity without including credentials."""
    return (
        legacy.get_setting("chatwoot_base_url").strip().rstrip("/"),
        legacy.get_setting("chatwoot_account_id").strip(),
        legacy.get_setting("chatwoot_inbox_id").strip(),
    )


def _reset_chatwoot_target_state(old_target, new_target) -> None:
    """Reset destination-scoped state after a deliberate Chatwoot reconfiguration.

    Matrix room IDs and mautrix-meta history are the durable source. Chatwoot
    conversation IDs and processed-event dedupe rows belong to one Chatwoot target.
    Keeping them when the base URL/account/inbox changes would either point replies
    at stale conversations or prevent history from being rebuilt in the new inbox.
    """
    if old_target == new_target or not any(new_target):
        return
    with legacy.db() as conn:
        links = conn.execute("SELECT COUNT(*) AS n FROM room_links").fetchone()["n"]
        events = conn.execute("SELECT COUNT(*) AS n FROM processed_events").fetchone()["n"]
        conn.execute("DELETE FROM room_links")
        conn.execute("DELETE FROM processed_events")
    now_ms = str(int(time.time() * 1000))
    legacy.set_setting("chatwoot_enabled_at_ms", now_ms)
    for key in (
        "api_inbox_callback_verified_at",
        "api_inbox_delivery_verified_at",
        "last_chatwoot_matrix_delivery_at",
        "last_chatwoot_matrix_event_id",
        "last_chatwoot_matrix_conversation_id",
        "last_chatwoot_matrix_error",
        "last_chatwoot_matrix_error_at",
        "historical_direction_repair_needed",
    ):
        legacy.set_setting(key, "")
    print(
        "Chatwoot target changed; reset destination-scoped sync state "
        f"old={old_target!r} new={new_target!r} links={links} processed_events={events}",
        flush=True,
    )


base_save_settings = prod.save_settings


def save_settings_with_activation_boundary():
    old_target = _chatwoot_target()
    response = base_save_settings()
    new_target = _chatwoot_target()
    _reset_chatwoot_target_state(old_target, new_target)
    ensure_activation_boundary()
    return response


application.view_functions["save_settings"] = save_settings_with_activation_boundary

# Install operational improvements before the single Matrix /sync owner starts.
# Importing here is intentional: runtime_enhancements imports this module to reuse
# the final inbox guards, so all guards above must already exist first.
import runtime_enhancements as enhancements  # noqa: E402

enhancements.install_runtime_enhancements()

# Admin v2 fixes the first-sync invite checkpoint and provides the multi-page UI.
import admin_v2  # noqa: E402

admin_v2.install()

# A 200 response from Matrix /join is not considered sufficient: confirm the
# membership state really became join and retry a small bounded number of times.
import autojoin_verify  # noqa: E402

autojoin_verify.install()

# /sync is the fast path, but it is not an authoritative inventory of old invites
# or pre-existing Marketplace portals. Reconcile against Synapse's server-admin
# membership/state APIs and keep doing so periodically so Element is never required.
import meta_portal_reconcile  # noqa: E402

meta_portal_reconcile.install(admin_v2, start_background=_start_matrix_sync)

# Chatwoot API-channel HMAC compatibility, positive Matrix delivery verification,
# robust Chatwoot response parsing, and day-based bidirectional history import.
# Install after reconciliation so this layer wraps its mandatory auto-join settings.
import delivery_history_v2  # noqa: E402

delivery_history_v2.install()

# Import both media layers before installing either one. The hardening layer needs
# to capture PR #44's verified text callback before media_context_v3 replaces it.
import media_context_v3  # noqa: E402
import media_context_v3_hardening  # noqa: E402

# Attachments, authoritative from-me classification from mautrix-meta's read-only
# bridge database, and Marketplace conversation labels/custom attributes.
media_context_v3.install()

# Keep credentials away from external attachment object stores and normalize
# multipart replay markers across Chatwoot versions.
media_context_v3_hardening.install()

# Rebuild deleted Chatwoot conversations from room-scoped Matrix history, mirror
# Facebook-authored self messages on the fast /sync path, and register Marketplace
# metadata so listing context is visible in the Chatwoot sidebar.
import marketplace_rebuild_v4  # noqa: E402

marketplace_rebuild_v4.install()

ensure_activation_boundary()
if _start_matrix_sync:
    threading.Thread(target=legacy.matrix_sync_loop, name="matrix-sync", daemon=True).start()
