"""Repair Chatwoot rebuilds for existing Meta portals and enrich Marketplace context.

This layer fixes three production-only gaps that become visible when a Chatwoot
conversation/inbox is deleted and recreated:

* processed Matrix event IDs are destination-scoped, so stale IDs must be cleared
  for the affected room before rebuilding its Chatwoot conversation;
* self-authored Facebook/Marketplace messages arrive in Matrix as the admin puppet
  and must be mirrored immediately unless they were created by Chatwoot itself;
* Marketplace room/listing metadata needs registered Chatwoot custom attributes and
  labels so it is visible to agents rather than being hidden in raw JSON only.
"""
from __future__ import annotations

import sqlite3

import requests

import final_app as runtime
import media_context_v3 as media
import runtime_enhancements as enhancements

legacy = runtime.legacy
prod = runtime.prod

_ATTRIBUTE_DEFINITIONS = (
    ("matrix_room_id", "Matrix room", 0),
    ("meta_network", "Meta network", 0),
    ("meta_channel", "Meta channel", 0),
    ("meta_portal_id", "Meta portal ID", 0),
    ("meta_parent_portal_id", "Meta parent portal ID", 0),
    ("meta_thread_type", "Meta thread type", 1),
    ("meta_thread_name", "Meta thread name", 0),
    ("marketplace_listing_title", "Marketplace listing", 0),
    ("marketplace_counterparty_name", "Marketplace contact", 0),
)

_SCHEMA_CACHE: set[tuple[str, str]] = set()


def _existing_link(room_id: str):
    with legacy.db() as conn:
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


def _room_meta_event_ids(room_id: str) -> list[str]:
    """Return Matrix event IDs that mautrix-meta persisted for one portal room."""
    try:
        with media._meta_db() as conn:
            portal = conn.execute(
                "SELECT bridge_id, id, receiver FROM portal WHERE mxid = ? LIMIT 1",
                (room_id,),
            ).fetchone()
            if not portal:
                return []
            rows = conn.execute(
                "SELECT mxid FROM message WHERE bridge_id = ? AND room_id = ? "
                "AND room_receiver = ? AND mxid IS NOT NULL AND mxid != ''",
                (portal[0], portal[1], portal[2]),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [str(row[0]) for row in rows if row and row[0]]


def _processed_count(event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    count = 0
    with legacy.db() as conn:
        for event_id in event_ids:
            row = conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone()
            if row:
                count += 1
    return count


def reset_room_chatwoot_dedupe(room_id: str, *, reason: str) -> int:
    """Forget Chatwoot mirror dedupe for exactly one mautrix-meta portal.

    We intentionally do not wipe all processed events: Chatwoot outbound callback
    IDs and unrelated rooms remain protected from replay.
    """
    event_ids = _room_meta_event_ids(room_id)
    if not event_ids:
        return 0
    removed = 0
    with legacy.db() as conn:
        for event_id in event_ids:
            cursor = conn.execute("DELETE FROM processed_events WHERE event_id = ?", (event_id,))
            removed += max(0, int(cursor.rowcount or 0))
    if removed:
        print(
            f"reset stale Chatwoot room history room={room_id} events={removed} reason={reason}",
            flush=True,
        )
    return removed


def _normalize_collection(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("payload", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def ensure_chatwoot_marketplace_schema() -> None:
    if not legacy.configured():
        return
    account_id = str(legacy.get_setting("chatwoot_account_id"))
    cache_key = (legacy.get_setting("chatwoot_base_url").rstrip("/"), account_id)
    if cache_key in _SCHEMA_CACHE:
        return

    try:
        definitions = _normalize_collection(
            prod.cw_get(f"/api/v1/accounts/{account_id}/custom_attribute_definitions")
        )
        existing_keys = {str(item.get("attribute_key") or "") for item in definitions}
        for key, display_name, display_type in _ATTRIBUTE_DEFINITIONS:
            if key in existing_keys:
                continue
            try:
                media._cw_json(
                    "POST",
                    f"/api/v1/accounts/{account_id}/custom_attribute_definitions",
                    {
                        "attribute_display_name": display_name,
                        "attribute_display_type": display_type,
                        "attribute_description": "Synced automatically from mautrix-meta",
                        "attribute_key": key,
                        "attribute_model": 0,
                    },
                )
            except requests.HTTPError as exc:
                # A concurrent reconciler may have created the definition first.
                if exc.response is None or exc.response.status_code not in (409, 422):
                    raise

        labels = _normalize_collection(prod.cw_get(f"/api/v1/accounts/{account_id}/labels"))
        existing_labels = {str(item.get("title") or "") for item in labels}
        for title, description in (
            ("facebook", "Conversation bridged from Facebook"),
            ("marketplace", "Facebook Marketplace conversation"),
        ):
            if title in existing_labels:
                continue
            try:
                media._cw_json(
                    "POST",
                    f"/api/v1/accounts/{account_id}/labels",
                    {
                        "title": title,
                        "description": description,
                        "show_on_sidebar": True,
                    },
                )
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in (409, 422):
                    raise
        _SCHEMA_CACHE.add(cache_key)
    except Exception as exc:
        # Schema/context is metadata. It must never block customer message delivery.
        print(f"Chatwoot Marketplace schema sync failed: {type(exc).__name__}: {exc}", flush=True)


def _marketplace_name_attributes(context: dict) -> dict:
    name = str(context.get("name") or "").strip()
    attrs: dict[str, object] = {}
    if name:
        attrs["meta_thread_name"] = name
    if not context.get("is_marketplace"):
        return attrs

    counterparty = ""
    listing = ""
    if " · " in name:
        counterparty, listing = (part.strip() for part in name.split(" · ", 1))
    elif " - " in name:
        counterparty, listing = (part.strip() for part in name.split(" - ", 1))
    else:
        listing = name
    if listing:
        attrs["marketplace_listing_title"] = listing
    if counterparty:
        attrs["marketplace_counterparty_name"] = counterparty
    return attrs


def sync_conversation_context(room_id: str, link) -> None:
    """Best-effort context sync that also makes Marketplace fields visible in UI."""
    try:
        _base_context_sync(room_id, link)
    except Exception as exc:
        print(
            f"base conversation context sync failed room={room_id}: {type(exc).__name__}: {exc}",
            flush=True,
        )

    context = media.portal_context(room_id)
    if not context or not link:
        return
    ensure_chatwoot_marketplace_schema()

    extra = _marketplace_name_attributes(context)
    if not extra:
        return
    try:
        account_id = int(legacy.get_setting("chatwoot_account_id"))
        conversation_id = int(link["conversation_id"])
        conversation = prod.cw_get(
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}"
        )
        current = conversation.get("custom_attributes") if isinstance(conversation, dict) else {}
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(extra)
        if merged != current:
            media._cw_json(
                "POST",
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/custom_attributes",
                {"custom_attributes": merged},
            )
    except Exception as exc:
        print(
            f"Marketplace conversation metadata sync failed room={room_id}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def repair_deleted_conversation(room_id: str) -> bool:
    repaired = _base_repair_deleted(room_id)
    if repaired:
        reset_room_chatwoot_dedupe(room_id, reason="deleted_chatwoot_conversation")
    return repaired


def import_recent_history(room_id: str) -> int:
    """Rebuild a missing Chatwoot conversation even when all Matrix events were seen before."""
    repair_deleted_conversation(room_id)
    if _existing_link(room_id) is None:
        event_ids = _room_meta_event_ids(room_id)
        if _processed_count(event_ids):
            reset_room_chatwoot_dedupe(room_id, reason="missing_room_link")

    imported = _base_import_history(room_id)
    link = _existing_link(room_id)
    if link:
        sync_conversation_context(room_id, link)
    return imported


def mirror_matrix_event(room_id: str, event: dict, *, history: bool = False) -> bool:
    """Mirror Facebook-authored self messages immediately instead of waiting for reconcile.

    Chatwoot-originated Matrix events are already marked processed before /sync sees
    them, so event_seen remains the loop-prevention authority. An unseen admin-puppet
    event is therefore a genuine message authored on Facebook/Marketplace itself.
    """
    sender = str(event.get("sender") or "")
    event_id = str(event.get("event_id") or "")
    if (
        not history
        and sender == legacy.MATRIX_ADMIN_MXID
        and event_id
        and not legacy.event_seen(event_id)
    ):
        return _base_mirror_event(room_id, event, history=True)
    return _base_mirror_event(room_id, event, history=history)


def install() -> None:
    global _base_context_sync, _base_repair_deleted, _base_import_history, _base_mirror_event

    _base_context_sync = media.sync_conversation_context
    _base_repair_deleted = enhancements.repair_deleted_conversation
    _base_import_history = enhancements.import_recent_history
    _base_mirror_event = media.mirror_matrix_event

    media.sync_conversation_context = sync_conversation_context
    media.mirror_matrix_event = mirror_matrix_event
    enhancements.repair_deleted_conversation = repair_deleted_conversation
    enhancements.import_recent_history = import_recent_history

    # The installed live handler performs a global lookup of media.mirror_matrix_event,
    # so replacing that function is enough for the fast /sync path. Reconciliation
    # resolves enhancements.import_recent_history at runtime as well.
    ensure_chatwoot_marketplace_schema()
