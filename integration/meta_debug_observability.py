"""Safe, structured diagnostics for the Meta -> Matrix -> Chatwoot pipeline.

The integration intentionally does not log message bodies, cookies, access tokens,
proxy credentials, or raw Meta payloads. It logs counts and routing decisions so an
operator can tell whether a missing Chatwoot conversation was lost at discovery,
Matrix portal creation, history filtering, dedupe, or Chatwoot linking.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import yaml

import final_app as runtime
import meta_portal_reconcile as reconcile
import runtime_enhancements as enhancements

legacy = runtime.legacy

_ENABLED = os.getenv("INTEGRATION_META_DEBUG", "true").lower() not in {"0", "false", "no", "off"}
_INTERVAL = max(30, int(os.getenv("INTEGRATION_META_DEBUG_INTERVAL", "60") or "60"))
_STOP = threading.Event()
_LAST_ROOM_SIGNATURES: dict[str, str] = {}


def emit(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    print("META_DEBUG " + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str), flush=True)


def _mautrix_config_snapshot() -> dict:
    path = Path("/mautrix/config.yaml")
    if not path.is_file():
        return {"available": False}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        network = data.get("network") or {}
        thread = network.get("thread_backfill") or {}
        backfill = data.get("backfill") or {}
        return {
            "available": True,
            "mode": network.get("mode"),
            "marketplace_space": network.get("marketplace_space"),
            "thread_batch_count": thread.get("batch_count"),
            "thread_batch_delay": thread.get("batch_delay"),
            "backfill_enabled": backfill.get("enabled"),
            "max_initial_messages": backfill.get("max_initial_messages"),
            "max_catchup_messages": backfill.get("max_catchup_messages"),
        }
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _link_for_room(room_id: str) -> dict:
    try:
        row = enhancements.existing_room_link(room_id)
    except Exception as exc:
        return {"linked": False, "link_error": f"{type(exc).__name__}: {exc}"}
    if not row:
        return {"linked": False}
    return {
        "linked": True,
        "conversation_id": int(row["conversation_id"]),
        "contact_id": int(row["contact_id"]),
    }


def _history_diagnostics(room_id: str) -> dict:
    limit = enhancements.setting_int("history_import_limit", enhancements.DEFAULT_HISTORY_LIMIT)
    counters = Counter()
    try:
        response = enhancements._matrix_get(
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages",
            params={"dir": "b", "limit": limit},
        )
        chunk = (response.json() or {}).get("chunk") or []
    except Exception as exc:
        return {"history_limit": limit, "history_error": f"{type(exc).__name__}: {exc}"}

    bridge_bot = legacy.bridge_bot_mxid()
    for event in chunk:
        counters["events"] += 1
        if event.get("type") != "m.room.message":
            counters["non_message_events"] += 1
            continue
        counters["message_events"] += 1
        sender = str(event.get("sender") or "")
        if sender == legacy.MATRIX_ADMIN_MXID:
            counters["admin_sender"] += 1
            continue
        if bridge_bot and sender == bridge_bot:
            counters["bridge_bot_sender"] += 1
            continue
        content = event.get("content") or {}
        if content.get("msgtype") not in {"m.text", "m.notice"}:
            counters["unsupported_msgtype"] += 1
            continue
        if not str(content.get("body") or "").strip():
            counters["empty_body"] += 1
            continue
        event_id = str(event.get("event_id") or "")
        if event_id and legacy.event_seen(event_id):
            counters["already_processed"] += 1
            continue
        counters["eligible_unprocessed"] += 1

    return {"history_limit": limit, **dict(counters)}


def collect_once(*, force_rooms: bool = False) -> dict:
    config = _mautrix_config_snapshot()
    try:
        memberships = reconcile.user_memberships()
    except Exception as exc:
        emit("inventory_failed", error=f"{type(exc).__name__}: {exc}", mautrix_config=config)
        return {"error": str(exc)}

    membership_counts = Counter(memberships.values())
    room_totals = Counter()
    errors: list[str] = []

    for room_id, membership in memberships.items():
        if membership not in {"join", "invite"}:
            continue
        try:
            state = reconcile.room_admin_state(room_id)
            if reconcile._room_is_space(state):
                room_totals["spaces"] += 1
                continue
            verified, reason = reconcile.verified_meta_state(
                state, allow_pending_invite=membership == "invite"
            )
            if not verified:
                room_totals["unverified"] += 1
                signature_data = {
                    "membership": membership,
                    "verified": False,
                    "reason": reason,
                }
            else:
                room_totals["verified"] += 1
                link = _link_for_room(room_id)
                history = _history_diagnostics(room_id) if membership == "join" else {}
                if link.get("linked"):
                    room_totals["linked"] += 1
                else:
                    room_totals["unlinked"] += 1
                if history.get("eligible_unprocessed", 0):
                    room_totals["rooms_with_eligible_history"] += 1
                signature_data = {
                    "membership": membership,
                    "verified": True,
                    "verification": reason,
                    **link,
                    **history,
                }

            signature = json.dumps(signature_data, sort_keys=True, default=str)
            if force_rooms or _LAST_ROOM_SIGNATURES.get(room_id) != signature:
                emit("portal_state", room_id=room_id, **signature_data)
                _LAST_ROOM_SIGNATURES[room_id] = signature
        except Exception as exc:
            room_totals["errors"] += 1
            errors.append(f"{room_id}:{type(exc).__name__}:{exc}")
            emit("portal_diagnostic_failed", room_id=room_id, error=f"{type(exc).__name__}: {exc}")

    with legacy.db() as conn:
        room_links = conn.execute("SELECT COUNT(*) AS n FROM room_links").fetchone()["n"]
        processed = conn.execute("SELECT COUNT(*) AS n FROM processed_events").fetchone()["n"]

    summary = {
        "memberships": dict(membership_counts),
        "portal_counts": dict(room_totals),
        "room_links": int(room_links),
        "processed_events": int(processed),
        "chatwoot_configured": bool(legacy.configured()),
        "history_import_enabled": enhancements.setting_bool("import_history_on_join", True),
        "history_import_limit": enhancements.setting_int("history_import_limit", enhancements.DEFAULT_HISTORY_LIMIT),
        "history_import_days": enhancements.setting_int("history_import_days", 30, 0, 3650),
        "sync_policy_revision": enhancements.setting_int("sync_policy_revision", 0, 0, 2_000_000_000),
        "sync_reconcile_requested_at": legacy.get_setting("sync_reconcile_requested_at"),
        "sync_reconcile_completed_at": legacy.get_setting("sync_reconcile_completed_at"),
        "runtime_reconcile_pending": legacy.get_setting("runtime_reconcile_pending"),
        "runtime_reconcile_version": legacy.get_setting("runtime_reconcile_version"),
        "mautrix_config": config,
        "errors": errors[:10],
    }
    emit("inventory_summary", **summary)
    return summary


def _loop() -> None:
    _STOP.wait(5)
    first = True
    while not _STOP.is_set():
        try:
            collect_once(force_rooms=first)
        except Exception as exc:
            emit("diagnostic_loop_error", error=f"{type(exc).__name__}: {exc}")
        first = False
        _STOP.wait(_INTERVAL)


def install() -> None:
    if not _ENABLED:
        emit("observability_disabled")
        return
    if any(t.name == "meta-debug-observability" and t.is_alive() for t in threading.enumerate()):
        return
    emit("observability_started", interval_seconds=_INTERVAL)
    threading.Thread(target=_loop, name="meta-debug-observability", daemon=True).start()
