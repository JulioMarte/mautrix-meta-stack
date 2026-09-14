"""Admin v2 and runtime reliability fixes for the single-client integration.

This module is imported by final_app before the Matrix sync thread starts. It keeps
/admin as a stable entrypoint while moving operators to dedicated Basic, Advanced
and Status pages, and fixes first-sync invite handling so Meta portals never depend
on a human clicking Accept in Element.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import requests
from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app, ui

import final_app as runtime
import runtime_enhancements as enhancements

legacy = runtime.legacy
prod = runtime.prod

IP_CHECK_URL = "https://api.ipify.org?format=json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def authenticated() -> bool:
    return bool(app.storage.user.get("authenticated", False))


def _origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",", 1)[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",", 1)[0].strip()
    return f"{proto}://{host}".rstrip("/")


def _clean_chatwoot_base(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Chatwoot URL must be a valid http(s) base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Chatwoot URL must not contain credentials, query parameters or fragments")
    return value


def _chatwoot_headers(token: str | None = None) -> dict:
    value = (token or legacy.get_setting("chatwoot_api_token") or "").strip()
    if not value:
        raise RuntimeError("Chatwoot Personal Access Token is not configured")
    return {"api_access_token": value, "Accept": "application/json"}


def _chatwoot_request(method: str, path: str, *, base: str | None = None, token: str | None = None, **kwargs):
    root = _clean_chatwoot_base(base or legacy.get_setting("chatwoot_base_url"))
    response = requests.request(
        method,
        root + path,
        headers={**_chatwoot_headers(token), **kwargs.pop("headers", {})},
        timeout=20,
        **kwargs,
    )
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def discover_chatwoot(base: str, token: str) -> dict:
    base = _clean_chatwoot_base(base)
    profile = _chatwoot_request("GET", "/api/v1/profile", base=base, token=token)
    account_id = profile.get("account_id")
    accounts = profile.get("accounts") or []
    if not account_id and accounts:
        account_id = accounts[0].get("id")
    if not account_id:
        raise RuntimeError("Chatwoot profile did not return an account ID")
    inboxes_raw = _chatwoot_request("GET", f"/api/v1/accounts/{int(account_id)}/inboxes", base=base, token=token)
    inboxes = inboxes_raw.get("payload") if isinstance(inboxes_raw, dict) else inboxes_raw
    if not isinstance(inboxes, list):
        raise RuntimeError("Chatwoot did not return an inbox list")
    return {
        "account_id": int(account_id),
        "inboxes": [
            {
                "id": int(item["id"]),
                "name": str(item.get("name") or f"Inbox {item['id']}"),
                "channel_type": str(item.get("channel_type") or ""),
            }
            for item in inboxes
            if isinstance(item, dict) and item.get("id") is not None
        ],
    }


def save_basic_connection(base: str, token: str, account: str, inbox: str) -> None:
    base = _clean_chatwoot_base(base)
    if not str(account).isdigit() or not str(inbox).isdigit():
        raise ValueError("Account ID and Inbox ID must be numeric")
    current_token = legacy.get_setting("chatwoot_api_token")
    token = (token or "").strip() or current_token
    if not token:
        raise ValueError("Chatwoot Personal Access Token is required")

    old = (
        legacy.get_setting("chatwoot_base_url"),
        legacy.get_setting("chatwoot_account_id"),
        legacy.get_setting("chatwoot_inbox_id"),
        current_token,
    )
    new = (base, str(account), str(inbox), token)
    legacy.set_setting("chatwoot_base_url", base)
    legacy.set_setting("chatwoot_account_id", str(account))
    legacy.set_setting("chatwoot_inbox_id", str(inbox))
    legacy.set_setting("chatwoot_api_token", token)
    if new != old:
        legacy.set_setting("chatwoot_verified_at", "")
        legacy.set_setting("api_inbox_callback_verified_at", "")
        legacy.set_setting("api_inbox_delivery_verified_at", "")
    runtime.ensure_activation_boundary()


def verify_chatwoot() -> str:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    data = _chatwoot_request("GET", f"/api/v1/accounts/{account_id}/inboxes/{inbox_id}")
    if str(data.get("channel_type") or "") != "Channel::Api":
        raise RuntimeError("The selected Chatwoot inbox must be an API Channel")
    checked = _now_utc()
    legacy.set_setting("chatwoot_verified_at", checked)
    return f"PASS — Chatwoot API Channel verified; {checked}"


def _extract_hooks(data) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if "url" in data or "subscriptions" in data:
            return [data]
        for key in ("payload", "webhooks", "data"):
            if key in data:
                result = _extract_hooks(data[key])
                if result:
                    return result
    return []


def legacy_account_webhooks(expected_old_url: str) -> list[dict]:
    if not legacy.get_setting("chatwoot_account_id"):
        return []
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    try:
        hooks = _extract_hooks(_chatwoot_request("GET", f"/api/v1/accounts/{account_id}/webhooks"))
    except Exception:
        return []
    expected = expected_old_url.rstrip("/")
    return [hook for hook in hooks if str(hook.get("url") or "").rstrip("/") == expected]


def remove_legacy_account_webhooks(expected_old_url: str) -> int:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    removed = 0
    for hook in legacy_account_webhooks(expected_old_url):
        hook_id = hook.get("id")
        if hook_id is None:
            continue
        _chatwoot_request("DELETE", f"/api/v1/accounts/{account_id}/webhooks/{int(hook_id)}")
        removed += 1
    if removed:
        legacy.set_setting("webhook_registration_verified_at", "")
        legacy.set_setting("webhook_delivery_verified_at", "")
    return removed


def _public_ip(proxies=None) -> str:
    session = requests.Session()
    session.trust_env = False
    response = session.get(IP_CHECK_URL, proxies=proxies, timeout=20)
    response.raise_for_status()
    observed = str(response.json().get("ip") or "").strip()
    return str(ipaddress.ip_address(observed))


def test_saved_meta_route() -> dict:
    enabled = legacy.get_setting("proxy_enabled") == "1"
    proxy = legacy.get_setting("proxy_url").strip()
    direct = _public_ip()
    checked = _now_utc()
    if not enabled:
        legacy.set_setting("proxy_verified_mode", "DIRECT")
        legacy.set_setting("proxy_verified_at", checked)
        legacy.set_setting("proxy_verified_ip", "")
        return {"mode": "DIRECT", "direct_ip": direct, "route_ip": direct, "checked_at": checked}
    if not proxy:
        raise RuntimeError("Proxy is enabled but no proxy URL is saved")
    prod.validate_proxy_url(proxy)
    routed = _public_ip({"http": proxy, "https": proxy})
    legacy.set_setting("proxy_verified_mode", "PROXY")
    legacy.set_setting("proxy_verified_at", checked)
    legacy.set_setting("proxy_verified_ip", routed)
    return {"mode": "PROXY", "direct_ip": direct, "route_ip": routed, "checked_at": checked}


def save_proxy(enabled: bool, proxy_url: str) -> None:
    proxy_url = (proxy_url or "").strip()
    current = legacy.get_setting("proxy_url").strip()
    candidate = proxy_url or current
    if enabled:
        if not candidate:
            raise ValueError("Enter a proxy URL before enabling the Meta proxy")
        prod.validate_proxy_url(candidate)
    old = (legacy.get_setting("proxy_enabled") == "1", current)
    legacy.set_setting("proxy_enabled", "1" if enabled else "0")
    if proxy_url:
        prod.validate_proxy_url(proxy_url)
        legacy.set_setting("proxy_url", proxy_url)
    new = (bool(enabled), candidate)
    if new != old:
        legacy.set_setting("proxy_verified_at", "")
        legacy.set_setting("proxy_verified_ip", "")
        legacy.set_setting("proxy_verified_mode", "")


def _matrix_sync_snapshot() -> dict:
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/sync",
        headers=legacy.matrix_headers(),
        params={"timeout": 0},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def reconcile_pending_meta_invites() -> dict:
    """Join every currently pending trusted mautrix-meta invite without touching the live sync token."""
    data = _matrix_sync_snapshot()
    invites = ((data.get("rooms") or {}).get("invite") or {})
    joined = 0
    ignored = 0
    errors = []
    for room_id, room in invites.items():
        try:
            if enhancements.auto_join_room(room_id, room):
                joined += 1
                try:
                    enhancements.import_recent_history(room_id)
                except Exception as exc:
                    errors.append(f"{room_id}: joined but history import failed: {exc}")
            else:
                ignored += 1
        except Exception as exc:
            errors.append(f"{room_id}: {exc}")
    checked = _now_utc()
    legacy.set_setting("meta_invite_reconcile_at", checked)
    legacy.set_setting("meta_invite_reconcile_joined", str(joined))
    legacy.set_setting("meta_invite_reconcile_error", " | ".join(errors)[:1000])
    return {"joined": joined, "ignored": ignored, "errors": errors, "checked_at": checked}


def reliable_sync_once():
    """Process trusted invites even during the initial /sync checkpoint.

    The previous implementation returned immediately on the first sync. That safely
    skipped historical joined-room timelines, but it also skipped pending invites and
    then advanced next_batch. Those invites could therefore require a manual Accept
    in Element forever. We process invites on every sync, including the first one,
    while still skipping old joined-room timelines on the initial checkpoint.
    """
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
    rooms = data.get("rooms") or {}

    for room_id, room in (rooms.get("invite") or {}).items():
        try:
            if enhancements.auto_join_room(room_id, room):
                legacy.set_setting("last_meta_auto_join_at", _now_utc())
                legacy.set_setting("last_meta_auto_join_room", room_id)
                enhancements.import_recent_history(room_id)
        except Exception as exc:
            legacy.set_setting("last_meta_auto_join_error", str(exc)[:1000])
            print(f"matrix auto-join failed room={room_id}: {exc}", flush=True)

    if not since:
        if next_batch:
            legacy.set_setting("matrix_next_batch", next_batch)
        return

    for room_id, room in (rooms.get("join") or {}).items():
        for event in ((room.get("timeline") or {}).get("events") or []):
            try:
                enhancements.enhanced_live_matrix_event(room_id, event)
            except Exception as exc:
                print(f"matrix event failed room={room_id} event={event.get('event_id')}: {exc}", flush=True)
                return
    if next_batch:
        legacy.set_setting("matrix_next_batch", next_batch)


def _status_row(label: str, ok: bool, detail: str):
    with ui.row().classes("items-start gap-3 w-full"):
        ui.icon("check_circle" if ok else "warning", size="sm").classes("text-green-700" if ok else "text-amber-600")
        with ui.column().classes("gap-0 grow"):
            ui.label(label).classes("font-medium")
            ui.label(detail).classes("text-xs text-slate-500")


def _admin_chrome(active: str):
    ui.page_title("Matrix ↔ Chatwoot Admin")
    ui.colors(primary="#2563eb", positive="#15803d", negative="#b91c1c", warning="#d97706")
    ui.query("body").classes("bg-slate-50 text-slate-900")
    drawer = ui.left_drawer(value=True).classes("bg-white border-r border-slate-200")
    with drawer:
        with ui.column().classes("w-full gap-1 p-3"):
            ui.label("Integration Admin").classes("text-lg font-semibold px-2 py-2")
            nav = [
                ("basic", "Basic setup", "tune", "/admin/basic"),
                ("advanced", "Advanced", "settings", "/admin/advanced"),
                ("status", "Status & tests", "monitor_heart", "/admin/status"),
            ]
            for key, label, icon, target in nav:
                classes = "w-full justify-start " + ("bg-blue-50 text-blue-700" if active == key else "")
                ui.button(label, icon=icon, on_click=lambda target=target: ui.navigate.to(target)).props("flat no-caps").classes(classes)
            ui.separator().classes("my-3")
            ui.label("Element is optional for diagnostics. Normal operation should happen entirely through Chatwoot.").classes("text-xs text-slate-500 px-2")
    with ui.header().classes("items-center justify-between bg-white text-slate-900 border-b border-slate-200"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="menu", on_click=drawer.toggle).props("flat round")
            ui.icon("hub").classes("text-blue-600")
            ui.label("Matrix ↔ Chatwoot").classes("font-semibold text-lg")
        async def logout():
            app.storage.user.clear()
            ui.navigate.to("/admin/login")
        ui.button("Logout", icon="logout", on_click=logout).props("flat no-caps")


def _require_auth() -> bool:
    if authenticated():
        return True
    ui.navigate.to("/admin/login")
    return False


@ui.page("/admin/basic")
def basic_page(request: Request):
    if not _require_auth():
        return
    _admin_chrome("basic")
    base_value = legacy.get_setting("chatwoot_base_url")
    account_value = legacy.get_setting("chatwoot_account_id")
    inbox_value = legacy.get_setting("chatwoot_inbox_id")
    callback_url = _origin(request) + "/webhooks/chatwoot/inbox"
    old_webhook_url = _origin(request) + "/webhooks/chatwoot"

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 md:p-6 gap-5"):
        ui.label("Basic setup").classes("text-3xl font-bold")
        ui.label("Connect Chatwoot and configure the one callback that carries agent replies to Meta. No account-level webhook is required for the normal path.").classes("text-slate-600")

        with ui.card().classes("w-full p-6"):
            ui.label("1 · Chatwoot API connection").classes("text-xl font-semibold")
            base = ui.input("Chatwoot URL", value=base_value, placeholder="https://chatwoot.example.com").props("outlined").classes("w-full")
            token = ui.input("Personal Access Token", value=legacy.get_setting("chatwoot_api_token"), password=True, password_toggle_button=True).props("outlined autocomplete=off").classes("w-full")
            with ui.row().classes("w-full gap-3 flex-wrap"):
                account = ui.input("Account ID", value=account_value).props("outlined").classes("grow min-w-48")
                inbox = ui.input("Inbox ID", value=inbox_value).props("outlined").classes("grow min-w-48")
            detected = ui.select(options={}, label="Detected API inboxes").props("outlined clearable").classes("w-full")

            def pick_inbox(event):
                if event.value is not None:
                    inbox.value = str(event.value)
            detected.on_value_change(pick_inbox)

            async def detect():
                try:
                    result = await asyncio.to_thread(discover_chatwoot, base.value or "", token.value or "")
                    account.value = str(result["account_id"])
                    api_inboxes = [item for item in result["inboxes"] if item["channel_type"] == "Channel::Api"]
                    detected.options = {item["id"]: f"{item['name']} · ID {item['id']}" for item in api_inboxes}
                    detected.update()
                    if len(api_inboxes) == 1:
                        inbox.value = str(api_inboxes[0]["id"])
                        detected.value = api_inboxes[0]["id"]
                    ui.notify(f"Found {len(api_inboxes)} API inbox(es)", type="positive")
                except Exception as exc:
                    ui.notify(f"Detection failed: {exc}", type="negative", close_button=True)

            async def save_and_test():
                try:
                    await asyncio.to_thread(save_basic_connection, base.value or "", token.value or "", account.value or "", inbox.value or "")
                    result = await asyncio.to_thread(verify_chatwoot)
                    ui.notify(result, type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative", close_button=True)

            with ui.row().classes("gap-2"):
                ui.button("Detect API inboxes", icon="travel_explore", on_click=detect).props("outline")
                ui.button("Save & test Chatwoot", icon="save", on_click=save_and_test)

        with ui.card().classes("w-full p-6 border border-blue-100"):
            ui.label("2 · Chatwoot API Inbox callback").classes("text-xl font-semibold")
            ui.label("This is now the canonical outbound path: Chatwoot agent reply → this integration → Matrix → Meta. It replaces the old account-level webhook setup for normal operation.").classes("text-slate-600")
            ui.input("Required callback URL", value=callback_url).props("outlined readonly").classes("w-full")
            callback_state = ui.label(
                "Verified: " + legacy.get_setting("api_inbox_callback_verified_at")
                if legacy.get_setting("api_inbox_callback_verified_at") else "Not verified yet"
            ).classes("text-sm text-slate-600")

            async def apply_callback():
                try:
                    result = await asyncio.to_thread(enhancements.configure_api_inbox_callback, callback_url)
                    callback_state.text = result
                    callback_state.classes(replace="text-sm text-green-700 font-medium")
                    ui.notify("API Inbox callback configured and verified", type="positive")
                except Exception as exc:
                    callback_state.text = f"FAIL — {exc}"
                    callback_state.classes(replace="text-sm text-red-700 font-medium")
                    ui.notify(str(exc), type="negative", close_button=True)

            ui.button("Apply & verify API Inbox callback", icon="link", on_click=apply_callback)
            ui.label("You should not need to create Settings → Integrations → Webhooks for this connector anymore.").classes("text-xs text-slate-500")

            old_hooks = legacy_account_webhooks(old_webhook_url)
            with ui.card().classes("w-full p-4 mt-4 bg-amber-50 border border-amber-200"):
                ui.label("Legacy account webhook migration").classes("font-semibold text-amber-900")
                if old_hooks:
                    ui.label(f"Detected {len(old_hooks)} old account-level webhook(s) pointing to {old_webhook_url}. Keep them only until the API Inbox callback above is verified and a real reply test succeeds, then remove them to avoid two outbound delivery paths.").classes("text-sm text-amber-800")

                    async def remove_old():
                        try:
                            count = await asyncio.to_thread(remove_legacy_account_webhooks, old_webhook_url)
                            ui.notify(f"Removed {count} legacy account webhook(s)", type="positive")
                            ui.navigate.to("/admin/basic")
                        except Exception as exc:
                            ui.notify(f"Could not remove legacy webhook: {exc}", type="negative", close_button=True)
                    ui.button("Remove legacy account webhook(s)", icon="delete", on_click=remove_old).props("outline color=warning")
                else:
                    ui.label("No legacy account-level webhook for this integration URL was detected. Good.").classes("text-sm text-green-700")

        with ui.card().classes("w-full p-6"):
            ui.label("What to test next").classes("text-xl font-semibold")
            ui.label("Send a brand-new Messenger or Marketplace message. It should appear in Chatwoot without opening Element or accepting a room. Then reply from Chatwoot; the message should reach Meta and Chatwoot should not show Error sending.").classes("text-slate-600")
            ui.button("Open Status & tests", icon="monitor_heart", on_click=lambda: ui.navigate.to("/admin/status")).props("outline")


@ui.page("/admin/advanced")
def advanced_page():
    if not _require_auth():
        return
    _admin_chrome("advanced")
    state = enhancements.operations_state()
    proxy_enabled = legacy.get_setting("proxy_enabled") == "1"

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 md:p-6 gap-5"):
        ui.label("Advanced settings").classes("text-3xl font-bold")
        ui.label("Runtime behavior that usually should not be changed after commissioning.").classes("text-slate-600")

        with ui.card().classes("w-full p-6"):
            ui.label("Meta network route").classes("text-xl font-semibold")
            proxy_switch = ui.switch("Route Meta through the saved proxy", value=proxy_enabled)
            proxy_input = ui.input("Proxy URL", value=legacy.get_setting("proxy_url"), password=True, password_toggle_button=True).props("outlined autocomplete=off").classes("w-full")
            route_status = ui.label(
                f"Last check: {legacy.get_setting('proxy_verified_at')} · {legacy.get_setting('proxy_verified_mode') or 'unverified'}"
                if legacy.get_setting("proxy_verified_at") else "Not tested yet"
            ).classes("text-sm text-slate-600")

            async def save_route():
                try:
                    await asyncio.to_thread(save_proxy, bool(proxy_switch.value), proxy_input.value or "")
                    ui.notify("Meta route saved", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative", close_button=True)

            async def test_route():
                try:
                    result = await asyncio.to_thread(test_saved_meta_route)
                    route_status.text = f"PASS — {result['mode']} · VPS {result['direct_ip']} · Meta route {result['route_ip']} · {result['checked_at']}"
                    route_status.classes(replace="text-sm text-green-700 font-medium")
                except Exception as exc:
                    route_status.text = f"FAIL — {exc}"
                    route_status.classes(replace="text-sm text-red-700 font-medium")

            with ui.row().classes("gap-2"):
                ui.button("Save route", icon="save", on_click=save_route)
                ui.button("Test saved route", icon="route", on_click=test_route).props("outline")
            ui.label("Changing DIRECT/PROXY affects the next Meta connection/reconnection. Existing already-open Meta sockets may need to reconnect.").classes("text-xs text-slate-500")

        with ui.card().classes("w-full p-6"):
            ui.label("Portal automation & history").classes("text-xl font-semibold")
            auto_join = ui.switch("Automatically join Meta portal rooms", value=state["auto_join"])
            ui.label("Only invitations from the configured mautrix-meta bot are accepted. New customer chats must never require a manual Accept in Element.").classes("text-xs text-slate-500")
            import_history = ui.switch("Import recent inbound history when a portal is joined", value=state["import_history"])
            history_limit = ui.number("Messages to inspect per portal", value=state["history_limit"], min=0, max=1000, step=25).classes("w-full")
            sync_profiles = ui.switch("Sync Meta display name and avatar into Chatwoot", value=state["sync_profiles"])
            repair_deleted = ui.switch("Recreate deleted Chatwoot conversations automatically", value=state["repair_deleted"])

            async def save_runtime():
                try:
                    enhancements.save_operations_settings(
                        auto_join=bool(auto_join.value),
                        import_history=bool(import_history.value),
                        history_limit=int(history_limit.value or 0),
                        sync_profiles=bool(sync_profiles.value),
                        repair_deleted=bool(repair_deleted.value),
                    )
                    ui.notify("Advanced runtime settings saved", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative", close_button=True)

            reconcile_status = ui.label("").classes("text-sm text-slate-600")
            async def reconcile():
                try:
                    result = await asyncio.to_thread(reconcile_pending_meta_invites)
                    reconcile_status.text = f"Checked {result['checked_at']} · joined {result['joined']} · ignored {result['ignored']} · errors {len(result['errors'])}"
                    reconcile_status.classes(replace="text-sm text-green-700 font-medium" if not result["errors"] else "text-sm text-amber-700 font-medium")
                    ui.notify(f"Joined {result['joined']} pending Meta room(s)", type="positive" if not result["errors"] else "warning")
                except Exception as exc:
                    reconcile_status.text = f"FAIL — {exc}"
                    reconcile_status.classes(replace="text-sm text-red-700 font-medium")

            with ui.row().classes("gap-2"):
                ui.button("Save advanced settings", icon="save", on_click=save_runtime)
                ui.button("Reconcile pending Meta invites now", icon="meeting_room", on_click=reconcile).props("outline")
            ui.label("Use Reconcile once after upgrading if Element still shows old pending invitations. New invitations are handled automatically by the runtime.").classes("text-xs text-slate-500")
            reconcile_status


@ui.page("/admin/status")
def status_page(request: Request):
    if not _require_auth():
        return
    _admin_chrome("status")
    callback_url = _origin(request) + "/webhooks/chatwoot/inbox"
    with legacy.db() as conn:
        links = int(conn.execute("SELECT COUNT(*) AS n FROM room_links").fetchone()["n"])

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 md:p-6 gap-5"):
        ui.label("Status & tests").classes("text-3xl font-bold")
        ui.label("Operational checks for the real runtime path.").classes("text-slate-600")
        with ui.card().classes("w-full p-6"):
            _status_row("Chatwoot API", bool(legacy.get_setting("chatwoot_verified_at")), legacy.get_setting("chatwoot_verified_at") or "Not tested")
            _status_row("API Inbox callback", bool(legacy.get_setting("api_inbox_callback_verified_at")), legacy.get_setting("api_inbox_callback_verified_at") or callback_url)
            _status_row("Signed API Inbox delivery", bool(legacy.get_setting("api_inbox_delivery_verified_at")), legacy.get_setting("api_inbox_delivery_verified_at") or "No signed callback received yet")
            _status_row("Meta auto-join", bool(legacy.get_setting("last_meta_auto_join_at") or legacy.get_setting("meta_invite_reconcile_at")), legacy.get_setting("last_meta_auto_join_at") or legacy.get_setting("meta_invite_reconcile_at") or "No Meta portal has needed auto-join since this version started")
            _status_row("Linked Chatwoot conversations", links > 0, str(links))
            mode = "PROXY" if legacy.get_setting("proxy_enabled") == "1" else "DIRECT"
            _status_row("Meta route", True, f"Saved mode: {mode}; last verified {legacy.get_setting('proxy_verified_at') or 'not tested'}")

        with ui.card().classes("w-full p-6 border border-emerald-100"):
            ui.label("Acceptance test").classes("text-xl font-semibold")
            ui.label("1. From a different Facebook account, send a brand-new Messenger message. 2. Do not open Element. 3. Confirm the Chatwoot conversation appears with the real name/avatar. 4. Reply from Chatwoot and confirm delivery to Meta without Error sending. 5. Repeat with Marketplace. 6. Delete the Chatwoot conversation and send another Meta message; it should recreate automatically.").classes("text-slate-600")
            ui.label("If Element still shows an invite visually but Chatwoot receives the conversation automatically, that is only stale client UI. If Chatwoot does not receive it, use Advanced → Reconcile pending Meta invites and inspect the status here.").classes("text-sm text-amber-700 mt-2")


async def _admin_entry_redirect(request: Request, call_next):
    if request.url.path == "/admin":
        return RedirectResponse("/admin/basic", status_code=303)
    if request.url.path == "/admin/operations":
        return RedirectResponse("/admin/advanced", status_code=303)
    return await call_next(request)


app.middleware("http")(_admin_entry_redirect)


def install() -> None:
    # runtime_enhancements installed its enhanced sync before this module loads.
    # Replace only the sync owner with the first-sync-safe implementation.
    legacy.sync_once = reliable_sync_once
