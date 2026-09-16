"""Bounded history policy and natural incremental resync.

The Chatwoot history window is intentionally a local Matrix/Synapse retention view,
not a request to fetch arbitrary years from Meta. Keep a hard product limit so an
operator cannot accidentally configure an extreme value, and when the window grows
(e.g. 5 -> 30 days) immediately reconcile existing verified portals. Matrix event
IDs remain the dedupe boundary, so only newly-in-window events are copied.
"""
from __future__ import annotations

import threading
import time

import delivery_history_v2 as delivery
import meta_portal_reconcile as reconcile
import runtime_enhancements as enhancements

legacy = enhancements.legacy

HARD_MAX_HISTORY_DAYS = 30
_RESYNC_LOCK = threading.Lock()

_base_operations_state = enhancements.operations_state
_base_save_operations_settings = enhancements.save_operations_settings


def _raw_history_days() -> int:
    try:
        value = int(legacy.get_setting("history_import_days", str(delivery.DEFAULT_HISTORY_DAYS)) or 0)
    except (TypeError, ValueError):
        value = delivery.DEFAULT_HISTORY_DAYS
    return max(0, min(HARD_MAX_HISTORY_DAYS, value))


def operations_state() -> dict:
    state = _base_operations_state()
    days = _raw_history_days()
    state["history_limit"] = days
    state["history_days"] = days
    state["history_hard_max_days"] = HARD_MAX_HISTORY_DAYS
    return state


def _incremental_resync(old_days: int, new_days: int) -> None:
    if not _RESYNC_LOCK.acquire(blocking=False):
        return
    try:
        # Let the settings transaction finish before the authoritative reconcile
        # reads the new window. This path talks to Synapse/Chatwoot, not Meta.
        time.sleep(0.5)
        result = reconcile.reconcile_meta_portals()
        print(
            "History window expanded; incremental Matrix resync completed "
            f"old_days={old_days} new_days={new_days} "
            f"imported={result.get('history_imported', 0)} linked={result.get('linked', 0)}",
            flush=True,
        )
    except Exception as exc:
        print(
            "History window incremental resync failed "
            f"old_days={old_days} new_days={new_days}: {type(exc).__name__}: {exc}",
            flush=True,
        )
    finally:
        _RESYNC_LOCK.release()


def save_operations_settings(*, auto_join: bool, import_history: bool, history_limit: int,
                             sync_profiles: bool, repair_deleted: bool) -> None:
    try:
        requested = int(history_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("History window must be a whole number of days") from exc
    if requested < 0 or requested > HARD_MAX_HISTORY_DAYS:
        raise ValueError(
            f"History window must be between 0 and {HARD_MAX_HISTORY_DAYS} days. "
            "Large historical scans are intentionally blocked."
        )

    old_days = _raw_history_days()
    _base_save_operations_settings(
        auto_join=True,
        import_history=import_history,
        history_limit=requested,
        sync_profiles=sync_profiles,
        repair_deleted=repair_deleted,
    )
    # Defensive persistence guard in case a lower layer changes its own maximum.
    legacy.set_setting("history_import_days", str(requested))

    if import_history and requested > old_days:
        threading.Thread(
            target=_incremental_resync,
            args=(old_days, requested),
            name="history-window-resync",
            daemon=True,
        ).start()


def install() -> None:
    # Clamp any previously-saved unsafe value immediately on deployment.
    try:
        raw = int(legacy.get_setting("history_import_days", str(delivery.DEFAULT_HISTORY_DAYS)) or 0)
    except (TypeError, ValueError):
        raw = delivery.DEFAULT_HISTORY_DAYS
    if raw > HARD_MAX_HISTORY_DAYS:
        legacy.set_setting("history_import_days", str(HARD_MAX_HISTORY_DAYS))
        print(
            f"History window clamped from {raw} to {HARD_MAX_HISTORY_DAYS} days by safety policy",
            flush=True,
        )
    elif raw < 0:
        legacy.set_setting("history_import_days", "0")

    # Keep every lower layer consistent with the product hard limit.
    delivery.MAX_HISTORY_DAYS = HARD_MAX_HISTORY_DAYS
    enhancements.operations_state = operations_state
    enhancements.save_operations_settings = save_operations_settings
