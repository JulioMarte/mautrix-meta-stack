"""Expose the canonical Marketplace listing and a visual item preview in Chatwoot.

The listing title remains a conversation attribute. This layer adds one operator-facing
link attribute and, when mautrix-meta preserved the Marketplace XMA image in Matrix,
a single private image note as a thumbnail. Discovery is direction-agnostic, so it works
whether the logged-in Facebook account is the buyer or the seller.
"""
from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

import final_app as runtime
import marketplace_rebuild_v4 as base
import media_context_v3 as media
import runtime_enhancements as enhancements
import delivery_history_v2 as delivery

legacy = runtime.legacy
prod = runtime.prod

LISTING_URL_KEY = "marketplace_listing_url"
LISTING_URL_DISPLAY = "Open Marketplace listing"
LISTING_CARD_MARKER = "matrix_bridge_marketplace_listing_card"
OWNED_DESCRIPTION = base._OWNED_DESCRIPTION
CACHE_TTL_SECONDS = 6 * 60 * 60
NEGATIVE_CACHE_TTL_SECONDS = 60
_LISTING_CACHE: dict[str, tuple[float, dict]] = {}
_SCHEMA_CACHE: set[tuple[str, str]] = set()
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_FB_SCHEME_RE = re.compile(r"(?:fb|facebook)://marketplace/item/(\d+)", re.IGNORECASE)


def _normalize_title(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _walk(value, key: str = ""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _walk(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, key)
    elif isinstance(value, str):
        yield key, value


def _unwrap_facebook_redirect(raw: str) -> str:
    """Unwrap Facebook /l.php links without following the redirect over the network."""
    value = html.unescape(str(raw or "").strip())
    for _ in range(3):
        try:
            parsed = urlparse(value)
        except ValueError:
            return value
        host = (parsed.hostname or "").casefold()
        if host not in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "l.facebook.com"}:
            return value
        if parsed.path.rstrip("/").casefold() != "/l.php":
            return value
        target = (parse_qs(parsed.query).get("u") or [""])[0]
        if not target:
            return value
        decoded = unquote(target).strip()
        if not decoded or decoded == value:
            return value
        value = decoded
    return value


def _canonical_marketplace_url(value: str) -> str:
    raw = _unwrap_facebook_redirect(value)
    scheme_match = _FB_SCHEME_RE.fullmatch(raw.rstrip("/"))
    if scheme_match:
        return f"https://www.facebook.com/marketplace/item/{scheme_match.group(1)}/"
    if not raw.startswith(("https://", "http://")):
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    if host not in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0].casefold() != "marketplace" or parts[1].casefold() != "item":
        return ""
    item_id = parts[2]
    if not item_id.isdigit():
        return ""
    return f"https://www.facebook.com/marketplace/item/{item_id}/"


def _marketplace_urls_from_text(value: str) -> list[str]:
    """Find listing URLs even when mautrix-meta put them in body/formatted_body captions."""
    text = html.unescape(str(value or ""))
    raw_candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith(("http://", "https://", "fb://", "facebook://")):
        raw_candidates.append(stripped)
    raw_candidates.extend(_HTTP_URL_RE.findall(text))
    raw_candidates.extend(match.group(0) for match in _FB_SCHEME_RE.finditer(text))

    found: list[str] = []
    for raw in raw_candidates:
        # URLs captured from prose/HTML frequently retain punctuation from the caption.
        cleaned = raw.rstrip(".,;:!?)]}")
        canonical = _canonical_marketplace_url(cleaned)
        if canonical and canonical not in found:
            found.append(canonical)
    return found


def _event_listing_candidate(event: dict, expected_title: str = "") -> dict:
    urls: list[str] = []
    titles: list[str] = []
    mxcs: list[str] = []
    title_keys = {
        "com.beeper.meta.full_post_title",
        "full_post_title",
        "og:title",
        "title",
        "titletext",
    }
    image_keys = {"url", "og:image", "image", "image_url", "preview_url", "previewurl"}

    for key, value in _walk(event):
        for listing_url in _marketplace_urls_from_text(value):
            if listing_url not in urls:
                urls.append(listing_url)
        lowered = key.casefold()
        if lowered in title_keys and value.strip():
            titles.append(value.strip())
        if value.startswith("mxc://") and lowered in image_keys:
            mxcs.append(value)

    if not urls:
        return {}

    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    if str(content.get("msgtype") or "") == "m.image":
        direct_mxc = str(content.get("url") or "")
        if direct_mxc.startswith("mxc://"):
            mxcs.insert(0, direct_mxc)

    expected_norm = _normalize_title(expected_title)
    best_title = ""
    score = 100
    for title in titles:
        normalized = _normalize_title(title)
        if expected_norm and normalized == expected_norm:
            best_title = title
            score += 80
            break
        if expected_norm and (expected_norm in normalized or normalized in expected_norm):
            best_title = title
            score += 45
        elif not best_title:
            best_title = title
    if mxcs:
        score += 15

    try:
        timestamp = int(event.get("origin_server_ts") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return {
        "url": urls[0],
        "title": best_title,
        "thumbnail_mxc": mxcs[0] if mxcs else "",
        "event_id": str(event.get("event_id") or ""),
        "timestamp": timestamp,
        "score": score,
    }


def _cache_candidate(room_id: str, candidate: dict) -> None:
    _LISTING_CACHE[room_id] = (time.monotonic(), dict(candidate))


def _cached_candidate(room_id: str) -> dict | None:
    cached = _LISTING_CACHE.get(room_id)
    if not cached:
        return None
    created, candidate = cached
    ttl = CACHE_TTL_SECONDS if candidate else NEGATIVE_CACHE_TTL_SECONDS
    if time.monotonic() - created > ttl:
        _LISTING_CACHE.pop(room_id, None)
        return None
    return dict(candidate)


def discover_marketplace_listing(room_id: str, *, expected_title: str = "") -> dict:
    cached = _cached_candidate(room_id)
    if cached is not None:
        return cached

    days = delivery.history_days()
    if days <= 0:
        _cache_candidate(room_id, {})
        return {}
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages"
    token = ""
    candidates: list[dict] = []
    scanned_events = 0
    pages = 0

    while True:
        params = {"dir": "b", "limit": delivery.MATRIX_PAGE_SIZE}
        if token:
            params["from"] = token
        response = enhancements._matrix_get(path, params=params)
        payload = response.json() or {}
        chunk = [item for item in (payload.get("chunk") or []) if isinstance(item, dict)]
        if not chunk:
            break
        pages += 1
        crossed = False
        for event in chunk:
            scanned_events += 1
            try:
                event_ts = int(event.get("origin_server_ts") or 0)
            except (TypeError, ValueError):
                event_ts = 0
            if event_ts and event_ts < cutoff_ms:
                crossed = True
                continue
            candidate = _event_listing_candidate(event, expected_title)
            if candidate:
                candidates.append(candidate)
        next_token = str(payload.get("end") or payload.get("end_token") or "")
        if crossed or not next_token or next_token == token:
            break
        token = next_token

    if candidates:
        # Prefer title-correlated XMA data; for ties choose the oldest event because the
        # Marketplace origin card normally predates links shared later in the chat.
        best = max(
            candidates,
            key=lambda item: (int(item.get("score") or 0), -int(item.get("timestamp") or 0)),
        )
        print(
            f"Marketplace listing resolved room={room_id} event={best.get('event_id') or '-'} "
            f"url={best.get('url')} pages={pages} events={scanned_events}",
            flush=True,
        )
    else:
        best = {}
        print(
            f"Marketplace listing URL not present in Matrix room={room_id} "
            f"pages={pages} events={scanned_events}; will retry after negative-cache TTL",
            flush=True,
        )
    _cache_candidate(room_id, best)
    return best


def ensure_listing_schema() -> None:
    base.ensure_chatwoot_marketplace_schema()
    if not legacy.configured():
        return
    account_id = str(legacy.get_setting("chatwoot_account_id"))
    cache_key = (legacy.get_setting("chatwoot_base_url").rstrip("/"), account_id)
    if cache_key in _SCHEMA_CACHE:
        return
    try:
        definitions = base._normalize_collection(
            prod.cw_get(f"/api/v1/accounts/{account_id}/custom_attribute_definitions")
        )
        existing = {str(item.get("attribute_key") or "") for item in definitions}
        if LISTING_URL_KEY not in existing:
            media._cw_json(
                "POST",
                f"/api/v1/accounts/{account_id}/custom_attribute_definitions",
                {
                    "attribute_display_name": LISTING_URL_DISPLAY,
                    "attribute_display_type": 4,
                    "attribute_description": OWNED_DESCRIPTION,
                    "attribute_key": LISTING_URL_KEY,
                    "attribute_model": 0,
                },
            )
        _SCHEMA_CACHE.add(cache_key)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (409, 422):
            _SCHEMA_CACHE.add(cache_key)
            return
        print(f"Chatwoot Marketplace listing schema sync failed: {type(exc).__name__}: {exc}", flush=True)
    except Exception as exc:
        print(f"Chatwoot Marketplace listing schema sync failed: {type(exc).__name__}: {exc}", flush=True)


def _listing_note_exists(account_id: int, conversation_id: int, listing_url: str) -> bool:
    try:
        data = prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages")
    except Exception:
        return False
    for message in delivery._payload_messages(data):
        attrs = message.get("content_attributes")
        if not isinstance(attrs, dict):
            continue
        if attrs.get(LISTING_CARD_MARKER) is True and attrs.get("marketplace_listing_url") == listing_url:
            return True
    return False


def _post_listing_thumbnail(account_id: int, conversation_id: int, candidate: dict) -> None:
    mxc = str(candidate.get("thumbnail_mxc") or "")
    listing_url = str(candidate.get("url") or "")
    if not mxc or not listing_url or _listing_note_exists(account_id, conversation_id, listing_url):
        return
    try:
        data, filename, mimetype, _caption = media.download_matrix_media(
            {"url": mxc, "body": "marketplace-item", "info": {}}
        )
        response = requests.post(
            legacy.chatwoot_url(
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
            ),
            headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
            data={
                "content": "",
                "message_type": "outgoing",
                "private": "true",
                "content_type": "text",
                "content_attributes": json.dumps({
                    LISTING_CARD_MARKER: True,
                    "marketplace_listing_url": listing_url,
                    "matrix_event_id": str(candidate.get("event_id") or ""),
                }),
            },
            files={"attachments[]": (filename or "marketplace-item", data, mimetype)},
            timeout=45,
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            f"Marketplace listing thumbnail sync failed conversation={conversation_id}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def sync_conversation_context(room_id: str, link) -> None:
    base.sync_conversation_context(room_id, link)
    context = media.portal_context(room_id)
    if not context or not context.get("is_marketplace") or not link:
        return

    ensure_listing_schema()
    expected_title = base._marketplace_listing_title(context)
    try:
        candidate = discover_marketplace_listing(room_id, expected_title=expected_title)
    except Exception as exc:
        print(
            f"Marketplace listing discovery failed room={room_id}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return
    if not candidate:
        return

    try:
        account_id = int(legacy.get_setting("chatwoot_account_id"))
        conversation_id = int(link["conversation_id"])
        conversation = prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}")
        current = conversation.get("custom_attributes") if isinstance(conversation, dict) else {}
        merged = dict(current) if isinstance(current, dict) else {}
        merged[LISTING_URL_KEY] = candidate["url"]
        if candidate.get("title"):
            merged["marketplace_listing_title"] = candidate["title"]
        if merged != current:
            media._cw_json(
                "POST",
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/custom_attributes",
                {"custom_attributes": merged},
            )
        _post_listing_thumbnail(account_id, conversation_id, candidate)
    except Exception as exc:
        print(
            f"Marketplace listing context sync failed room={room_id}: {type(exc).__name__}: {exc}",
            flush=True,
        )


def mirror_matrix_event(room_id: str, event: dict, *, history: bool = False) -> bool:
    context = media.portal_context(room_id)
    if context.get("is_marketplace"):
        candidate = _event_listing_candidate(event, base._marketplace_listing_title(context))
        if candidate:
            _cache_candidate(room_id, candidate)
    return base.mirror_matrix_event(room_id, event, history=history)


def import_recent_history(room_id: str) -> int:
    imported = base.import_recent_history(room_id)
    link = base._existing_link(room_id)
    if link:
        sync_conversation_context(room_id, link)
    return imported


def install() -> None:
    media.sync_conversation_context = sync_conversation_context
    media.mirror_matrix_event = mirror_matrix_event
    enhancements.import_recent_history = import_recent_history
    ensure_listing_schema()
