"""Runtime enhancements for production Meta -> Matrix -> Chatwoot operation.

This module deliberately lives beside the existing integration runtime so we can
harden behavior without duplicating the whole NiceGUI application. It is loaded
before NiceGUI starts serving requests.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
from urllib.parse import quote

import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from nicegui import ui

import nicegui_app as admin_ui

runtime = admin_ui.runtime
legacy = admin_ui.legacy
prod = admin_ui.prod

DEFAULT_HISTORY_LIMIT = 100
PROFILE_CACHE_SECONDS = 600
_profile_cache: dict[str, tuple[float, dict]] = {}


def setting_bool(key: str, default: bool) -> bool:
    raw = legacy.get_setting(key, "")
    if not raw:
        return bool(default)
    return str(raw).lower() in {"1", "true", "yes", "on"}


def setting_int(key: str, default: int, minimum: int = 0, maximum: int = 1000) -> int:
    try:
        value = int(legacy.get_setting(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def save_operations_settings(*, auto_join: bool, import_history: bool,
                             history_limit: int, sync_profiles: bool,
                             repair_deleted: bool) -> None:
    history_limit = max(0, min(1000, int(history_limit)))
    legacy.set_setting("auto_join_meta_portals", "1" if auto_join else "0")
    legacy.set_setting("import_history_on_join", "1" if import_history else "0")
    legacy.set_setting("history_import_limit", str(history_limit))
    legacy.set_setting("sync_contact_profiles", "1" if sync_profiles else "0")
    legacy.set_setting("repair_deleted_conversations", "1" if repair_deleted else "0")


def operations_state() -> dict:
    return {
        "auto_join": setting_bool("auto_join_meta_portals", True),
        "import_history": setting_bool("import_history_on_join", True),
        "history_limit": setting_int("history_import_limit", DEFAULT_HISTORY_LIMIT),
        "sync_profiles": setting_bool("sync_contact_profiles", True),
        "repair_deleted": setting_bool("repair_deleted_conversations", True),
        "api_inbox_callback_verified_at": legacy.get_setting("api_inbox_callback_verified_at"),
        "api_inbox_secret_saved": bool(legacy.get_setting("chatwoot_api_inbox_signing_secret")),
    }


def _matrix_get(path: str, *, params=None, timeout=20):
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}{path}",
        headers=legacy.matrix_headers(), params=params, timeout=timeout,
    )
    response.raise_for_status()
    return response


def _matrix_post(path: str, payload=None, timeout=20):
    response = requests.post(
        f"{legacy.MATRIX_HOMESERVER}{path}",
        headers=legacy.matrix_headers(), json=payload or {}, timeout=timeout,
    )
    response.raise_for_status()
    return response


def matrix_profile(mxid: str) -> dict:
    now = time.time()
    cached = _profile_cache.get(mxid)
    if cached and now - cached[0] < PROFILE_CACHE_SECONDS:
        return dict(cached[1])
    response = _matrix_get(f"/_matrix/client/v3/profile/{quote(mxid, safe='')}")
    data = response.json() if response.content else {}
    result = {
        "displayname": str(data.get("displayname") or "").strip(),
        "avatar_url": str(data.get("avatar_url") or "").strip(),
    }
    _profile_cache[mxid] = (now, result)
    return dict(result)


def matrix_avatar_bytes(mxc: str):
    if not mxc.startswith("mxc://"):
        return None
    remainder = mxc[6:]
    if "/" not in remainder:
        return None
    server, media_id = remainder.split("/", 1)
    # Authenticated media endpoint first (MSC3916 / modern Synapse), then the
    # legacy endpoint for installations that still expose it.
    paths = [
        f"/_matrix/client/v1/media/download/{quote(server, safe='')}/{quote(media_id, safe='')}",
        f"/_matrix/media/v3/download/{quote(server, safe='')}/{quote(media_id, safe='')}",
    ]
    for path in paths:
        response = requests.get(
            f"{legacy.MATRIX_HOMESERVER}{path}",
            headers=legacy.matrix_headers(), timeout=20,
        )
        if response.status_code == 200 and response.content:
            content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
            extension = mimetypes.guess_extension(content_type) or ".jpg"
            return response.content, content_type, "avatar" + extension
    return None


def fallback_display_name(sender: str) -> str:
    localpart = sender.split(":", 1)[0].lstrip("@").replace("_", " ").strip()
    return localpart or "Meta contact"


def contact_identity(sender: str) -> dict:
    if not setting_bool("sync_contact_profiles", True):
        return {"name": fallback_display_name(sender), "avatar_url": ""}
    try:
        profile = matrix_profile(sender)
    except Exception as exc:
        print(f"matrix profile lookup failed sender={sender}: {exc}", flush=True)
        profile = {}
    return {
        "name": profile.get("displayname") or fallback_display_name(sender),
        "avatar_url": profile.get("avatar_url") or "",
    }


def _chatwoot_request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers = {"api_access_token": legacy.get_setting("chatwoot_api_token"), **headers}
    response = requests.request(
        method,
        legacy.chatwoot_url(path),
        headers=headers,
        timeout=20,
        **kwargs,
    )
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def update_chatwoot_contact_profile(account_id: int, contact_id: int, sender: str) -> None:
    if not setting_bool("sync_contact_profiles", True):
        return
    identity = contact_identity(sender)
    data = {"name": identity["name"]}
    avatar = None
    if identity.get("avatar_url"):
        try:
            avatar = matrix_avatar_bytes(identity["avatar_url"])
        except Exception as exc:
            print(f"matrix avatar fetch failed sender={sender}: {exc}", flush=True)
    try:
        if avatar:
            body, content_type, filename = avatar
            _chatwoot_request(
                "PUT",
                f"/api/v1/accounts/{account_id}/contacts/{contact_id}",
                data=data,
                files={"avatar": (filename, body, content_type)},
            )
        else:
            _chatwoot_request(
                "PUT",
                f"/api/v1/accounts/{account_id}/contacts/{contact_id}",
                json=data,
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        # Profile enrichment must never block message delivery.
        print(f"chatwoot contact profile sync failed contact={contact_id}: {exc}", flush=True)


def _existing_room_link(room_id: str):
    with legacy.db() as conn:
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


def _delete_room_link(room_id: str) -> None:
    with legacy.db() as conn:
        conn.execute("DELETE FROM room_links WHERE room_id = ?", (room_id,))


def repair_deleted_conversation(room_id: str) -> bool:
    """Drop stale local linkage when an operator deleted the Chatwoot conversation."""
    if not setting_bool("repair_deleted_conversations", True):
        return False
    row = _existing_room_link(room_id)
    if not row or not legacy.configured():
        return False
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    try:
        prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{int(row['conversation_id'])}")
        return False
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
    _delete_room_link(room_id)
    print(
        f"removed stale Chatwoot mapping room={room_id} conversation={row['conversation_id']}",
        flush=True,
    )
    return True


_base_ensure_room_link = prod.ensure_room_link


def enhanced_ensure_room_link(room_id: str, sender: str):
    repair_deleted_conversation(room_id)
    existing = _existing_room_link(room_id)
    if existing:
        update_chatwoot_contact_profile(
            int(legacy.get_setting("chatwoot_account_id")), int(existing["contact_id"]), sender
        )
        return existing

    # Create with the real Matrix/Meta display name instead of the technical ghost MXID.
    identity = contact_identity(sender)
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    identifier = "matrix-room:" + hashlib.sha256(room_id.encode()).hexdigest()[:32]
    generated_source_id = "mx-" + hashlib.sha256((room_id + ":source").encode()).hexdigest()[:30]

    try:
        raw = legacy.cw_post(
            f"/api/v1/accounts/{account_id}/contacts",
            {
                "inbox_id": inbox_id,
                "name": identity["name"],
                "identifier": identifier,
                "additional_attributes": {"matrix_room_id": room_id, "matrix_sender": sender},
            },
        )
        contact = prod.contact_object(raw)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (409, 422):
            raise
        contact = prod.find_contact_by_identifier(account_id, identifier)
    if not contact or not contact.get("id"):
        raise RuntimeError("Chatwoot contact could not be created or resolved")
    contact_id = int(contact["id"])
    source_id = prod.contact_source_id(contact, inbox_id)
    if not source_id:
        assoc = legacy.cw_post(
            f"/api/v1/accounts/{account_id}/contacts/{contact_id}/contact_inboxes",
            {"inbox_id": inbox_id, "source_id": generated_source_id},
        )
        source_id = str((assoc or {}).get("source_id") or generated_source_id)

    update_chatwoot_contact_profile(account_id, contact_id, sender)
    conversation = legacy.cw_post(
        f"/api/v1/accounts/{account_id}/conversations",
        {
            "source_id": source_id,
            "inbox_id": inbox_id,
            "contact_id": contact_id,
            "status": "open",
            "custom_attributes": {"matrix_room_id": room_id, "matrix_sender": sender},
        },
    )
    conversation_id = int(conversation["id"])
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (room_id, contact_id, source_id, conversation_id, int(time.time())),
        )
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


def _invite_sender(room: dict) -> str:
    for event in ((room.get("invite_state") or {}).get("events") or []):
        if event.get("type") != "m.room.member":
            continue
        content = event.get("content") or {}
        if content.get("membership") == "invite" and event.get("state_key") == legacy.MATRIX_ADMIN_MXID:
            return str(event.get("sender") or "")
    return ""


def auto_join_room(room_id: str, room: dict) -> bool:
    if not setting_bool("auto_join_meta_portals", True):
        return False
    inviter = _invite_sender(room)
    expected = legacy.bridge_bot_mxid()
    if not expected or inviter != expected:
        print(f"matrix invite ignored room={room_id} inviter={inviter or 'unknown'}", flush=True)
        return False
    _matrix_post(f"/_matrix/client/v3/join/{quote(room_id, safe='')}")
    print(f"auto-joined Meta portal room={room_id}", flush=True)
    return True


def import_recent_history(room_id: str) -> int:
    if not setting_bool("import_history_on_join", True):
        return 0
    limit = setting_int("history_import_limit", DEFAULT_HISTORY_LIMIT)
    if limit <= 0:
        return 0
    response = _matrix_get(
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages",
        params={"dir": "b", "limit": limit},
    )
    chunk = (response.json() or {}).get("chunk") or []
    imported = 0
    # Matrix returns backwards history. Process oldest -> newest so Chatwoot reads naturally.
    for event in reversed(chunk):
        if event.get("type") != "m.room.message":
            continue
        try:
            # Explicit history import intentionally bypasses the live activation timestamp,
            # but the normal event_seen guard still makes this idempotent.
            prod.matrix_event_to_chatwoot(room_id, event)
            if legacy.event_seen(event.get("event_id", "")):
                imported += 1
        except Exception as exc:
            print(f"history import failed room={room_id} event={event.get('event_id')}: {exc}", flush=True)
            break
    return imported


_base_live_matrix_event = legacy.matrix_event_to_chatwoot


def enhanced_live_matrix_event(room_id: str, event: dict):
    if event.get("type") == "m.room.message":
        repair_deleted_conversation(room_id)
    return _base_live_matrix_event(room_id, event)


def enhanced_sync_once():
    since = legacy.get_setting("matrix_next_batch")
    params = {"timeout": 25000}
    if since:
        params["since"] = since
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/sync",
        headers=legacy.matrix_headers(), params=params, timeout=35,
    )
    response.raise_for_status()
    data = response.json()
    next_batch = data.get("next_batch")
    if not since:
        if next_batch:
            legacy.set_setting("matrix_next_batch", next_batch)
        return

    rooms = data.get("rooms") or {}
    for room_id, room in (rooms.get("invite") or {}).items():
        try:
            if auto_join_room(room_id, room):
                imported = import_recent_history(room_id)
                print(f"Meta portal ready room={room_id} imported_history={imported}", flush=True)
        except Exception as exc:
            print(f"matrix auto-join failed room={room_id}: {exc}", flush=True)

    for room_id, room in (rooms.get("join") or {}).items():
        for event in ((room.get("timeline") or {}).get("events") or []):
            try:
                enhanced_live_matrix_event(room_id, event)
            except Exception as exc:
                print(f"matrix event failed room={room_id} event={event.get('event_id')}: {exc}", flush=True)
                # Keep the sync token unchanged so this event can retry next loop.
                return
    if next_batch:
        legacy.set_setting("matrix_next_batch", next_batch)


def api_inbox_details() -> dict:
    if not legacy.configured():
        raise RuntimeError("Configure Chatwoot first")
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/inboxes/{inbox_id}")
    if not isinstance(data, dict):
        raise RuntimeError("Chatwoot returned an invalid inbox response")
    if str(data.get("channel_type") or "") != "Channel::Api":
        raise RuntimeError("The configured Chatwoot inbox is not an API Channel")
    return data


def configure_api_inbox_callback(expected_url: str) -> str:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    expected_url = expected_url.rstrip("/")
    # Chatwoot Channel::Api exposes webhook_url as an editable channel attribute.
    _chatwoot_request(
        "PATCH",
        f"/api/v1/accounts/{account_id}/inboxes/{inbox_id}",
        json={"channel": {"webhook_url": expected_url}},
        headers={"Content-Type": "application/json"},
    )
    return verify_api_inbox_callback(expected_url)


def verify_api_inbox_callback(expected_url: str) -> str:
    data = api_inbox_details()
    expected_url = expected_url.rstrip("/")
    actual = str(data.get("webhook_url") or data.get("callback_webhook_url") or "").rstrip("/")
    if actual != expected_url:
        raise RuntimeError(f"API inbox callback is {actual or 'not configured'}; expected {expected_url}")
    secret = str(data.get("secret") or "").strip()
    if secret:
        old = legacy.get_setting("chatwoot_api_inbox_signing_secret")
        legacy.set_setting("chatwoot_api_inbox_signing_secret", secret)
        if old != secret:
            legacy.set_setting("api_inbox_delivery_verified_at", "")
    elif not legacy.get_setting("chatwoot_api_inbox_signing_secret"):
        raise RuntimeError("Chatwoot did not expose the API inbox signing secret to this admin token")
    checked = admin_ui._now_utc()
    legacy.set_setting("api_inbox_callback_verified_at", checked)
    return f"PASS — API inbox callback + signing secret verified; {checked}"


def verify_inbox_signature(raw_body: bytes, signature: str, timestamp: str, now: int | None = None) -> bool:
    secret = legacy.get_setting("chatwoot_api_inbox_signing_secret")
    if not secret:
        raise RuntimeError("Chatwoot API inbox signing secret is not configured")
    # Use the same HMAC format already proven against account webhooks, but with
    # Channel::Api's independent secret.
    import hmac
    import hashlib as _hashlib
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid Chatwoot webhook timestamp") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp_int) > admin_ui.WEBHOOK_MAX_AGE_SECONDS:
        raise RuntimeError("Chatwoot webhook timestamp is too old or too far in the future")
    signed = str(timestamp).encode() + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode(), signed, _hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise RuntimeError("Invalid Chatwoot API inbox webhook signature")
    return True


@admin_ui.app.post("/webhooks/chatwoot/inbox")
async def api_inbox_webhook(request: Request):
    raw = await request.body()
    try:
        verify_inbox_signature(
            raw,
            request.headers.get("X-Chatwoot-Signature", ""),
            request.headers.get("X-Chatwoot-Timestamp", ""),
        )
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    configured_inbox = str(legacy.get_setting("chatwoot_inbox_id"))
    payload_inbox = str(((payload.get("inbox") or {}).get("id") or (payload.get("conversation") or {}).get("inbox_id") or ""))
    if payload_inbox and payload_inbox != configured_inbox:
        return JSONResponse({"ok": True, "ignored": True, "reason": "outside_configured_chatwoot_inbox"})
    legacy.set_setting("api_inbox_delivery_verified_at", admin_ui._now_utc())
    return await admin_ui._handle_chatwoot_payload(payload)


def install_runtime_enhancements() -> None:
    # All Meta portal messages still pass through the final inbox-isolation guard.
    prod.ensure_room_link = enhanced_ensure_room_link
    legacy.ensure_room_link = enhanced_ensure_room_link
    legacy.matrix_event_to_chatwoot = enhanced_live_matrix_event
    legacy.sync_once = enhanced_sync_once


# A small persistent navigation affordance so the new page is discoverable from
# the existing admin without rewriting the established setup UI.
_original_page_shell = admin_ui.page_shell


def enhanced_page_shell(title: str):
    _original_page_shell(title)
    if title != "Integration Admin · Login":
        with ui.element("div").classes("fixed bottom-5 right-5 z-50 flex gap-2"):
            ui.button("Dashboard", on_click=lambda: ui.navigate.to("/admin"), icon="dashboard").props("outline")
            ui.button("Sync & delivery", on_click=lambda: ui.navigate.to("/admin/operations"), icon="tune")


admin_ui.page_shell = enhanced_page_shell


@ui.page("/admin/operations")
def operations_page(request: Request):
    admin_ui.page_shell("Integration Admin · Sync & delivery")
    if not admin_ui.authenticated():
        ui.navigate.to("/admin/login")
        return

    state = operations_state()
    base_url = str(request.base_url).rstrip("/")
    callback_url = base_url + "/webhooks/chatwoot/inbox"

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-5"):
        ui.label("Meta sync & Chatwoot delivery").classes("text-2xl font-bold")
        ui.label(
            "Operational behavior lives here. Element is not part of the normal workflow: "
            "Meta portal invites are auto-accepted only when they come from the configured mautrix-meta bot."
        ).classes("text-slate-600")

        with ui.card().classes("w-full p-5"):
            ui.label("1. Matrix portal automation").classes("text-lg font-semibold")
            auto_join = ui.switch("Auto-accept new Meta portal rooms", value=state["auto_join"])
            auto_join.tooltip("Only invitations sent by the configured Meta bridge bot are accepted.")
            import_history = ui.switch("Import recent customer history when a portal first joins", value=state["import_history"])
            history_limit = ui.number(
                "Recent Matrix messages to inspect per new portal", value=state["history_limit"], min=0, max=1000, step=25
            ).classes("w-full")
            ui.label(
                "Historical agent/outbound Matrix messages are intentionally not replayed to Meta. "
                "Inbound customer history is idempotent by Matrix event ID."
            ).classes("text-sm text-slate-500")

        with ui.card().classes("w-full p-5"):
            ui.label("2. Contact identity").classes("text-lg font-semibold")
            sync_profiles = ui.switch("Sync Meta display name and avatar from Matrix", value=state["sync_profiles"])
            ui.label(
                "This replaces technical names such as meta_123... with the display name and avatar mautrix-meta exposes in Matrix."
            ).classes("text-sm text-slate-500")

        with ui.card().classes("w-full p-5"):
            ui.label("3. Deleted-conversation recovery").classes("text-lg font-semibold")
            repair_deleted = ui.switch(
                "Automatically recreate a Chatwoot conversation if the linked one was deleted",
                value=state["repair_deleted"],
            )
            ui.label(
                "The stable Matrix room/contact remains; only the stale Chatwoot conversation mapping is discarded and rebuilt."
            ).classes("text-sm text-slate-500")

        async def save_settings():
            try:
                save_operations_settings(
                    auto_join=bool(auto_join.value),
                    import_history=bool(import_history.value),
                    history_limit=int(history_limit.value or 0),
                    sync_profiles=bool(sync_profiles.value),
                    repair_deleted=bool(repair_deleted.value),
                )
                ui.notify("Sync & delivery settings saved", type="positive")
            except Exception as exc:
                ui.notify(str(exc), type="negative")

        ui.button("Save sync & delivery settings", on_click=save_settings, icon="save")

        with ui.card().classes("w-full p-5"):
            ui.label("4. Chatwoot API Inbox callback").classes("text-lg font-semibold")
            ui.label(
                "This is the canonical outbound transport for agent replies. It prevents Chatwoot from showing a delivery error while "
                "a separate account webhook delivered the same message successfully."
            ).classes("text-sm text-slate-600")
            with ui.row().classes("w-full items-center"):
                ui.input("Required callback URL", value=callback_url).props("readonly").classes("grow")
                ui.button("Copy", on_click=lambda: ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(callback_url)})"), icon="content_copy").props("flat")
            callback_status = ui.label(
                "Verified: " + state["api_inbox_callback_verified_at"]
                if state["api_inbox_callback_verified_at"] else
                "Not verified yet"
            ).classes("text-sm")

            async def apply_callback():
                try:
                    result = configure_api_inbox_callback(callback_url)
                    callback_status.set_text(result)
                    ui.notify(result, type="positive")
                except Exception as exc:
                    callback_status.set_text("FAIL — " + str(exc))
                    ui.notify(str(exc), type="negative")

            async def verify_callback():
                try:
                    result = verify_api_inbox_callback(callback_url)
                    callback_status.set_text(result)
                    ui.notify(result, type="positive")
                except Exception as exc:
                    callback_status.set_text("FAIL — " + str(exc))
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("gap-2"):
                ui.button("Apply callback to configured inbox", on_click=apply_callback, icon="link")
                ui.button("Verify callback", on_click=verify_callback, icon="verified").props("outline")
            ui.label(
                "The integration imports the API inbox signing secret from Chatwoot and verifies signed deliveries. "
                "The existing account webhook may remain during migration; message IDs are deduplicated."
            ).classes("text-xs text-slate-500")

        with ui.card().classes("w-full p-5"):
            ui.label("5. History model").classes("text-lg font-semibold")
            ui.label(
                "mautrix-meta is configured to discover existing Meta threads and backfill a bounded number of messages per room. "
                "This page controls how many recent Matrix events are copied into Chatwoot when each portal joins. "
                "Full outbound-history replay is intentionally disabled because replaying old agent messages could resend them to Meta."
            ).classes("text-sm text-slate-600")


install_runtime_enhancements()
