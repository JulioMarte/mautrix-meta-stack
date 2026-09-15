"""Final sync-integrity layer for Marketplace operator links and historical fidelity.

This layer addresses three production-only edge cases:

* mautrix-meta does not always preserve the Marketplace item's canonical URL in
  Matrix. When it does, use it. When it does not, expose the exact Facebook
  conversation URL that mautrix-meta itself derives from the portal ID, so the
  operator still has a deterministic way back to the Marketplace context instead
  of an empty field.
* rebuilding a deleted Chatwoot conversation must not erase the durable marker for
  Matrix events that originated from Chatwoot; erasing that marker causes the
  same outgoing reply to be imported a second time.
* preserve Matrix's original event timestamp in Chatwoot's supported
  `external_created_at` metadata for imported text/media messages.
"""
from __future__ import annotations

import contextvars
import json
from datetime import datetime, timezone

import requests

import final_app as runtime
import marketplace_listing_v5 as listing
import marketplace_rebuild_v4 as rebuild
import media_context_v3 as media

legacy = runtime.legacy
prod = runtime.prod

OPEN_URL_KEY = "marketplace_open_url"
OPEN_URL_DISPLAY = "Open Marketplace"
OLD_URL_KEY = listing.LISTING_URL_KEY
OWNED_DESCRIPTION = rebuild._OWNED_DESCRIPTION
_EVENT_TS_MS: contextvars.ContextVar[int] = contextvars.ContextVar("matrix_event_ts_ms", default=0)
_SCHEMA_CACHE: set[tuple[str, str]] = set()

# These rows prove that an event was created by Chatwoot (or deliberately skipped
# because Chatwoot already owned it). They must survive a Chatwoot conversation
# rebuild or the history importer can duplicate the agent's own reply.
_PROTECTED_DIRECTIONS = {
    "chatwoot_to_matrix_origin",
    "post_activation_admin_history_skipped",
}


def _facebook_thread_url(room_id: str) -> str:
    """Return the exact Facebook thread URL using mautrix-meta's portal identity.

    mautrix-meta itself uses `https://www.facebook.com/messages/t/<portal.ID>/`
    when it needs to expose a Facebook thread URL. Reusing that mapping is safe;
    unlike a Marketplace item ID, the thread ID is authoritative for the room.
    """
    context = media.portal_context(room_id)
    if not context or not context.get("is_marketplace"):
        return ""
    portal_id = str(context.get("portal_id") or "").strip()
    if not portal_id.isdigit():
        return ""
    return f"https://www.facebook.com/messages/t/{portal_id}/"


def _definition_collection(payload) -> list[dict]:
    return rebuild._normalize_collection(payload)


def ensure_open_marketplace_schema() -> None:
    if not legacy.configured():
        return
    account_id = str(legacy.get_setting("chatwoot_account_id"))
    cache_key = (legacy.get_setting("chatwoot_base_url").rstrip("/"), account_id)
    if cache_key in _SCHEMA_CACHE:
        return
    try:
        definitions = _definition_collection(
            prod.cw_get(f"/api/v1/accounts/{account_id}/custom_attribute_definitions")
        )
        by_key = {str(item.get("attribute_key") or ""): item for item in definitions}
        if OPEN_URL_KEY not in by_key:
            media._cw_json(
                "POST",
                f"/api/v1/accounts/{account_id}/custom_attribute_definitions",
                {
                    "attribute_display_name": OPEN_URL_DISPLAY,
                    "attribute_display_type": 4,
                    "attribute_description": OWNED_DESCRIPTION,
                    "attribute_key": OPEN_URL_KEY,
                    "attribute_model": 0,
                },
            )
        # The old field was too narrow: it rendered `---` whenever Meta omitted the
        # item URL. Remove only our own definition so operators see one useful link.
        old = by_key.get(OLD_URL_KEY)
        if old and str(old.get("attribute_description") or "") == OWNED_DESCRIPTION:
            definition_id = old.get("id")
            if definition_id is not None:
                try:
                    media._cw_json(
                        "DELETE",
                        f"/api/v1/accounts/{account_id}/custom_attribute_definitions/{int(definition_id)}",
                    )
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 404:
                        raise
        _SCHEMA_CACHE.add(cache_key)
    except Exception as exc:
        print(f"Chatwoot Marketplace open-link schema sync failed: {type(exc).__name__}: {exc}", flush=True)


def _best_marketplace_url(room_id: str, expected_title: str) -> tuple[str, str]:
    """Return (url, kind), preferring an actual item link over the thread fallback."""
    try:
        candidate = listing.discover_marketplace_listing(room_id, expected_title=expected_title)
    except Exception as exc:
        print(f"Marketplace item discovery failed room={room_id}: {type(exc).__name__}: {exc}", flush=True)
        candidate = {}
    item_url = str(candidate.get("url") or "") if isinstance(candidate, dict) else ""
    if item_url:
        return item_url, "listing"
    thread_url = _facebook_thread_url(room_id)
    return (thread_url, "thread") if thread_url else ("", "")


def sync_conversation_context(room_id: str, link) -> None:
    """Keep normal context sync, then guarantee one actionable Marketplace link."""
    _base_context_sync(room_id, link)
    context = media.portal_context(room_id)
    if not context or not context.get("is_marketplace") or not link:
        return
    ensure_open_marketplace_schema()
    expected_title = rebuild._marketplace_listing_title(context)
    url, kind = _best_marketplace_url(room_id, expected_title)
    if not url:
        return
    try:
        account_id = int(legacy.get_setting("chatwoot_account_id"))
        conversation_id = int(link["conversation_id"])
        conversation = prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}")
        current = conversation.get("custom_attributes") if isinstance(conversation, dict) else {}
        merged = dict(current) if isinstance(current, dict) else {}
        merged.pop(OLD_URL_KEY, None)
        merged[OPEN_URL_KEY] = url
        if merged != current:
            media._cw_json(
                "POST",
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/custom_attributes",
                {"custom_attributes": merged},
            )
        if kind == "thread":
            print(
                f"Marketplace item URL unavailable; using authoritative Facebook thread fallback "
                f"room={room_id} url={url}",
                flush=True,
            )
    except Exception as exc:
        print(f"Marketplace open-link sync failed room={room_id}: {type(exc).__name__}: {exc}", flush=True)


def reset_room_chatwoot_dedupe(room_id: str, *, reason: str) -> int:
    """Reset only Matrix->Chatwoot import markers, preserving Chatwoot-origin events."""
    event_ids = rebuild._room_meta_event_ids(room_id)
    if not event_ids:
        return 0
    removed = 0
    protected = 0
    with legacy.db() as conn:
        for event_id in event_ids:
            row = conn.execute(
                "SELECT direction FROM processed_events WHERE event_id = ? LIMIT 1",
                (event_id,),
            ).fetchone()
            if not row:
                continue
            direction = str(row["direction"] or "")
            if direction in _PROTECTED_DIRECTIONS:
                protected += 1
                continue
            cursor = conn.execute("DELETE FROM processed_events WHERE event_id = ?", (event_id,))
            removed += max(0, int(cursor.rowcount or 0))
    if removed or protected:
        print(
            f"reset stale Chatwoot room history room={room_id} events={removed} "
            f"preserved_outbound={protected} reason={reason}",
            flush=True,
        )
    return removed


def _external_created_at() -> str:
    timestamp_ms = int(_EVENT_TS_MS.get() or 0)
    if timestamp_ms <= 0:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _post_chatwoot_text(conversation_id: int, *, body: str, direction: str,
                        event_id: str, history: bool) -> dict:
    created_at = _external_created_at()
    if not created_at:
        return _base_post_text(
            conversation_id, body=body, direction=direction,
            event_id=event_id, history=history,
        )
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    attributes = {media.MIRROR_MARKER: True, "matrix_event_id": event_id}
    if history:
        import delivery_history_v2 as delivery
        attributes[delivery.HISTORY_MARKER] = True
    return legacy.cw_post(
        f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages",
        {
            "content": body,
            "message_type": direction,
            "private": False,
            "content_type": "text",
            "content_attributes": attributes,
            "external_created_at": created_at,
        },
    )


def _post_chatwoot_media(conversation_id: int, *, data: bytes, filename: str, mimetype: str,
                         caption: str, direction: str, event_id: str) -> dict:
    created_at = _external_created_at()
    if not created_at:
        return _base_post_media(
            conversation_id, data=data, filename=filename, mimetype=mimetype,
            caption=caption, direction=direction, event_id=event_id,
        )
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    response = requests.post(
        legacy.chatwoot_url(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"),
        headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        data={
            "content": caption,
            "message_type": direction,
            "private": "false",
            "content_type": "text",
            "external_created_at": created_at,
            "content_attributes": json.dumps({media.MIRROR_MARKER: True, "matrix_event_id": event_id}),
        },
        files={"attachments[]": (filename, data, mimetype)},
        timeout=45,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def mirror_matrix_event(room_id: str, event: dict, *, history: bool = False) -> bool:
    try:
        timestamp_ms = int(event.get("origin_server_ts") or 0)
    except (TypeError, ValueError):
        timestamp_ms = 0
    token = _EVENT_TS_MS.set(timestamp_ms)
    try:
        return _base_mirror(room_id, event, history=history)
    finally:
        _EVENT_TS_MS.reset(token)


def install() -> None:
    global _base_context_sync, _base_post_text, _base_post_media, _base_mirror

    _base_context_sync = listing.sync_conversation_context
    _base_post_text = media._post_chatwoot_text
    _base_post_media = media.post_chatwoot_media
    _base_mirror = media.mirror_matrix_event

    # Marketplace listing v5 calls its global sync_conversation_context from the
    # history wrapper, while the media layer calls its own global. Patch both.
    listing.sync_conversation_context = sync_conversation_context
    media.sync_conversation_context = sync_conversation_context

    # Preserve Chatwoot-origin markers when a deleted conversation is rebuilt.
    rebuild.reset_room_chatwoot_dedupe = reset_room_chatwoot_dedupe

    # Preserve the remote timestamp as Chatwoot-supported external metadata.
    media._post_chatwoot_text = _post_chatwoot_text
    media.post_chatwoot_media = _post_chatwoot_media
    media.mirror_matrix_event = mirror_matrix_event

    ensure_open_marketplace_schema()
