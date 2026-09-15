"""Repair Chatwoot rebuilds and expose only operator-useful Facebook context.

Technical Matrix/mautrix identifiers remain internal to the integration. Chatwoot
agents should see business context, not bridge plumbing:

* Marketplace conversations get a single `marketplace` label.
* The conversation exposes the Marketplace listing title when mautrix-meta provides
  a Marketplace room name that can be split safely.
* The contact exposes a Facebook profile link when the remote Facebook identifier
  can be reduced to a numeric Facebook user ID.

The module also keeps the room-scoped history rebuild and fast-path mirroring fixes
needed when Chatwoot conversations/inboxes are deleted and recreated.
"""
from __future__ import annotations

import re
import sqlite3

import requests

import final_app as runtime
import media_context_v3 as media
import runtime_enhancements as enhancements

legacy = runtime.legacy
prod = runtime.prod

# Only these two fields are intentionally visible to agents / AI tooling.
_ATTRIBUTE_DEFINITIONS = (
    ("marketplace_listing_title", "Marketplace listing", 0, 0),
    ("facebook_profile_url", "Facebook profile", 4, 1),
)

# Definitions created by the previous implementation were technically useful for
# debugging but noisy and context-poor for operators. Remove only definitions that
# carry our exact ownership description; never delete a similarly named user field.
_OWNED_DESCRIPTION = "Synced automatically from mautrix-meta"
_OBSOLETE_ATTRIBUTE_KEYS = {
    "matrix_room_id",
    "meta_network",
    "meta_channel",
    "meta_portal_id",
    "meta_parent_portal_id",
    "meta_thread_type",
    "meta_thread_name",
    "marketplace_counterparty_name",
}
_OBSOLETE_CONVERSATION_KEYS = _OBSOLETE_ATTRIBUTE_KEYS | {"matrix_sender"}
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
    """Forget Chatwoot mirror dedupe for exactly one mautrix-meta portal."""
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


def _delete_owned_definition(account_id: str, definition: dict) -> None:
    definition_id = definition.get("id")
    if definition_id is None:
        return
    media._cw_json(
        "DELETE",
        f"/api/v1/accounts/{account_id}/custom_attribute_definitions/{int(definition_id)}",
    )


def _delete_owned_label(account_id: str, label: dict) -> None:
    label_id = label.get("id")
    if label_id is None:
        return
    media._cw_json("DELETE", f"/api/v1/accounts/{account_id}/labels/{int(label_id)}")


def ensure_chatwoot_marketplace_schema() -> None:
    """Keep Chatwoot's visible schema intentionally small and operator-oriented."""
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
        existing: dict[str, dict] = {
            str(item.get("attribute_key") or ""): item for item in definitions
            if item.get("attribute_key")
        }

        # Clean up only the noisy definitions that this integration itself created.
        for key in sorted(_OBSOLETE_ATTRIBUTE_KEYS):
            definition = existing.get(key)
            if not definition:
                continue
            if str(definition.get("attribute_description") or "") != _OWNED_DESCRIPTION:
                continue
            try:
                _delete_owned_definition(account_id, definition)
                existing.pop(key, None)
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise

        for key, display_name, display_type, attribute_model in _ATTRIBUTE_DEFINITIONS:
            if key in existing:
                continue
            try:
                media._cw_json(
                    "POST",
                    f"/api/v1/accounts/{account_id}/custom_attribute_definitions",
                    {
                        "attribute_display_name": display_name,
                        "attribute_display_type": display_type,
                        "attribute_description": _OWNED_DESCRIPTION,
                        "attribute_key": key,
                        "attribute_model": attribute_model,
                    },
                )
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in (409, 422):
                    raise

        labels = _normalize_collection(prod.cw_get(f"/api/v1/accounts/{account_id}/labels"))
        by_title = {str(item.get("title") or ""): item for item in labels if item.get("title")}

        # `facebook` is redundant when this API inbox is already the Facebook inbox.
        # Delete only the label we created in the previous version.
        facebook_label = by_title.get("facebook")
        if (
            facebook_label
            and str(facebook_label.get("description") or "") == "Conversation bridged from Facebook"
        ):
            try:
                _delete_owned_label(account_id, facebook_label)
                by_title.pop("facebook", None)
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise

        if "marketplace" not in by_title:
            try:
                media._cw_json(
                    "POST",
                    f"/api/v1/accounts/{account_id}/labels",
                    {
                        "title": "marketplace",
                        "description": "Facebook Marketplace conversation",
                        "show_on_sidebar": True,
                    },
                )
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in (409, 422):
                    raise

        _SCHEMA_CACHE.add(cache_key)
    except Exception as exc:
        # Context metadata must never block customer message delivery.
        print(f"Chatwoot Marketplace schema sync failed: {type(exc).__name__}: {exc}", flush=True)


def _marketplace_listing_title(context: dict) -> str:
    """Extract the listing title without duplicating the contact name in Chatwoot."""
    if not context.get("is_marketplace"):
        return ""
    name = str(context.get("name") or "").strip()
    if not name:
        return ""
    for separator in (" · ", " - "):
        if separator in name:
            _counterparty, listing = (part.strip() for part in name.split(separator, 1))
            return listing
    return name


def _customer_remote_id_for_room(room_id: str) -> str:
    """Resolve the other Facebook participant from mautrix-meta's authoritative DB."""
    try:
        with media._meta_db() as conn:
            self_ids = media._self_remote_ids(conn)
            portal = conn.execute(
                "SELECT bridge_id, id, receiver FROM portal WHERE mxid = ? LIMIT 1",
                (room_id,),
            ).fetchone()
            if not portal:
                return ""
            sql = (
                "SELECT sender_id FROM message "
                "WHERE bridge_id = ? AND room_id = ? AND room_receiver = ? "
            )
            params: list[object] = [portal[0], portal[1], portal[2]]
            if self_ids:
                placeholders = ",".join("?" for _ in self_ids)
                sql += f"AND sender_id NOT IN ({placeholders}) "
                params.extend(sorted(self_ids))
            sql += "ORDER BY timestamp DESC, rowid DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    return str(row[0] or "") if row else ""


def _facebook_numeric_id(raw_remote_id: str) -> str:
    """Normalize mautrix-meta Messenger IDs such as `12345:4@msgr` to `12345`."""
    value = str(raw_remote_id or "").strip()
    if not value:
        return ""
    candidate = value.split(":", 1)[0].split("@", 1)[0]
    return candidate if re.fullmatch(r"[0-9]{5,}", candidate) else ""


def facebook_profile_url(room_id: str) -> str:
    """Return a best-effort canonical Facebook profile URL for the remote contact.

    mautrix-meta uses Facebook identifiers for Messenger users. We still fail closed:
    only a numeric base identifier produces a link, so scoped/non-Facebook IDs are
    never turned into guessed URLs.
    """
    facebook_id = _facebook_numeric_id(_customer_remote_id_for_room(room_id))
    return f"https://www.facebook.com/profile.php?id={facebook_id}" if facebook_id else ""


def _sync_contact_profile_link(account_id: int, contact_id: int, room_id: str) -> None:
    url = facebook_profile_url(room_id)
    if not url:
        return
    # Chatwoot merges incoming contact custom_attributes with existing values on PUT.
    enhancements.chatwoot_request(
        "PUT",
        f"/api/v1/accounts/{account_id}/contacts/{contact_id}",
        json={"custom_attributes": {"facebook_profile_url": url}},
        headers={"Content-Type": "application/json"},
    )


def _sync_marketplace_label(account_id: int, conversation_id: int, is_marketplace: bool) -> None:
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels")
    labels = data.get("payload") if isinstance(data, dict) else []
    current = {str(item) for item in labels or [] if item}
    desired = set(current)
    if is_marketplace:
        desired.add("marketplace")
    else:
        desired.discard("marketplace")
    # Remove only the redundant label previously managed by this integration.
    desired.discard("facebook")
    if desired != current:
        media._cw_json(
            "POST",
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels",
            {"labels": sorted(desired)},
        )


def sync_conversation_context(room_id: str, link) -> None:
    """Expose only listing context and a reusable Facebook contact link."""
    context = media.portal_context(room_id)
    if not context or not link:
        return
    ensure_chatwoot_marketplace_schema()

    try:
        account_id = int(legacy.get_setting("chatwoot_account_id"))
        conversation_id = int(link["conversation_id"])
        contact_id = int(link["contact_id"])

        conversation = prod.cw_get(
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}"
        )
        current = conversation.get("custom_attributes") if isinstance(conversation, dict) else {}
        merged = dict(current) if isinstance(current, dict) else {}
        for key in _OBSOLETE_CONVERSATION_KEYS:
            merged.pop(key, None)

        listing = _marketplace_listing_title(context)
        if listing:
            merged["marketplace_listing_title"] = listing
        else:
            merged.pop("marketplace_listing_title", None)

        if merged != current:
            media._cw_json(
                "POST",
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/custom_attributes",
                {"custom_attributes": merged},
            )

        _sync_marketplace_label(account_id, conversation_id, bool(context.get("is_marketplace")))
        _sync_contact_profile_link(account_id, contact_id, room_id)
    except Exception as exc:
        print(
            f"Marketplace operator context sync failed room={room_id}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def repair_deleted_conversation(room_id: str) -> bool:
    repaired = _base_repair_deleted(room_id)
    if repaired:
        reset_room_chatwoot_dedupe(room_id, reason="deleted_chatwoot_conversation")
    return repaired


def import_recent_history(room_id: str) -> int:
    """Rebuild a missing Chatwoot conversation even when Matrix events were seen before."""
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
    """Mirror Facebook-authored self messages immediately instead of waiting for reconcile."""
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
    global _base_repair_deleted, _base_import_history, _base_mirror_event

    _base_repair_deleted = enhancements.repair_deleted_conversation
    _base_import_history = enhancements.import_recent_history
    _base_mirror_event = media.mirror_matrix_event

    media.sync_conversation_context = sync_conversation_context
    media.mirror_matrix_event = mirror_matrix_event
    enhancements.repair_deleted_conversation = repair_deleted_conversation
    enhancements.import_recent_history = import_recent_history

    # The installed live handler performs a global lookup of media.mirror_matrix_event,
    # and reconciliation resolves enhancements.import_recent_history at runtime.
    ensure_chatwoot_marketplace_schema()
