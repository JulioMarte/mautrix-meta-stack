"""Security and compatibility guards for media_context_v3.

Keep the media bridge fail-closed for delivery while making conversation metadata
best-effort. In particular, never forward the Chatwoot API token to an external
object-store URL contained in an attachment callback.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlparse

import requests

import delivery_history_v2 as delivery
import media_context_v3 as media

legacy = media.legacy

# Keep both implementations available. Text-only callbacks must use the media-core
# path too: unlike the older PR #44 handler, it records the Matrix event ID as a
# Chatwoot-origin event. Without that durable marker the periodic history importer
# sees the Matrix echo a few seconds later and creates a second outgoing Chatwoot
# row even though Meta received the message only once. The legacy reference remains
# only so regression tests can prove it is never selected for live replies.
_legacy_text_only_outgoing = delivery.handle_chatwoot_outgoing_verified
_original_media_outgoing = media.handle_chatwoot_outgoing
_original_outgoing = _original_media_outgoing
_original_mirror_matrix_event = media.mirror_matrix_event
_original_live_matrix_event = media.live_matrix_event
_original_download_matrix_media = media.download_matrix_media


def _truthy_marker(value) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"true", "1", "yes"}


def _same_origin(left: str, right: str) -> bool:
    a = urlparse(left)
    b = urlparse(right)
    return (
        a.scheme.lower(), a.hostname, a.port or (443 if a.scheme.lower() == "https" else 80)
    ) == (
        b.scheme.lower(), b.hostname, b.port or (443 if b.scheme.lower() == "https" else 80)
    )


def _safe_download_headers(url: str, headers: dict | None) -> dict:
    result = dict(headers or {})
    if "api_access_token" in result:
        base = legacy.get_setting("chatwoot_base_url")
        if not base or not _same_origin(url, base):
            result.pop("api_access_token", None)
    return result


_original_bounded_download = media._bounded_download


def bounded_download(url: str, *, headers: dict | None = None):
    return _original_bounded_download(url, headers=_safe_download_headers(url, headers))


def download_matrix_media(content: dict):
    """Try both supported Synapse media download paths for Meta attachments."""
    try:
        return _original_download_matrix_media(content)
    except requests.RequestException:
        mxc = str(content.get("url") or "")
        server, media_id = media._mxc_parts(mxc)
        url = (
            f"{legacy.MATRIX_HOMESERVER}/_matrix/media/v3/download/"
            f"{quote(server, safe='')}/{quote(media_id, safe='')}"
        )
        data, response_mime = bounded_download(url, headers=legacy.matrix_headers())
        info = content.get("info") or {}
        mimetype = str(info.get("mimetype") or response_mime or "application/octet-stream")
        filename = str(content.get("filename") or content.get("body") or "attachment").strip() or "attachment"
        body = str(content.get("body") or "").strip()
        caption = body if body and body != filename else ""
        return data, filename, mimetype, caption


_original_context_sync = media.sync_conversation_context


def safe_sync_conversation_context(room_id: str, link) -> None:
    try:
        _original_context_sync(room_id, link)
    except Exception as exc:
        # Labels/custom attributes improve routing, but must not block customer
        # messages or media if a particular Chatwoot version rejects metadata.
        print(
            f"Chatwoot conversation context sync failed room={room_id}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def post_chatwoot_media(conversation_id: int, *, data: bytes, filename: str, mimetype: str,
                        caption: str, direction: str, event_id: str) -> dict:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    # Rails multipart parsing understands nested field names reliably. Sending a
    # JSON string as `content_attributes` can be persisted as a string by some
    # Chatwoot versions, which would defeat the replay guard.
    form = {
        "content": caption,
        "message_type": direction,
        "private": "false",
        "content_type": "text",
        f"content_attributes[{media.MIRROR_MARKER}]": "true",
        "content_attributes[matrix_event_id]": event_id,
    }
    response = requests.post(
        legacy.chatwoot_url(
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
        ),
        headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        data=form,
        files={"attachments[]": (filename, data, mimetype)},
        timeout=45,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _normalize_sticker_event(event: dict) -> dict:
    """Normalize Matrix `m.sticker` into the media core's room-message shape."""
    if event.get("type") != "m.sticker":
        return event
    normalized = dict(event)
    content = dict(event.get("content") or {})
    content["msgtype"] = "m.sticker"
    normalized["type"] = "m.room.message"
    normalized["content"] = content
    return normalized


def mirror_matrix_event(room_id: str, event: dict, *, history: bool = False) -> bool:
    return _original_mirror_matrix_event(
        room_id,
        _normalize_sticker_event(event),
        history=history,
    )


def live_matrix_event(room_id: str, event: dict):
    # Normalize first so stickers pass the same activation boundary, stale-link
    # repair, inbox validation, provenance and dedupe guards as other attachments.
    return _original_live_matrix_event(room_id, _normalize_sticker_event(event))


def handle_chatwoot_outgoing(payload: dict, *, signature_verified: bool) -> dict:
    attributes = payload.get("content_attributes") or {}
    if isinstance(attributes, dict):
        if _truthy_marker(attributes.get(delivery.HISTORY_MARKER)):
            return {"ok": True, "ignored": True, "reason": "history_import"}
        if _truthy_marker(attributes.get(media.MIRROR_MARKER)):
            return {"ok": True, "ignored": True, "reason": "matrix_mirror"}

    # Use one delivery implementation for text and attachments. The media-core
    # handler marks every newly-created Matrix event with
    # `chatwoot_to_matrix_origin` before the history importer can mirror it back
    # into Chatwoot. The previous split routed text through the legacy handler,
    # which only marked `chatwoot:<message_id>` and caused the visible duplicate.
    return _original_media_outgoing(payload, signature_verified=signature_verified)


def import_recent_history(room_id: str) -> int:
    # Production has the mautrix data volume mounted read-only. Unit tests and
    # disaster-recovery/offline environments may not. In that case preserve the
    # already-proven PR #44 importer rather than silently misclassifying history or
    # attempting remote profile lookups from an unavailable bridge database.
    if not os.path.isfile(media.META_DB_PATH):
        return delivery.import_recent_history_days(room_id)
    return media.import_recent_history(room_id)


def install() -> None:
    media._bounded_download = bounded_download
    media.download_matrix_media = download_matrix_media
    media.sync_conversation_context = safe_sync_conversation_context
    media.post_chatwoot_media = post_chatwoot_media
    media.mirror_matrix_event = mirror_matrix_event
    media.live_matrix_event = live_matrix_event
    media.enhancements.enhanced_live_matrix_event = live_matrix_event
    media.prod.matrix_event_to_chatwoot = mirror_matrix_event
    legacy.matrix_event_to_chatwoot = live_matrix_event
    media.handle_chatwoot_outgoing = handle_chatwoot_outgoing
    media.enhancements.import_recent_history = import_recent_history
    delivery.handle_chatwoot_outgoing_verified = handle_chatwoot_outgoing
