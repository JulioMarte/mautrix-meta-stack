"""Production runtime enhancements for Meta -> Matrix -> Chatwoot.

Loaded by final_app before Matrix sync starts. It auto-joins trusted Meta portals,
imports recent inbound history, syncs contact identity, repairs deleted Chatwoot
conversation mappings, and exposes an operator page plus a signed API-inbox
callback endpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from nicegui import app as nicegui_app, ui

import final_app as runtime

legacy = runtime.legacy
prod = runtime.prod
DEFAULT_HISTORY_LIMIT = 100
PROFILE_CACHE_SECONDS = 600
WEBHOOK_MAX_AGE_SECONDS = 300
_profile_cache: dict[str, tuple[float, dict]] = {}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def authenticated() -> bool:
    return bool(nicegui_app.storage.user.get("authenticated", False))


def setting_bool(key: str, default: bool) -> bool:
    raw = legacy.get_setting(key, "")
    return default if raw == "" else str(raw).lower() in {"1", "true", "yes", "on"}


def setting_int(key: str, default: int, minimum: int = 0, maximum: int = 1000) -> int:
    try:
        value = int(legacy.get_setting(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def save_operations_settings(*, auto_join: bool, import_history: bool, history_limit: int,
                             sync_profiles: bool, repair_deleted: bool) -> None:
    legacy.set_setting("auto_join_meta_portals", "1" if auto_join else "0")
    legacy.set_setting("import_history_on_join", "1" if import_history else "0")
    legacy.set_setting("history_import_limit", str(max(0, min(1000, int(history_limit)))))
    legacy.set_setting("sync_contact_profiles", "1" if sync_profiles else "0")
    legacy.set_setting("repair_deleted_conversations", "1" if repair_deleted else "0")


def operations_state() -> dict:
    return {
        "auto_join": setting_bool("auto_join_meta_portals", True),
        "import_history": setting_bool("import_history_on_join", True),
        "history_limit": setting_int("history_import_limit", DEFAULT_HISTORY_LIMIT),
        "sync_profiles": setting_bool("sync_contact_profiles", True),
        "repair_deleted": setting_bool("repair_deleted_conversations", True),
        "callback_verified": legacy.get_setting("api_inbox_callback_verified_at"),
        "delivery_verified": legacy.get_setting("api_inbox_delivery_verified_at"),
        "callback_secret_saved": bool(legacy.get_setting("chatwoot_api_inbox_signing_secret")),
    }


def _matrix_get(path: str, *, params=None, timeout=20):
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}{path}", headers=legacy.matrix_headers(), params=params, timeout=timeout
    )
    response.raise_for_status()
    return response


def _matrix_post(path: str, payload=None, timeout=20):
    response = requests.post(
        f"{legacy.MATRIX_HOMESERVER}{path}", headers=legacy.matrix_headers(), json=payload or {}, timeout=timeout
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
    if not mxc.startswith("mxc://") or "/" not in mxc[6:]:
        return None
    server, media_id = mxc[6:].split("/", 1)
    for path in (
        f"/_matrix/client/v1/media/download/{quote(server, safe='')}/{quote(media_id, safe='')}",
        f"/_matrix/media/v3/download/{quote(server, safe='')}/{quote(media_id, safe='')}",
    ):
        response = requests.get(
            f"{legacy.MATRIX_HOMESERVER}{path}", headers=legacy.matrix_headers(), timeout=20
        )
        if response.status_code == 200 and response.content:
            content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
            extension = mimetypes.guess_extension(content_type) or ".jpg"
            return response.content, content_type, "avatar" + extension
    return None


def fallback_display_name(sender: str) -> str:
    return sender.split(":", 1)[0].lstrip("@").replace("_", " ").strip() or "Meta contact"


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


def chatwoot_request(method: str, path: str, **kwargs):
    headers = {"api_access_token": legacy.get_setting("chatwoot_api_token"), **kwargs.pop("headers", {})}
    response = requests.request(method, legacy.chatwoot_url(path), headers=headers, timeout=20, **kwargs)
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def update_chatwoot_contact_profile(account_id: int, contact_id: int, sender: str) -> None:
    if not setting_bool("sync_contact_profiles", True):
        return
    identity = contact_identity(sender)
    try:
        avatar = matrix_avatar_bytes(identity["avatar_url"]) if identity.get("avatar_url") else None
        if avatar:
            body, content_type, filename = avatar
            chatwoot_request(
                "PUT", f"/api/v1/accounts/{account_id}/contacts/{contact_id}",
                data={"name": identity["name"]}, files={"avatar": (filename, body, content_type)},
            )
        else:
            chatwoot_request(
                "PUT", f"/api/v1/accounts/{account_id}/contacts/{contact_id}",
                json={"name": identity["name"]}, headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        # Metadata is useful but must never stop a customer message.
        print(f"chatwoot contact profile sync failed contact={contact_id}: {exc}", flush=True)


def existing_room_link(room_id: str):
    with legacy.db() as conn:
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


def delete_room_link(room_id: str) -> None:
    with legacy.db() as conn:
        conn.execute("DELETE FROM room_links WHERE room_id = ?", (room_id,))


def repair_deleted_conversation(room_id: str) -> bool:
    if not setting_bool("repair_deleted_conversations", True):
        return False
    row = existing_room_link(room_id)
    if not row or not legacy.configured():
        return False
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    try:
        prod.cw_get(f"/api/v1/accounts/{account_id}/conversations/{int(row['conversation_id'])}")
        return False
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
    delete_room_link(room_id)
    print(f"removed stale Chatwoot mapping room={room_id} conversation={row['conversation_id']}", flush=True)
    return True


def enhanced_ensure_room_link(room_id: str, sender: str):
    repair_deleted_conversation(room_id)
    existing = existing_room_link(room_id)
    if existing:
        update_chatwoot_contact_profile(
            int(legacy.get_setting("chatwoot_account_id")), int(existing["contact_id"]), sender
        )
        return existing

    identity = contact_identity(sender)
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    identifier = "matrix-room:" + hashlib.sha256(room_id.encode()).hexdigest()[:32]
    generated_source_id = "mx-" + hashlib.sha256((room_id + ":source").encode()).hexdigest()[:30]
    try:
        raw = legacy.cw_post(
            f"/api/v1/accounts/{account_id}/contacts",
            {"inbox_id": inbox_id, "name": identity["name"], "identifier": identifier,
             "additional_attributes": {"matrix_room_id": room_id, "matrix_sender": sender}},
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
        {"source_id": source_id, "inbox_id": inbox_id, "contact_id": contact_id, "status": "open",
         "custom_attributes": {"matrix_room_id": room_id, "matrix_sender": sender}},
    )
    conversation_id = int(conversation["id"])
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (room_id, contact_id, source_id, conversation_id, int(time.time())),
        )
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


def invite_sender(room: dict) -> str:
    for event in ((room.get("invite_state") or {}).get("events") or []):
        if event.get("type") == "m.room.member" and (event.get("content") or {}).get("membership") == "invite" \
                and event.get("state_key") == legacy.MATRIX_ADMIN_MXID:
            return str(event.get("sender") or "")
    return ""


def auto_join_room(room_id: str, room: dict) -> bool:
    if not setting_bool("auto_join_meta_portals", True):
        return False
    inviter = invite_sender(room)
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
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages", params={"dir": "b", "limit": limit}
    )
    imported = 0
    for event in reversed((response.json() or {}).get("chunk") or []):
        if event.get("type") != "m.room.message":
            continue
        before = legacy.event_seen(event.get("event_id", ""))
        try:
            # Bypass only the activation timestamp. prod still filters bridge/admin senders,
            # portal provenance, message types and duplicate Matrix event IDs.
            prod.matrix_event_to_chatwoot(room_id, event)
        except Exception as exc:
            print(f"history import failed room={room_id} event={event.get('event_id')}: {exc}", flush=True)
            break
        if not before and legacy.event_seen(event.get("event_id", "")):
            imported += 1
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
                return
    if next_batch:
        legacy.set_setting("matrix_next_batch", next_batch)


def api_inbox_details() -> dict:
    if not legacy.configured():
        raise RuntimeError("Configure Chatwoot first")
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/inboxes/{inbox_id}")
    if not isinstance(data, dict) or str(data.get("channel_type") or "") != "Channel::Api":
        raise RuntimeError("The configured Chatwoot inbox is not an API Channel")
    return data


def verify_api_inbox_callback(expected_url: str) -> str:
    data = api_inbox_details()
    expected = expected_url.rstrip("/")
    actual = str(data.get("webhook_url") or data.get("callback_webhook_url") or "").rstrip("/")
    if actual != expected:
        raise RuntimeError(f"API inbox callback is {actual or 'not configured'}; expected {expected}")
    secret = str(data.get("hmac_token") or data.get("secret") or "").strip()
    if secret:
        previous = legacy.get_setting("chatwoot_api_inbox_signing_secret")
        legacy.set_setting("chatwoot_api_inbox_signing_secret", secret)
        if previous != secret:
            legacy.set_setting("api_inbox_delivery_verified_at", "")
    elif not legacy.get_setting("chatwoot_api_inbox_signing_secret"):
        raise RuntimeError("Chatwoot did not expose the API inbox HMAC token to this admin token")
    checked = now_utc()
    legacy.set_setting("api_inbox_callback_verified_at", checked)
    return f"PASS — API inbox callback + signing secret verified; {checked}"


def configure_api_inbox_callback(expected_url: str) -> str:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    chatwoot_request(
        "PATCH", f"/api/v1/accounts/{account_id}/inboxes/{inbox_id}",
        json={"channel": {"webhook_url": expected_url.rstrip("/")}},
        headers={"Content-Type": "application/json"},
    )
    return verify_api_inbox_callback(expected_url)


def verify_inbox_signature(raw_body: bytes, signature: str, timestamp: str, now: int | None = None) -> bool:
    secret = legacy.get_setting("chatwoot_api_inbox_signing_secret")
    if not secret:
        raise RuntimeError("Chatwoot API inbox signing secret is not configured")
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid Chatwoot webhook timestamp") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp_int) > WEBHOOK_MAX_AGE_SECONDS:
        raise RuntimeError("Chatwoot webhook timestamp is too old or too far in the future")
    expected = "sha256=" + hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise RuntimeError("Invalid Chatwoot API inbox webhook signature")
    return True


def handle_chatwoot_outgoing(payload: dict) -> dict:
    if payload.get("event") != "message_created":
        return {"ok": True, "ignored": True}
    message_type = payload.get("message_type")
    if message_type not in ("outgoing", 1) or payload.get("private") is True:
        return {"ok": True, "ignored": True}
    conversation = payload.get("conversation") or {}
    conversation_id = conversation.get("id") or payload.get("conversation_id")
    content = str(payload.get("content") or "").strip()
    message_id = str(payload.get("id") or "")
    configured_inbox = str(legacy.get_setting("chatwoot_inbox_id"))
    payload_inbox = str(((payload.get("inbox") or {}).get("id") or conversation.get("inbox_id") or ""))
    if payload_inbox and payload_inbox != configured_inbox:
        return {"ok": True, "ignored": True, "reason": "outside_configured_chatwoot_inbox"}
    if not conversation_id or not content:
        return {"ok": True, "ignored": True}
    event_key = "chatwoot:" + message_id if message_id else ""
    if event_key and legacy.event_seen(event_key):
        return {"ok": True, "duplicate": True}
    with legacy.db() as conn:
        link = conn.execute(
            "SELECT * FROM room_links WHERE conversation_id = ?", (int(conversation_id),)
        ).fetchone()
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped conversation"}
    legacy.send_matrix_message(link["room_id"], content, "cw-" + (message_id or hashlib.sha256(content.encode()).hexdigest()[:20]))
    if event_key:
        legacy.mark_event(event_key, "chatwoot_to_matrix")
    return {"ok": True}


@nicegui_app.post("/webhooks/chatwoot/inbox")
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
    legacy.set_setting("api_inbox_delivery_verified_at", now_utc())
    try:
        return JSONResponse(handle_chatwoot_outgoing(payload))
    except Exception as exc:
        print(f"API inbox outgoing delivery failed: {exc}", flush=True)
        return JSONResponse({"error": "delivery failed"}, status_code=502)


def page_shell(title: str):
    ui.page_title(title)
    ui.colors(primary="#2563eb", positive="#15803d", negative="#b91c1c")
    ui.query("body").classes("bg-slate-50 text-slate-900")


@ui.page("/admin/operations")
def operations_page(request: Request):
    page_shell("Integration Admin · Sync & delivery")
    if not authenticated():
        ui.navigate.to("/admin/login")
        return
    state = operations_state()
    callback_url = str(request.base_url).rstrip("/") + "/webhooks/chatwoot/inbox"
    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-5"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Meta sync & Chatwoot delivery").classes("text-2xl font-bold")
            ui.button("Back to setup", on_click=lambda: ui.navigate.to("/admin"), icon="arrow_back").props("outline")
        ui.label(
            "This page controls runtime behavior. Element is not required for normal operation."
        ).classes("text-slate-600")

        with ui.card().classes("w-full p-5"):
            ui.label("Matrix portal automation").classes("text-lg font-semibold")
            auto_join = ui.switch("Auto-accept new Meta portal rooms", value=state["auto_join"])
            ui.label("Only invitations sent by the configured mautrix-meta bridge bot are accepted.").classes("text-xs text-slate-500")
            import_history = ui.switch("Import recent inbound customer history when a portal joins", value=state["import_history"])
            history_limit = ui.number("Messages to inspect per new portal", value=state["history_limit"], min=0, max=1000, step=25).classes("w-full")
            ui.label("Historical outbound messages are not replayed, preventing accidental duplicate sends to Meta.").classes("text-xs text-slate-500")

        with ui.card().classes("w-full p-5"):
            ui.label("Contact identity & recovery").classes("text-lg font-semibold")
            sync_profiles = ui.switch("Sync Meta display name and avatar from Matrix", value=state["sync_profiles"])
            repair_deleted = ui.switch("Recreate the Chatwoot conversation if an agent deleted the linked conversation", value=state["repair_deleted"])

        async def save_settings():
            try:
                save_operations_settings(
                    auto_join=bool(auto_join.value), import_history=bool(import_history.value),
                    history_limit=int(history_limit.value or 0), sync_profiles=bool(sync_profiles.value),
                    repair_deleted=bool(repair_deleted.value),
                )
                ui.notify("Sync & delivery settings saved", type="positive")
            except Exception as exc:
                ui.notify(str(exc), type="negative")
        ui.button("Save runtime settings", on_click=save_settings, icon="save")

        with ui.card().classes("w-full p-5"):
            ui.label("Chatwoot API Inbox callback").classes("text-lg font-semibold")
            ui.label(
                "Agent replies should leave Chatwoot through the API Inbox callback. This removes the false 'Error sending' state caused by a broken/missing channel callback while an account webhook delivered separately."
            ).classes("text-sm text-slate-600")
            with ui.row().classes("w-full items-center"):
                ui.input("Required callback URL", value=callback_url).props("readonly").classes("grow")
                ui.button("Copy", on_click=lambda: ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(callback_url)})"), icon="content_copy").props("flat")
            status = ui.label(
                "Verified: " + state["callback_verified"] if state["callback_verified"] else "Not verified yet"
            ).classes("text-sm")

            async def apply_callback():
                try:
                    result = configure_api_inbox_callback(callback_url)
                    status.set_text(result)
                    ui.notify(result, type="positive")
                except Exception as exc:
                    status.set_text("FAIL — " + str(exc))
                    ui.notify(str(exc), type="negative")

            async def verify_callback():
                try:
                    result = verify_api_inbox_callback(callback_url)
                    status.set_text(result)
                    ui.notify(result, type="positive")
                except Exception as exc:
                    status.set_text("FAIL — " + str(exc))
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("gap-2"):
                ui.button("Apply callback to configured inbox", on_click=apply_callback, icon="link")
                ui.button("Verify callback", on_click=verify_callback, icon="verified").props("outline")
            ui.label(
                "The signing secret is imported from the selected API inbox. Account-level webhook delivery can remain during migration; identical Chatwoot message IDs are deduplicated."
            ).classes("text-xs text-slate-500")

        with ui.card().classes("w-full p-5"):
            ui.label("History behavior").classes("text-lg font-semibold")
            ui.label(
                "The bridge discovers existing Meta threads and backfills a bounded Matrix history. This integration then copies recent inbound customer events into Chatwoot once per Matrix event ID. Outbound history is deliberately not replayed because replaying it could send old agent messages again."
            ).classes("text-sm text-slate-600")


def install_runtime_enhancements() -> None:
    prod.ensure_room_link = enhanced_ensure_room_link
    legacy.ensure_room_link = enhanced_ensure_room_link
    legacy.matrix_event_to_chatwoot = enhanced_live_matrix_event
    legacy.sync_once = enhanced_sync_once
