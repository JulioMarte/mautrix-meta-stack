"""Fail-closed guards for Chatwoot target changes and callback scope.

A Chatwoot base URL/account/inbox is a security and identity boundary. Numeric
conversation IDs, webhook secrets, API-inbox HMAC tokens and deletion tombstones
must never carry across that boundary. Reconfiguration shares the destructive
lifecycle lock so old-target callbacks cannot interleave with a target switch.
"""
from __future__ import annotations

import contextvars

import conversation_lifecycle_v10 as lifecycle
import delivery_history_v2 as delivery
import final_app as runtime
import nicegui_app
import runtime_enhancements as enhancements

legacy = runtime.legacy
_base_runtime_reset = runtime._reset_chatwoot_target_state
_base_save_configuration = nicegui_app._legacy_ui.save_configuration
_base_configured_inbox_matches = delivery._configured_inbox_matches
_base_account_webhook_signature = nicegui_app._legacy_ui.verify_chatwoot_signature
_base_api_inbox_signature = enhancements.verify_inbox_signature
_base_callback_handler = None
_VERIFIED_API_INBOX_TARGET: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "verified_api_inbox_target", default=None
)
_INSTALLED = False


def _target_changed(old_target, new_target) -> bool:
    return old_target != new_target and any(new_target)


def _reconfiguring() -> bool:
    return legacy.get_setting("chatwoot_target_reconfiguring") == "1"


def _current_target() -> tuple[str, str, str]:
    target = runtime._chatwoot_target()
    return (str(target[0]), str(target[1]), str(target[2]))


def reset_chatwoot_target_state(old_target, new_target) -> None:
    """Reset every state item whose meaning depends on one Chatwoot target."""
    with lifecycle._RECONCILE_LOCK:
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
    """Make NiceGUI target changes atomic relative to destructive lifecycle work.

    If the underlying save raises after writing only part of the new target, still
    invalidate old-target mappings, tombstones, and callback secrets before exposing
    the partially changed configuration to callbacks.
    """
    with lifecycle._RECONCILE_LOCK:
        old_target = _current_target()
        legacy.set_setting("chatwoot_target_reconfiguring", "1")
        try:
            try:
                result = _base_save_configuration(*args, **kwargs)
            except BaseException:
                new_target = _current_target()
                if _target_changed(old_target, new_target):
                    runtime._reset_chatwoot_target_state(old_target, new_target)
                raise
            new_target = _current_target()
            runtime._reset_chatwoot_target_state(old_target, new_target)
            return result
        finally:
            legacy.set_setting("chatwoot_target_reconfiguring", "0")


def configured_inbox_matches(payload: dict) -> bool:
    """Reject an explicit account mismatch before the existing inbox check."""
    account = payload.get("account") or {}
    candidate = account.get("id") or payload.get("account_id")
    if candidate not in (None, "") and str(candidate) != str(legacy.get_setting("chatwoot_account_id")):
        return False
    return _base_configured_inbox_matches(payload)


def verify_api_inbox_signature(raw_body: bytes, signature: str, timestamp: str,
                               now: int | None = None) -> bool:
    """Bind one successful HMAC verification to the exact target it authenticated."""
    _VERIFIED_API_INBOX_TARGET.set(None)
    with lifecycle._RECONCILE_LOCK:
        if _reconfiguring():
            raise RuntimeError("Chatwoot target reconfiguration in progress")
        result = _base_api_inbox_signature(raw_body, signature, timestamp, now=now)
        _VERIFIED_API_INBOX_TARGET.set(_current_target())
        return result


def callback_handler(payload: dict, *, signature_verified: bool) -> dict:
    """Reject callbacks if the target changed after their HMAC was verified."""
    try:
        with lifecycle._RECONCILE_LOCK:
            if _reconfiguring():
                raise RuntimeError("Chatwoot target reconfiguration in progress")
            if signature_verified:
                verified_target = _VERIFIED_API_INBOX_TARGET.get()
                if verified_target is None:
                    raise RuntimeError("Chatwoot callback has no bound verification target")
                if verified_target != _current_target():
                    raise RuntimeError("Chatwoot target changed after callback signature verification")
            return _base_callback_handler(payload, signature_verified=signature_verified)
    finally:
        _VERIFIED_API_INBOX_TARGET.set(None)


def verify_account_webhook_signature(raw_body: bytes, signature: str, timestamp: str,
                                     now: int | None = None) -> bool:
    with lifecycle._RECONCILE_LOCK:
        if _reconfiguring():
            raise RuntimeError("Chatwoot target reconfiguration in progress")
        return _base_account_webhook_signature(raw_body, signature, timestamp, now=now)


def install() -> None:
    global _INSTALLED, _base_callback_handler
    if _INSTALLED:
        return
    _base_callback_handler = delivery.callback_outgoing_handler
    runtime._reset_chatwoot_target_state = reset_chatwoot_target_state
    nicegui_app._legacy_ui.save_configuration = save_configuration
    nicegui_app._legacy_ui.verify_chatwoot_signature = verify_account_webhook_signature
    enhancements.verify_inbox_signature = verify_api_inbox_signature
    delivery._configured_inbox_matches = configured_inbox_matches
    delivery.callback_outgoing_handler = callback_handler
    _INSTALLED = True
