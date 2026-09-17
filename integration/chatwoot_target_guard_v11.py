"""Fail-closed guards for Chatwoot target changes and callback scope.

A Chatwoot base URL/account/inbox is a security and identity boundary. Numeric
conversation IDs, webhook secrets, API-inbox HMAC tokens and deletion tombstones
must never carry across that boundary.
"""
from __future__ import annotations

import final_app as runtime
import delivery_history_v2 as delivery
import nicegui_app

legacy = runtime.legacy
_base_runtime_reset = runtime._reset_chatwoot_target_state
_base_save_configuration = nicegui_app._legacy_ui.save_configuration
_base_configured_inbox_matches = delivery._configured_inbox_matches
_INSTALLED = False


def _target_changed(old_target, new_target) -> bool:
    return old_target != new_target and any(new_target)


def reset_chatwoot_target_state(old_target, new_target) -> None:
    """Reset every state item whose meaning depends on one Chatwoot target."""
    _base_runtime_reset(old_target, new_target)
    if not _target_changed(old_target, new_target):
        return

    deleted_operations = 0
    with legacy.db() as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_deletions'"
        ).fetchone()
        if table:
            cursor = conn.execute("DELETE FROM conversation_deletions")
            deleted_operations = max(0, int(cursor.rowcount or 0))

    # Both callback secrets authenticate data from one specific Chatwoot target.
    # Keeping either one after a target change creates a stale-source replay window.
    for key in (
        "chatwoot_api_inbox_signing_secret",
        "chatwoot_webhook_signing_secret",
        "api_inbox_callback_verified_at",
        "api_inbox_delivery_verified_at",
        "webhook_registration_verified_at",
        "webhook_delivery_verified_at",
        "webhook_last_delivery_id",
    ):
        legacy.set_setting(key, "")

    print(
        "Chatwoot target guard: invalidated destination-scoped authentication "
        f"and deletion state operations={deleted_operations}",
        flush=True,
    )


def save_configuration(*args, **kwargs):
    """Make the NiceGUI configuration path honor the same target reset as Flask."""
    old_target = runtime._chatwoot_target()
    result = _base_save_configuration(*args, **kwargs)
    new_target = runtime._chatwoot_target()
    runtime._reset_chatwoot_target_state(old_target, new_target)
    return result


def configured_inbox_matches(payload: dict) -> bool:
    """Reject an explicit account mismatch before the existing inbox check."""
    account = payload.get("account") or {}
    candidate = account.get("id") or payload.get("account_id")
    if candidate not in (None, "") and str(candidate) != str(legacy.get_setting("chatwoot_account_id")):
        return False
    return _base_configured_inbox_matches(payload)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    runtime._reset_chatwoot_target_state = reset_chatwoot_target_state
    nicegui_app._legacy_ui.save_configuration = save_configuration
    delivery._configured_inbox_matches = configured_inbox_matches
    _INSTALLED = True
