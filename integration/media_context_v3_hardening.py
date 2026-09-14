"""Security and compatibility guards for media_context_v3.

Keep the media bridge fail-closed for delivery while making conversation metadata
best-effort. In particular, never forward the Chatwoot API token to an external
object-store URL contained in an attachment callback.
"""
from __future__ import annotations

from urllib.parse import urlparse

import requests

import delivery_history_v2 as delivery
import media_context_v3 as media

legacy = media.legacy

# Capture PR #44's verified text-only path before media_context_v3.install replaces
# the public callback hook. Existing text semantics and tests stay unchanged; only
# messages that actually contain attachments take the media-aware path.
_text_only_outgoing = delivery.handle_chatwoot_outgoing_verified
_original_media_outgoing = media.handle_chatwoot_outgoing
_original_outgoing = _original_media_outgoing


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


def handle_chatwoot_outgoing(payload: dict, *, signature_verified: bool) -> dict:
    attributes = payload.get("content_attributes") or {}
    if isinstance(attributes, dict):
        if _truthy_marker(attributes.get(delivery.HISTORY_MARKER)):
            return {"ok": True, "ignored": True, "reason": "history_import"}
        if _truthy_marker(attributes.get(media.MIRROR_MARKER)):
            return {"ok": True, "ignored": True, "reason": "matrix_mirror"}

    attachments = [item for item in (payload.get("attachments") or []) if isinstance(item, dict)]
    if not attachments:
        return _text_only_outgoing(payload, signature_verified=signature_verified)
    return _original_media_outgoing(payload, signature_verified=signature_verified)


def install() -> None:
    media._bounded_download = bounded_download
    media.sync_conversation_context = safe_sync_conversation_context
    media.post_chatwoot_media = post_chatwoot_media
    media.handle_chatwoot_outgoing = handle_chatwoot_outgoing
    delivery.handle_chatwoot_outgoing_verified = handle_chatwoot_outgoing
