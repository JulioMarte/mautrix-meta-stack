"""Resolve a real Facebook Marketplace item URL when Matrix lacks XMA external_url.

mautrix-meta intentionally preserves only a subset of Meta XMA fields in Matrix. In
some Marketplace threads that subset contains the listing title/image but no item URL.
As a second, read-only fallback this layer opens the exact Facebook thread using the
same authenticated Facebook session already persisted by mautrix-meta and extracts
only an explicit `/marketplace/item/<numeric-id>` URL from the returned page.

Security properties:
* cookies are read from mautrix-meta's already-mounted read-only DB;
* cookies, HTML and proxy credentials are never logged or persisted by this layer;
* only GET requests to facebook.com are made;
* ambiguous pages with multiple unrelated Marketplace links fail closed;
* the existing authoritative Facebook thread URL remains the final fallback.
"""
from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import unquote, urlparse

import requests

import final_app as runtime
import marketplace_listing_v5 as listing
import marketplace_rebuild_v4 as rebuild
import media_context_v3 as media
import sync_integrity_v6 as integrity

legacy = runtime.legacy
prod = runtime.prod

_POSITIVE_TTL_SECONDS = 6 * 60 * 60
_NEGATIVE_TTL_SECONDS = 10 * 60
_PAGE_CACHE: dict[str, tuple[float, str]] = {}
_ITEM_PATH_RE = re.compile(r"(?:https?://(?:www\.|m\.|web\.)?facebook\.com)?/marketplace/item/(\d{5,})/?", re.IGNORECASE)


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _decode_page_text(value: str) -> str:
    """Normalize the common HTML/JSON escaping Facebook uses in bootstrap payloads."""
    text = html.unescape(str(value or ""))
    for _ in range(3):
        updated = (
            text.replace("\\/", "/")
            .replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\u003A", ":")
            .replace("\\u003a", ":")
        )
        try:
            updated = unquote(updated)
        except Exception:
            pass
        if updated == text:
            break
        text = updated
    return text


def _candidate_score(text: str, start: int, end: int, expected_title: str) -> int:
    expected = _normalize_text(expected_title)
    if not expected:
        return 0
    window = _normalize_text(text[max(0, start - 3000): min(len(text), end + 3000)])
    if expected and expected in window:
        return 100
    words = [word for word in re.findall(r"[\wáéíóúüñ]+", expected, re.IGNORECASE) if len(word) >= 3]
    return sum(8 for word in words if word in window)


def _extract_marketplace_item_url(page_text: str, expected_title: str = "") -> str:
    """Extract one explicit item URL; refuse ambiguous unrelated Marketplace links."""
    text = _decode_page_text(page_text)
    by_id: dict[str, tuple[int, int]] = {}
    for match in _ITEM_PATH_RE.finditer(text):
        item_id = match.group(1)
        score = _candidate_score(text, match.start(), match.end(), expected_title)
        previous = by_id.get(item_id)
        if previous is None or score > previous[0]:
            by_id[item_id] = (score, match.start())
    if not by_id:
        return ""
    if len(by_id) == 1:
        item_id = next(iter(by_id))
        return f"https://www.facebook.com/marketplace/item/{item_id}/"

    ranked = sorted(
        ((score, -position, item_id) for item_id, (score, position) in by_id.items()),
        reverse=True,
    )
    best_score, _neg_pos, best_id = ranked[0]
    second_score = ranked[1][0]
    # Multiple Marketplace URLs commonly appear in Facebook navigation/recommendations.
    # Only choose one when the conversation's listing title uniquely correlates with it.
    if best_score <= 0 or best_score == second_score:
        return ""
    return f"https://www.facebook.com/marketplace/item/{best_id}/"


def _facebook_session_material() -> tuple[dict[str, str], str]:
    """Return authenticated Facebook cookies and UA without exposing them outside memory."""
    try:
        with media._meta_db() as conn:
            row = conn.execute(
                "SELECT metadata FROM user_login WHERE user_mxid = ? LIMIT 1",
                (legacy.MATRIX_ADMIN_MXID,),
            ).fetchone()
    except Exception:
        return {}, ""
    if not row or not row[0]:
        return {}, ""
    try:
        metadata = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (TypeError, ValueError):
        return {}, ""
    if not isinstance(metadata, dict):
        return {}, ""
    cookies = metadata.get("cookies")
    if not isinstance(cookies, dict):
        return {}, ""
    material = {str(k): str(v) for k, v in cookies.items() if k and v}
    # mautrix-meta itself treats these as the required Facebook session cookies.
    if not all(material.get(key) for key in ("c_user", "xs", "datr")):
        return {}, ""
    return material, str(metadata.get("login_ua") or "").strip()


def _facebook_proxies() -> dict[str, str] | None:
    try:
        _source, enabled, proxy = prod.effective_proxy()
    except Exception:
        return None
    if not enabled or not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _cached_page_result(room_id: str) -> str | None:
    cached = _PAGE_CACHE.get(room_id)
    if not cached:
        return None
    created, url = cached
    ttl = _POSITIVE_TTL_SECONDS if url else _NEGATIVE_TTL_SECONDS
    if time.monotonic() - created > ttl:
        _PAGE_CACHE.pop(room_id, None)
        return None
    return url


def _cache_page_result(room_id: str, url: str) -> str:
    _PAGE_CACHE[room_id] = (time.monotonic(), str(url or ""))
    return str(url or "")


def discover_marketplace_item_from_facebook(room_id: str, expected_title: str = "") -> str:
    """Use the authenticated Facebook thread page only when bridge/Matrix data lacked the item URL."""
    cached = _cached_page_result(room_id)
    if cached is not None:
        return cached

    thread_url = integrity._facebook_thread_url(room_id)
    if not thread_url:
        return _cache_page_result(room_id, "")
    cookies, user_agent = _facebook_session_material()
    if not cookies:
        print(
            f"Marketplace item page lookup skipped room={room_id}: authenticated Facebook session unavailable",
            flush=True,
        )
        return _cache_page_result(room_id, "")

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    try:
        response = requests.get(
            thread_url,
            headers=headers,
            cookies=cookies,
            proxies=_facebook_proxies(),
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        final = urlparse(response.url)
        host = (final.hostname or "").casefold()
        if host != "facebook.com" and not host.endswith(".facebook.com"):
            print(
                f"Marketplace item page lookup refused non-Facebook redirect room={room_id}",
                flush=True,
            )
            return _cache_page_result(room_id, "")
        if "/login" in (final.path or "").casefold():
            print(
                f"Marketplace item page lookup needs Facebook reauthentication room={room_id}",
                flush=True,
            )
            return _cache_page_result(room_id, "")
        item_url = _extract_marketplace_item_url(
            f"{response.url}\n{response.text}", expected_title=expected_title
        )
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        print(
            f"Marketplace item page lookup failed room={room_id}: {type(exc).__name__}{suffix}",
            flush=True,
        )
        return _cache_page_result(room_id, "")

    if item_url:
        print(f"Marketplace listing resolved from authenticated Facebook thread room={room_id} url={item_url}", flush=True)
    else:
        print(
            f"Marketplace thread page did not expose an unambiguous item URL room={room_id}",
            flush=True,
        )
    return _cache_page_result(room_id, item_url)


def _best_marketplace_url(room_id: str, expected_title: str) -> tuple[str, str]:
    """Prefer Matrix XMA URL, then authenticated Facebook item URL, then the exact thread."""
    try:
        candidate = listing.discover_marketplace_listing(room_id, expected_title=expected_title)
    except Exception as exc:
        print(f"Marketplace Matrix item discovery failed room={room_id}: {type(exc).__name__}: {exc}", flush=True)
        candidate = {}
    item_url = str(candidate.get("url") or "") if isinstance(candidate, dict) else ""
    if item_url:
        return item_url, "listing"

    item_url = discover_marketplace_item_from_facebook(room_id, expected_title=expected_title)
    if item_url:
        return item_url, "listing"

    thread_url = integrity._facebook_thread_url(room_id)
    return (thread_url, "thread") if thread_url else ("", "")


def install() -> None:
    # sync_integrity_v6 resolves this global each time conversation context is synced.
    integrity._best_marketplace_url = _best_marketplace_url
