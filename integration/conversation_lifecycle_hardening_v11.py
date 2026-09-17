"""Adversarial hardening for Chatwoot target changes.

Deletion operations and webhook signing credentials are scoped to one Chatwoot
base/account/inbox target. Reusing them after an operator switches targets can
misclassify a new conversation that happens to reuse an old numeric ID or trust a
callback signed by the old target. This layer clears only target-scoped state.
"""
from __future__ import annotations

import sys

import final_app as runtime

legacy = runtime.legacy
_base_runtime_reset = None
_base_admin_save = None


def _target() -> tuple[str, str, str]:
    return (
        legacy.get_setting("chatwoot_base_url").strip().rstrip("/"),
        legacy.get_setting("chatwoot_account_id").strip(),
        legacy.get_setting("chatwoot_inbox_id").strip(),
    )


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def clear_target_scoped_deletion_state() -> dict:
    deleted_operations = 0
    with legacy.db() as conn:
        if _table_exists(conn, "conversation_deletions"):
            deleted_operations = int(
                conn.execute("SELECT COUNT(*) AS n FROM conversation_deletions").fetchone()["n"]
            )
            conn.execute("DELETE FROM conversation_deletions")

    # These secrets authenticate callbacks from a specific Chatwoot target.
    # Keeping either one across a target switch would allow stale credentials to
    # remain trusted until an operator manually re-verifies the new target.
    for key in (
        "chatwoot_api_inbox_signing_secret",
        "api_inbox_callback_verified_at",
        "api_inbox_delivery_verified_at",
        "chatwoot_webhook_signing_secret",
        "webhook_registration_verified_at",
        "webhook_delivery_verified_at",
    ):
        legacy.set_setting(key, "")

    return {"deleted_operations": deleted_operations}


def _target_changed(old_target, new_target) -> bool:
    return tuple(old_target or ()) != tuple(new_target or ()) and any(new_target or ())


def _runtime_reset_with_deletion_state(old_target, new_target):
    result = _base_runtime_reset(old_target, new_target)
    if _target_changed(old_target, new_target):
        cleared = clear_target_scoped_deletion_state()
        print(
            "conversation lifecycle: Chatwoot target changed; cleared destructive state "
            f"operations={cleared['deleted_operations']}",
            flush=True,
        )
    return result


def _admin_save_with_deletion_state(*args, **kwargs):
    old_target = _target()
    result = _base_admin_save(*args, **kwargs)
    new_target = _target()
    if _target_changed(old_target, new_target):
        cleared = clear_target_scoped_deletion_state()
        print(
            "conversation lifecycle: NiceGUI Chatwoot target changed; cleared destructive state "
            f"operations={cleared['deleted_operations']}",
            flush=True,
        )
    return result


def install() -> None:
    global _base_runtime_reset, _base_admin_save

    if hasattr(runtime, "_reset_chatwoot_target_state") and not getattr(
        runtime, "_conversation_lifecycle_target_reset_v11", False
    ):
        _base_runtime_reset = runtime._reset_chatwoot_target_state
        runtime._reset_chatwoot_target_state = _runtime_reset_with_deletion_state
        runtime._conversation_lifecycle_target_reset_v11 = True

    admin_ui = sys.modules.get("nicegui_legacy")
    if admin_ui is not None and hasattr(admin_ui, "save_configuration") and not getattr(
        admin_ui, "_conversation_lifecycle_target_reset_v11", False
    ):
        _base_admin_save = admin_ui.save_configuration
        admin_ui.save_configuration = _admin_save_with_deletion_state
        admin_ui._conversation_lifecycle_target_reset_v11 = True
