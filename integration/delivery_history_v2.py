"""Outbound delivery verification and day-based bidirectional history import.

This module is installed after the existing runtime layers. It keeps the existing
API-inbox callback URL, but makes delivery fail closed and observable: a callback
whose Chatwoot HMAC cannot be verified (some Chatwoot API-channel versions expose
a different secret than the key used to sign callbacks) must be re-authenticated
against Chatwoot's authenticated messages API before it can reach Matrix.

History is configured in days rather than a fixed Matrix event count. Pagination
continues backwards until the configured UTC cutoff is crossed. Customer messages
are imported as incoming and messages authored by the dedicated Matrix integration
user are imported as outgoing. Historical outgoing Chatwoot messages carry a marker
that makes the callback path ignore them, so importing history can never replay old
agent messages to Meta.
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import quote

import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from nicegui import app as nicegui_app

import autojoin_verify
import runtime_enhancements as enhancements

legacy = enhancements.legacy
prod = enhancements.prod

DEFAULT_HISTORY_DAYS = 30
MAX_HISTORY_DAYS = 3650
MATRIX_PAGE_SIZE = 100
HISTORY_MARKER = "matrix_history_import"
_POLICY_RECONCILE_THREAD = "sync-policy-reconcile"

_original_operations_state = enhancements.operations_state
_original_save_operations_settings = enhancements.save_operations_settings


def _now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def _message_type_is_outgoing(value) -> bool:
    return value in ("outgoing", 1, "1")


def _payload_messages(data) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    payload = data.get("payload")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("messages", "payload", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    for key in ("messages", "data"):
        nested = data.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return []


def robust_contact_object(payload):
    """Accept the contact response shapes observed across Chatwoot versions."""
    if isinstance(payload, dict) and payload.get("id"):
        return payload
    if not isinstance(payload, dict):
        return None
    nested = payload.get("payload")
    if isinstance(nested, list):
        return next((item for item in nested if isinstance(item, dict) and item.get("id")), None)
    if isinstance(nested, dict):
        if nested.get("id"):
            return nested
        for key in ("contact", "data", "payload"):
            item = nested.get(key)
            if isinstance(item, dict) and item.get("id"):
                return item
            if isinstance(item, list):
                found = next((row for row in item if isinstance(row, dict) and row.get("id")), None)
                if found:
                    return found
    return None


def _fresh_callback_timestamp(timestamp: str, now: int | None = None) -> None:
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid Chatwoot webhook timestamp") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp_int) > enhancements.WEBHOOK_MAX_AGE_SECONDS:
        raise RuntimeError("Chatwoot webhook timestamp is too old or too far in the future")


def _conversation_id(payload: dict) -> int:
    conversation = payload.get("conversation") or {}
    value = conversation.get("id") or payload.get("conversation_id")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Chatwoot callback has no valid conversation id") from exc


def _configured_inbox_matches(payload: dict) -> bool:
    configured = str(legacy.get_setting("chatwoot_inbox_id"))
    conversation = payload.get("conversation") or {}
    inbox = payload.get("inbox") or {}
    candidate = inbox.get("id") or conversation.get("inbox_id") or payload.get("inbox_id")
    return not candidate or str(candidate) == configured


def verify_outgoing_against_chatwoot(payload: dict) -> dict:
    """Authenticate an exact outgoing callback through Chatwoot's own REST API."""
    conversation_id = _conversation_id(payload)
    message_id = str(payload.get("id") or "")
    content = str(payload.get("content") or "")
    if not message_id:
        raise RuntimeError("Chatwoot callback has no message id")
    account_id = int(legacy.get_setting("chatwoot_account_id"))

    conversation = prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}")
    actual_inbox = None
    try:
        configured_inbox = int(legacy.get_setting("chatwoot_inbox_id"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Configured Chatwoot inbox is invalid") from exc
    for candidate in (
        conversation.get("inbox_id") if isinstance(conversation, dict) else None,
        ((conversation.get("inbox") or {}).get("id") if isinstance(conversation, dict) and isinstance(conversation.get("inbox"), dict) else None),
        ((conversation.get("contact_inbox") or {}).get("inbox_id") if isinstance(conversation, dict) and isinstance(conversation.get("contact_inbox"), dict) else None),
    ):
        try:
            actual_inbox = int(candidate)
            break
        except (TypeError, ValueError):
            continue
    if actual_inbox != configured_inbox:
        raise RuntimeError("Chatwoot callback conversation is outside the configured inbox")

    data = prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages")
    for message in _payload_messages(data):
        if str(message.get("id") or "") != message_id:
            continue
        if str(message.get("content") or "") != content:
            raise RuntimeError("Chatwoot callback content does not match the authenticated message")
        if not _message_type_is_outgoing(message.get("message_type")):
            raise RuntimeError("Authenticated Chatwoot message is not outgoing")
        if message.get("private") is True:
            raise RuntimeError("Authenticated Chatwoot message is private")
        return message
    raise RuntimeError("Chatwoot callback message was not found through the authenticated API")


def verify_matrix_event(room_id: str, event_id: str, expected_body: str) -> None:
    encoded_room = quote(room_id, safe="")
    encoded_event = quote(event_id, safe="")
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{encoded_room}/event/{encoded_event}",
        headers=legacy.matrix_headers(),
        timeout=20,
    )
    response.raise_for_status()
    event = response.json() if response.content else {}
    if event.get("type") != "m.room.message":
        raise RuntimeError("Matrix acknowledged an outbound event but it is not a room message")
    if str((event.get("content") or {}).get("body") or "") != expected_body:
        raise RuntimeError("Matrix acknowledged an outbound event with unexpected content")


def handle_chatwoot_outgoing_verified(payload: dict, *, signature_verified: bool) -> dict:
    if payload.get("event") != "message_created":
        return {"ok": True, "ignored": True}
    if not _message_type_is_outgoing(payload.get("message_type")) or payload.get("private") is True:
        return {"ok": True, "ignored": True}
    if not _configured_inbox_matches(payload):
        return {"ok": True, "ignored": True, "reason": "outside_configured_chatwoot_inbox"}

    attributes = payload.get("content_attributes") or {}
    if isinstance(attributes, dict) and attributes.get(HISTORY_MARKER) is True:
        return {"ok": True, "ignored": True, "reason": "history_import"}

    conversation_id = _conversation_id(payload)
    content = str(payload.get("content") or "").strip()
    message_id = str(payload.get("id") or "")
    if not content or not message_id:
        return {"ok": True, "ignored": True, "reason": "missing_content_or_message_id"}

    event_key = "chatwoot:" + message_id
    if legacy.event_seen(event_key):
        return {"ok": True, "duplicate": True}

    if not signature_verified:
        verify_outgoing_against_chatwoot(payload)

    with legacy.db() as conn:
        link = conn.execute(
            "SELECT * FROM room_links WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
    if not link:
        raise RuntimeError(f"Chatwoot conversation {conversation_id} has no Matrix room mapping")

    matrix_result = legacy.send_matrix_message(link["room_id"], content, "cw-" + message_id)
    event_id = str((matrix_result or {}).get("event_id") or "") if isinstance(matrix_result, dict) else ""
    if not event_id:
        reason = (matrix_result or {}).get("reason") if isinstance(matrix_result, dict) else ""
        raise RuntimeError(f"Matrix did not acknowledge Chatwoot message {message_id}: {reason or 'no event_id'}")
    verify_matrix_event(link["room_id"], event_id, content)

    legacy.mark_event(event_key, "chatwoot_to_matrix")
    now = _now_utc()
    legacy.set_setting("api_inbox_delivery_verified_at", now)
    legacy.set_setting("last_chatwoot_matrix_delivery_at", now)
    legacy.set_setting("last_chatwoot_matrix_event_id", event_id)
    legacy.set_setting("last_chatwoot_matrix_conversation_id", str(conversation_id))
    legacy.set_setting("last_chatwoot_matrix_error", "")
    print(
        f"Chatwoot outgoing verified in Matrix conversation={conversation_id} "
        f"room={link['room_id']} chatwoot_message={message_id} matrix_event={event_id}",
        flush=True,
    )
    return {"ok": True, "matrix_event_id": event_id}


# Runtime layers may replace callback delivery semantics without mutating the
# public legacy handler that its focused unit tests exercise directly.
callback_outgoing_handler = handle_chatwoot_outgoing_verified


async def outbound_callback_middleware(request: Request, call_next):
    if request.url.path != "/webhooks/chatwoot/inbox" or request.method.upper() != "POST":
        return await call_next(request)

    raw = await request.body()
    timestamp = request.headers.get("X-Chatwoot-Timestamp", "")
    signature = request.headers.get("X-Chatwoot-Signature", "")
    try:
        _fresh_callback_timestamp(timestamp)
        payload = json.loads(raw.decode("utf-8"))
        signature_verified = False
        try:
            signature_verified = enhancements.verify_inbox_signature(raw, signature, timestamp)
        except RuntimeError as exc:
            # Chatwoot issue #13809 documents API-channel installations where the
            # secret exposed through REST differs from the internal signing key.
            # The callback is not trusted yet: the exact message is authenticated
            # against Chatwoot's REST API below before any Matrix send is allowed.
            print(f"Chatwoot API inbox HMAC mismatch; using authenticated API fallback: {exc}", flush=True)
        result = callback_outgoing_handler(payload, signature_verified=bool(signature_verified))
        return JSONResponse(result)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        legacy.set_setting("last_chatwoot_matrix_error", error[:1000])
        legacy.set_setting("last_chatwoot_matrix_error_at", _now_utc())
        print(f"Chatwoot outgoing delivery failed: {error}", flush=True)
        return JSONResponse({"error": "delivery failed"}, status_code=502)


def history_days() -> int:
    return enhancements.setting_int("history_import_days", DEFAULT_HISTORY_DAYS, 0, MAX_HISTORY_DAYS)


def operations_state_days() -> dict:
    state = _original_operations_state()
    days = history_days()
    state["history_limit"] = days
    state["history_days"] = days
    return state


def _request_policy_reconcile(reason: str) -> None:
    revision = enhancements.setting_int("sync_policy_revision", 0, 0, 2_000_000_000) + 1
    legacy.set_setting("sync_policy_revision", str(revision))
    legacy.set_setting("sync_reconcile_requested_at", _now_utc())
    legacy.set_setting("sync_reconcile_requested_reason", reason)
    print(
        f"event=sync_reconcile_requested revision={revision} reason={reason}",
        flush=True,
    )

    # Production performs the reconciliation asynchronously so an operator save
    # never blocks on every historical room. Unit tests and offline tooling keep
    # START_MATRIX_SYNC=false and therefore only persist the request.
    if os.getenv("START_MATRIX_SYNC", "true").lower() != "true":
        return
    if any(t.name == _POLICY_RECONCILE_THREAD and t.is_alive() for t in threading.enumerate()):
        return

    def run():
        try:
            import meta_portal_reconcile
            result = meta_portal_reconcile.reconcile_meta_portals()
            legacy.set_setting("sync_reconcile_completed_at", _now_utc())
            legacy.set_setting("sync_reconcile_last_error", "")
            print(
                f"event=sync_reconcile_completed revision={revision} "
                f"errors={len((result or {}).get('errors') or []) if isinstance(result, dict) else 0}",
                flush=True,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            legacy.set_setting("sync_reconcile_last_error", error[:1000])
            print(
                f"event=sync_reconcile_failed revision={revision} error={error}",
                flush=True,
            )

    threading.Thread(target=run, name=_POLICY_RECONCILE_THREAD, daemon=True).start()


def save_operations_settings_days(*, auto_join: bool, import_history: bool, history_limit: int,
                                  sync_profiles: bool, repair_deleted: bool) -> None:
    previous = (
        history_days(),
        enhancements.setting_bool("import_history_on_join", True),
        enhancements.setting_bool("sync_contact_profiles", True),
    )
    days = max(0, min(MAX_HISTORY_DAYS, int(history_limit)))
    _original_save_operations_settings(
        auto_join=True,
        import_history=import_history,
        history_limit=enhancements.DEFAULT_HISTORY_LIMIT,
        sync_profiles=sync_profiles,
        repair_deleted=repair_deleted,
    )
    legacy.set_setting("history_import_days", str(days))
    current = (days, bool(import_history), bool(sync_profiles))
    if current != previous:
        reasons = []
        if days != previous[0]:
            reasons.append(f"history_days:{previous[0]}->{days}")
        if bool(import_history) != previous[1]:
            reasons.append(f"history_import:{int(previous[1])}->{int(bool(import_history))}")
        if bool(sync_profiles) != previous[2]:
            reasons.append(f"profile_sync:{int(previous[2])}->{int(bool(sync_profiles))}")
        _request_policy_reconcile(",".join(reasons) or "operations_policy_changed")


def _trusted_customer_sender(events: list[dict]) -> str:
    bot = legacy.bridge_bot_mxid()
    for event in events:
        sender = str(event.get("sender") or "")
        if not sender or sender in {legacy.MATRIX_ADMIN_MXID, bot}:
            continue
        trusted, _ = autojoin_verify.trusted_meta_inviter(sender)
        if trusted:
            return sender
    return ""


def _import_outgoing_history(room_id: str, event: dict, customer_sender: str) -> bool:
    event_id = str(event.get("event_id") or "")
    if not event_id or legacy.event_seen(event_id):
        return False
    content = event.get("content") or {}
    if content.get("msgtype") not in ("m.text", "m.notice"):
        return False
    body = str(content.get("body") or "").strip()
    if not body or not customer_sender:
        return False
    link = prod.ensure_room_link(room_id, customer_sender)
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    legacy.cw_post(
        f"/api/v1/accounts/{account_id}/conversations/{link['conversation_id']}/messages",
        {
            "content": body,
            "message_type": "outgoing",
            "private": False,
            "content_type": "text",
            "content_attributes": {HISTORY_MARKER: True, "matrix_event_id": event_id},
        },
    )
    legacy.mark_event(event_id, "matrix_history_outgoing_to_chatwoot")
    return True


def import_recent_history_days(room_id: str) -> int:
    if not enhancements.setting_bool("import_history_on_join", True):
        return 0
    days = history_days()
    if days <= 0:
        return 0

    cutoff_ms = int((time.time() - days * 86400) * 1000)
    encoded_room = quote(room_id, safe="")
    path = f"/_matrix/client/v3/rooms/{encoded_room}/messages"
    token = ""
    collected: list[dict] = []

    while True:
        params = {"dir": "b", "limit": MATRIX_PAGE_SIZE}
        if token:
            params["from"] = token
        response = enhancements._matrix_get(path, params=params)
        data = response.json() or {}
        chunk = [event for event in (data.get("chunk") or []) if isinstance(event, dict)]
        if not chunk:
            break

        crossed_cutoff = False
        for event in chunk:
            try:
                event_ts = int(event.get("origin_server_ts") or 0)
            except (TypeError, ValueError):
                event_ts = 0
            if event_ts and event_ts < cutoff_ms:
                crossed_cutoff = True
                continue
            if event.get("type") == "m.room.message":
                collected.append(event)

        next_token = str(data.get("end") or data.get("end_token") or "")
        if crossed_cutoff or not next_token or next_token == token:
            break
        token = next_token

    ordered = list(reversed(collected))
    customer_sender = _trusted_customer_sender(ordered)
    imported = 0
    for event in ordered:
        event_id = str(event.get("event_id") or "")
        if event_id and legacy.event_seen(event_id):
            continue
        sender = str(event.get("sender") or "")
        try:
            if sender == legacy.MATRIX_ADMIN_MXID:
                if _import_outgoing_history(room_id, event, customer_sender):
                    imported += 1
                continue
            before = legacy.event_seen(event_id)
            prod.matrix_event_to_chatwoot(room_id, event)
            if not before and legacy.event_seen(event_id):
                imported += 1
        except Exception as exc:
            print(f"history import failed room={room_id} event={event_id}: {type(exc).__name__}: {exc}", flush=True)
            continue
    return imported


def install() -> None:
    prod.contact_object = robust_contact_object
    enhancements.operations_state = operations_state_days
    enhancements.save_operations_settings = save_operations_settings_days
    enhancements.import_recent_history = import_recent_history_days
    legacy.set_setting("history_import_days", str(history_days()))
    if not getattr(nicegui_app, "_meta_outbound_callback_middleware_v2", False):
        nicegui_app.middleware("http")(outbound_callback_middleware)
        setattr(nicegui_app, "_meta_outbound_callback_middleware_v2", True)
