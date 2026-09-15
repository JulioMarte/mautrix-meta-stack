"""NiceGUI production surface for the single-client Matrix <-> Chatwoot integration."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from nicegui import app, ui

# CHATWOOT_WEBHOOK_SECRET used to protect a secret-in-path webhook URL. Keep an
# existing value only for migration compatibility. New installs use Chatwoot's
# webhook signing secret, stored in the admin database, and the canonical
# /webhooks/chatwoot endpoint.
LEGACY_WEBHOOK_PATH_SECRET = os.getenv("CHATWOOT_WEBHOOK_SECRET", "").strip()
if not LEGACY_WEBHOOK_PATH_SECRET:
    # Legacy modules still validate this variable at import time even though the
    # NiceGUI runtime no longer uses it for the canonical webhook endpoint.
    os.environ["CHATWOOT_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)

import final_app as runtime

legacy = runtime.legacy
prod = runtime.prod

COOKIE_SECURE = os.getenv("INTEGRATION_COOKIE_SECURE", "true").lower() == "true"
ALLOW_INSECURE_CHATWOOT = os.getenv("ALLOW_INSECURE_CHATWOOT", "false").lower() == "true"
IP_CHECK_URL = "https://api.ipify.org?format=json"
WEBHOOK_MAX_AGE_SECONDS = 300


async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.middleware("http")(_security_headers)


def authenticated() -> bool:
    return bool(app.storage.user.get("authenticated", False))


def migrate_proxy_env_once() -> None:
    """Import an old Coolify-managed proxy once, then make the admin authoritative."""
    if legacy.get_setting("proxy_ui_initialized") == "1":
        return
    env_proxy = (getattr(prod, "ENV_PROXY_URL", "") or "").strip()
    if env_proxy:
        prod.validate_proxy_url(env_proxy)
        legacy.set_setting("proxy_url", env_proxy)
        legacy.set_setting("proxy_enabled", "1" if getattr(prod, "ENV_PROXY_ENABLED", False) else "0")
    legacy.set_setting("proxy_ui_initialized", "1")


def effective_proxy():
    return False, legacy.get_setting("proxy_enabled") == "1", legacy.get_setting("proxy_url")


def proxy_resolver_payload() -> dict:
    """Return exactly what mautrix-meta receives from the authenticated proxy resolver."""
    _, enabled, proxy = effective_proxy()
    if not enabled:
        return {"proxy_url": ""}
    if not proxy:
        raise RuntimeError("Proxy is enabled but no proxy URL is configured")
    prod.validate_proxy_url(proxy)
    return {"proxy_url": proxy}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def setup_state() -> dict:
    _, proxy_enabled, proxy_value = effective_proxy()
    base = legacy.get_setting("chatwoot_base_url")
    account = legacy.get_setting("chatwoot_account_id")
    inbox = legacy.get_setting("chatwoot_inbox_id")
    token_saved = bool(legacy.get_setting("chatwoot_api_token"))
    webhook_secret_saved = bool(legacy.get_setting("chatwoot_webhook_signing_secret"))
    chatwoot_ready = bool(base and account and inbox and token_saved)
    proxy_ready = (not proxy_enabled) or bool(proxy_value)
    with legacy.db() as conn:
        link_count = conn.execute("SELECT COUNT(*) AS n FROM room_links").fetchone()["n"]
    return {
        "base": base,
        "account": account,
        "inbox": inbox,
        "token_saved": token_saved,
        "chatwoot_ready": chatwoot_ready,
        "chatwoot_verified_at": legacy.get_setting("chatwoot_verified_at"),
        "managed_proxy": False,
        "proxy_enabled": proxy_enabled,
        "proxy_value": proxy_value,
        "proxy_ready": proxy_ready,
        "proxy_verified_at": legacy.get_setting("proxy_verified_at"),
        "proxy_verified_ip": legacy.get_setting("proxy_verified_ip"),
        "proxy_verified_mode": legacy.get_setting("proxy_verified_mode"),
        "webhook_secret_saved": webhook_secret_saved,
        "webhook_registration_verified_at": legacy.get_setting("webhook_registration_verified_at"),
        "webhook_delivery_verified_at": legacy.get_setting("webhook_delivery_verified_at"),
        "link_count": int(link_count),
    }


def get_saved_secret(name: str) -> str:
    """Return a saved secret only to an already-authenticated admin callback/page."""
    if name in {"chatwoot_api_token", "proxy_url", "chatwoot_webhook_signing_secret"}:
        return legacy.get_setting(name)
    raise ValueError("Unknown saved secret")


def _clean_chatwoot_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    parsed = urlsplit(base)
    scheme_ok = parsed.scheme == "https" or (ALLOW_INSECURE_CHATWOOT and parsed.scheme == "http")
    if not scheme_ok or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Chatwoot URL must be a clean HTTPS base URL")
    return base


def save_configuration(base: str, account: str, inbox: str, token: str,
                       proxy_enabled: bool, proxy_url: str,
                       webhook_signing_secret: str = "") -> None:
    base = _clean_chatwoot_base(base)
    if not str(account).isdigit() or not str(inbox).isdigit():
        raise ValueError("Account ID and Inbox ID must be numeric")
    if not token.strip() and not legacy.get_setting("chatwoot_api_token"):
        raise ValueError("Chatwoot API token is required the first time you configure the integration")

    existing_proxy = legacy.get_setting("proxy_url")
    requested_proxy = (proxy_url or "").strip()
    if proxy_enabled:
        candidate_proxy = requested_proxy or existing_proxy
        if not candidate_proxy:
            raise ValueError("Proxy is enabled but no proxy URL is configured")
        prod.validate_proxy_url(candidate_proxy)

    old_chatwoot = (
        legacy.get_setting("chatwoot_base_url"),
        legacy.get_setting("chatwoot_account_id"),
        legacy.get_setting("chatwoot_inbox_id"),
        legacy.get_setting("chatwoot_api_token"),
    )
    new_token = token.strip() or old_chatwoot[3]
    new_chatwoot = (base, str(account), str(inbox), new_token)
    old_proxy = (legacy.get_setting("proxy_enabled") == "1", existing_proxy)
    new_proxy = (bool(proxy_enabled), requested_proxy or existing_proxy)

    legacy.set_setting("chatwoot_base_url", base)
    legacy.set_setting("chatwoot_account_id", str(account))
    legacy.set_setting("chatwoot_inbox_id", str(inbox))
    if token and token.strip():
        legacy.set_setting("chatwoot_api_token", token.strip())
    legacy.set_setting("proxy_enabled", "1" if proxy_enabled else "0")
    if requested_proxy:
        prod.validate_proxy_url(requested_proxy)
        legacy.set_setting("proxy_url", requested_proxy)
    if webhook_signing_secret and webhook_signing_secret.strip():
        old_webhook_secret = legacy.get_setting("chatwoot_webhook_signing_secret")
        new_webhook_secret = webhook_signing_secret.strip()
        legacy.set_setting("chatwoot_webhook_signing_secret", new_webhook_secret)
        if new_webhook_secret != old_webhook_secret:
            legacy.set_setting("webhook_delivery_verified_at", "")

    if new_chatwoot != old_chatwoot:
        legacy.set_setting("chatwoot_verified_at", "")
        legacy.set_setting("webhook_registration_verified_at", "")
    if new_proxy != old_proxy:
        legacy.set_setting("proxy_verified_at", "")
        legacy.set_setting("proxy_verified_ip", "")
        legacy.set_setting("proxy_verified_mode", "")
    runtime.ensure_activation_boundary()


def discover_chatwoot(base: str, token: str = "") -> dict:
    """Validate a Chatwoot token and discover account/inbox numeric IDs."""
    base = _clean_chatwoot_base(base)
    token = (token or "").strip() or legacy.get_setting("chatwoot_api_token")
    if not token:
        raise ValueError("Paste the Chatwoot Personal Access Token first")
    session = requests.Session()
    session.trust_env = False
    headers = {"api_access_token": token, "Accept": "application/json"}
    profile = session.get(f"{base}/api/v1/profile", headers=headers, timeout=20)
    profile.raise_for_status()
    profile_data = profile.json()
    account_id = profile_data.get("account_id")
    accounts = profile_data.get("accounts") or []
    if not account_id and accounts:
        account_id = accounts[0].get("id")
    if not account_id:
        raise RuntimeError("Chatwoot profile did not return an account ID")
    inbox_response = session.get(
        f"{base}/api/v1/accounts/{int(account_id)}/inboxes", headers=headers, timeout=20
    )
    inbox_response.raise_for_status()
    payload = inbox_response.json()
    inboxes = payload.get("payload") if isinstance(payload, dict) else payload
    if not isinstance(inboxes, list):
        raise RuntimeError("Chatwoot did not return an inbox list")
    return {
        "account_id": int(account_id),
        "inboxes": [
            {"id": int(item["id"]), "name": str(item.get("name") or f"Inbox {item['id']}")}
            for item in inboxes if isinstance(item, dict) and item.get("id") is not None
        ],
    }


def verify_chatwoot() -> str:
    if not legacy.configured():
        raise RuntimeError("Chatwoot is not configured")
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = str(legacy.get_setting("chatwoot_inbox_id"))
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/inboxes")
    inboxes = data.get("payload") if isinstance(data, dict) else data
    if not isinstance(inboxes, list) or not any(
        isinstance(item, dict) and str(item.get("id")) == inbox_id for item in inboxes
    ):
        raise RuntimeError(
            "Configured Chatwoot inbox was not found. Use the numeric Inbox ID returned by Chatwoot, "
            "not the Inbox Identifier token shown in the inbox Configuration tab."
        )
    checked_at = _now_utc()
    legacy.set_setting("chatwoot_verified_at", checked_at)
    return f"PASS — authenticated to Chatwoot; account {account_id}; inbox {inbox_id}; {checked_at}"


def _extract_webhook_records(data) -> list[dict]:
    """Normalize webhook-list responses across Chatwoot API response shapes."""
    if isinstance(data, list):
        records = [item for item in data if isinstance(item, dict)]
        if data and not records:
            raise RuntimeError("Chatwoot returned a webhook list without webhook objects")
        return records

    if not isinstance(data, dict):
        raise RuntimeError(f"Chatwoot returned an unsupported webhook response ({type(data).__name__})")

    if "url" in data or "subscriptions" in data:
        return [data]

    # Current self-hosted Chatwoot renders {"payload": {"webhooks": [...]}}.
    # Older/documented variants may return a top-level list, payload list, or
    # top-level webhooks list, so keep the parser deliberately tolerant.
    for key in ("payload", "webhooks", "data"):
        if key in data and data[key] is not None:
            try:
                return _extract_webhook_records(data[key])
            except RuntimeError:
                continue

    values = [value for value in data.values() if isinstance(value, dict)]
    if values and len(values) == len(data):
        return values

    raise RuntimeError(
        "Chatwoot returned webhook data in an unexpected format. "
        "The integration did not change any settings; update Chatwoot or retry after updating this integration."
    )


def _webhook_subscriptions(value) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        result = set()
        for item in value:
            if isinstance(item, str):
                result.add(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("event") or item.get("id")
                if name:
                    result.add(str(name))
        return result
    if isinstance(value, dict):
        return {str(key) for key, enabled in value.items() if enabled}
    return set()


def verify_webhook_registration(expected_url: str) -> str:
    if not legacy.configured():
        raise RuntimeError("Save and test Chatwoot first")
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/webhooks")
    hooks = _extract_webhook_records(data)
    expected = expected_url.rstrip("/")
    for hook in hooks:
        if str(hook.get("url") or "").rstrip("/") != expected:
            continue
        subscriptions = _webhook_subscriptions(hook.get("subscriptions"))
        if "message_created" not in subscriptions:
            raise RuntimeError("Webhook URL exists in Chatwoot but message_created is not selected")

        remote_secret = str(hook.get("secret") or "").strip()
        stored_secret = legacy.get_setting("chatwoot_webhook_signing_secret")
        secret_imported = False
        if remote_secret and remote_secret != stored_secret:
            legacy.set_setting("chatwoot_webhook_signing_secret", remote_secret)
            legacy.set_setting("webhook_delivery_verified_at", "")
            secret_imported = True

        checked_at = _now_utc()
        legacy.set_setting("webhook_registration_verified_at", checked_at)
        if remote_secret:
            secret_note = "signing secret imported automatically" if secret_imported else "signing secret confirmed"
        elif stored_secret:
            secret_note = "saved signing secret retained"
        else:
            secret_note = "registration verified, but this Chatwoot response did not expose the signing secret"
        return f"PASS — webhook URL + message_created verified; {secret_note}; {checked_at}"
    raise RuntimeError("Webhook URL was not found in Chatwoot. Add the exact URL shown below, then retry")


def verify_chatwoot_signature(raw_body: bytes, signature: str, timestamp: str,
                              now: int | None = None) -> bool:
    secret = legacy.get_setting("chatwoot_webhook_signing_secret")
    if not secret:
        raise RuntimeError("Chatwoot webhook signing secret is not configured")
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid Chatwoot webhook timestamp") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp_int) > WEBHOOK_MAX_AGE_SECONDS:
        raise RuntimeError("Chatwoot webhook timestamp is too old or too far in the future")
    signed = str(timestamp).encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest
    if not signature or not hmac.compare_digest(signature, expected):
        raise RuntimeError("Invalid Chatwoot webhook signature")
    return True


def _public_ip(session: requests.Session, proxies=None) -> str:
    response = session.get(IP_CHECK_URL, proxies=proxies, timeout=20)
    response.raise_for_status()
    observed = str(response.json().get("ip", "")).strip()
    try:
        return str(ipaddress.ip_address(observed))
    except ValueError as exc:
        raise RuntimeError("IP check service returned an invalid public IP") from exc


def test_proxy_url(proxy: str) -> dict:
    """Low-level helper for validating a proxy URL without changing active routing."""
    proxy = (proxy or "").strip()
    if not proxy:
        raise ValueError("Enter a proxy URL first")
    prod.validate_proxy_url(proxy)
    direct_session = requests.Session()
    direct_session.trust_env = False
    direct_ip = _public_ip(direct_session)
    proxy_session = requests.Session()
    proxy_session.trust_env = False
    proxy_ip = _public_ip(proxy_session, proxies={"http": proxy, "https": proxy})
    return {
        "mode": "PROXY",
        "direct_ip": direct_ip,
        "route_ip": proxy_ip,
        "proxy_ip": proxy_ip,
        "different": direct_ip != proxy_ip,
        "checked_at": _now_utc(),
    }


def verify_meta_route() -> dict:
    """Test the exact route currently returned to mautrix-meta by /internal/proxy."""
    resolver = proxy_resolver_payload()
    proxy = str(resolver.get("proxy_url") or "").strip()

    direct_session = requests.Session()
    direct_session.trust_env = False
    direct_ip = _public_ip(direct_session)
    checked_at = _now_utc()

    if not proxy:
        result = {
            "mode": "DIRECT",
            "direct_ip": direct_ip,
            "route_ip": direct_ip,
            "proxy_ip": "",
            "different": False,
            "checked_at": checked_at,
        }
        legacy.set_setting("proxy_verified_at", checked_at)
        legacy.set_setting("proxy_verified_ip", "")
        legacy.set_setting("proxy_verified_mode", "DIRECT")
        return result

    proxy_session = requests.Session()
    proxy_session.trust_env = False
    route_ip = _public_ip(proxy_session, proxies={"http": proxy, "https": proxy})
    result = {
        "mode": "PROXY",
        "direct_ip": direct_ip,
        "route_ip": route_ip,
        "proxy_ip": route_ip,
        "different": direct_ip != route_ip,
        "checked_at": checked_at,
    }
    if result["different"]:
        legacy.set_setting("proxy_verified_at", checked_at)
        legacy.set_setting("proxy_verified_ip", route_ip)
        legacy.set_setting("proxy_verified_mode", "PROXY")
    return result


def verify_proxy() -> dict:
    """Backward-compatible proxy-only verification used by existing tests/callers."""
    _, enabled, proxy = effective_proxy()
    if not enabled or not proxy:
        raise RuntimeError("Proxy is not configured")
    result = test_proxy_url(proxy)
    if result["different"]:
        legacy.set_setting("proxy_verified_at", result["checked_at"])
        legacy.set_setting("proxy_verified_ip", result["proxy_ip"])
        legacy.set_setting("proxy_verified_mode", "PROXY")
    return result


def _webhook_origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",", 1)[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",", 1)[0].strip()
    return f"{proto}://{host}".rstrip("/")


def _decode_webhook_payload(raw_body: bytes):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    return payload


async def _handle_chatwoot_payload(payload: dict):
    if payload.get("event") != "message_created":
        return {"ok": True, "ignored": True}
    if payload.get("message_type") not in ("outgoing", 1) or payload.get("private") is True:
        return {"ok": True, "ignored": True}
    conversation = payload.get("conversation") or {}
    conversation_id = conversation.get("id") or payload.get("conversation_id")
    content = str(payload.get("content") or "").strip()
    message_id = str(payload.get("id") or "")
    if not conversation_id or not content:
        return {"ok": True, "ignored": True}
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    event_key = "chatwoot:" + message_id if message_id else ""
    if event_key and legacy.event_seen(event_key):
        return {"ok": True, "duplicate": True}
    with legacy.db() as conn:
        link = conn.execute("SELECT * FROM room_links WHERE conversation_id = ?", (conversation_id,)).fetchone()
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped conversation"}
    await asyncio.to_thread(
        legacy.send_matrix_message, link["room_id"], content,
        "cw-" + (message_id or legacy.uuid.uuid4().hex),
    )
    if event_key:
        legacy.mark_event(event_key, "chatwoot_to_matrix")
    return {"ok": True}


@app.get("/")
async def root_redirect():
    return RedirectResponse("/admin", status_code=303)


@app.get("/health")
async def health():
    state = setup_state()
    return {
        "ok": True,
        "configured": state["chatwoot_ready"],
        "proxy_enabled": state["proxy_enabled"],
        "proxy_ready": state["proxy_ready"],
        "proxy_mode": "PROXY" if state["proxy_enabled"] else "DIRECT",
        "admin_ui": "nicegui",
    }


@app.get("/internal/proxy")
async def internal_proxy(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return JSONResponse({"detail": "not found"}, status_code=404)
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not (hmac.compare_digest(username, "mautrix") and hmac.compare_digest(password, prod.PROXY_RESOLVER_SECRET)):
        return JSONResponse({"detail": "not found"}, status_code=404)
    try:
        return proxy_resolver_payload()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request):
    raw_body = await request.body()
    try:
        verify_chatwoot_signature(
            raw_body,
            request.headers.get("x-chatwoot-signature", ""),
            request.headers.get("x-chatwoot-timestamp", ""),
        )
        payload = _decode_webhook_payload(raw_body)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    legacy.set_setting("webhook_delivery_verified_at", _now_utc())
    delivery = request.headers.get("x-chatwoot-delivery", "").strip()
    if delivery:
        legacy.set_setting("webhook_last_delivery_id", delivery[:200])
    return await _handle_chatwoot_payload(payload)


@app.post("/webhooks/chatwoot/{secret}")
async def legacy_chatwoot_webhook(secret: str, request: Request):
    """Temporary backward-compatible path while an existing Chatwoot webhook is migrated."""
    if not LEGACY_WEBHOOK_PATH_SECRET or not hmac.compare_digest(secret, LEGACY_WEBHOOK_PATH_SECRET):
        return JSONResponse({"detail": "not found"}, status_code=404)
    raw_body = await request.body()
    try:
        payload = _decode_webhook_payload(raw_body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return await _handle_chatwoot_payload(payload)


def page_shell(title: str):
    ui.page_title(title)
    ui.colors(primary="#2563eb", secondary="#475569", accent="#0f766e", positive="#15803d", negative="#b91c1c")
    ui.query("body").classes("bg-slate-50 text-slate-900")


def status_badge(label: str, ok: bool):
    ui.badge(label, color="positive" if ok else "warning").props("outline" if not ok else "")


def _check_row(label: str, ok: bool, detail: str):
    with ui.row().classes("items-start gap-3"):
        ui.icon("check_circle" if ok else "radio_button_unchecked").classes("text-green-700" if ok else "text-slate-400")
        with ui.column().classes("gap-0"):
            ui.label(label).classes("font-medium")
            ui.label(detail).classes("text-xs text-slate-500")


@ui.page("/admin/login")
def login_page():
    page_shell("Integration Admin · Login")
    if authenticated():
        ui.navigate.to("/admin")
        return
    with ui.column().classes("w-full min-h-screen items-center justify-center p-6"):
        with ui.card().classes("w-full max-w-md p-7 shadow-xl rounded-xl"):
            with ui.row().classes("items-center gap-3 mb-1"):
                ui.icon("hub", size="lg").classes("text-blue-600")
                ui.label("Integration Admin").classes("text-2xl font-semibold")
            ui.label("Configure and test the complete Chatwoot ↔ Matrix ↔ Meta connection here.").classes("text-slate-500 mb-5")
            password = ui.input("Admin password", password=True, password_toggle_button=True).props("outlined autocomplete=current-password").classes("w-full")
            status = ui.label("").classes("text-red-700 min-h-6")

            async def do_login():
                if hmac.compare_digest(password.value or "", legacy.ADMIN_PASSWORD):
                    app.storage.user["authenticated"] = True
                    ui.navigate.to("/admin")
                    return
                status.text = "Invalid password"
                await asyncio.sleep(0.35)

            password.on("keydown.enter", do_login)
            ui.button("Sign in", on_click=do_login, icon="login").classes("w-full mt-2")


@ui.page("/admin")
def admin_page(request: Request):
    page_shell("Integration Admin")
    if not authenticated():
        ui.navigate.to("/admin/login")
        return

    state = setup_state()
    webhook_url = _webhook_origin(request) + "/webhooks/chatwoot"

    with ui.header().classes("items-center justify-between bg-white text-slate-900 border-b border-slate-200"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("hub", size="md").classes("text-blue-600")
            with ui.column().classes("gap-0"):
                ui.label("Matrix ↔ Chatwoot").classes("text-xl font-semibold")
                ui.label("Single-client integration").classes("text-xs text-slate-500")
        async def logout():
            app.storage.user.clear()
            ui.navigate.to("/admin/login")
        ui.button("Logout", on_click=logout, icon="logout").props("flat")

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 md:p-6 gap-5"):
        with ui.card().classes("w-full p-5 bg-blue-50 border border-blue-100"):
            ui.label("Setup guide").classes("text-lg font-semibold")
            ui.label("Connection credentials, webhook signing secret, proxy settings and tests live in this panel. Coolify is only for infrastructure/bootstrap secrets.").classes("text-slate-600")
            if not state["chatwoot_ready"]:
                next_action = "Next: enter Chatwoot URL + Personal API token, detect the account/inbox, then Save."
            elif not state["chatwoot_verified_at"]:
                next_action = "Next: run Test Chatwoot below."
            elif state["proxy_enabled"] and not state["proxy_verified_at"]:
                next_action = "Next: save the Meta route, then test the current runtime route and confirm its egress IP."
            elif not state["webhook_registration_verified_at"]:
                next_action = "Next: create the webhook in Chatwoot, then run Verify webhook & import secret below."
            elif not state["webhook_secret_saved"]:
                next_action = "Next: this Chatwoot version did not expose the signing secret through the API; paste it in Step 3 and Save."
            elif not state["webhook_delivery_verified_at"]:
                next_action = "Next: send a real message/reply so Chatwoot delivers one signed webhook."
            else:
                next_action = "Configuration checks are complete. Run the live Meta ↔ Chatwoot end-to-end test."
            ui.label(next_action).classes("mt-2 font-semibold text-blue-900")
            with ui.column().classes("gap-2 mt-3"):
                _check_row("Chatwoot saved", state["chatwoot_ready"], "URL, token, account and inbox are stored in the private integration volume.")
                _check_row("Chatwoot API tested", bool(state["chatwoot_verified_at"]), state["chatwoot_verified_at"] or "Not tested yet")
                _check_row("Meta route", (not state["proxy_enabled"]) or bool(state["proxy_verified_at"]), ("DIRECT — no proxy returned to mautrix-meta" if not state["proxy_enabled"] else (state["proxy_verified_at"] or "PROXY enabled but current route not tested yet")))
                _check_row("Webhook registered", bool(state["webhook_registration_verified_at"]), state["webhook_registration_verified_at"] or "Registration still needs verification")
                _check_row("Webhook signing secret", state["webhook_secret_saved"], "Stored in the integration volume" if state["webhook_secret_saved"] else "Not stored yet")
                _check_row("Signed webhook received", bool(state["webhook_delivery_verified_at"]), state["webhook_delivery_verified_at"] or "No verified Chatwoot delivery yet")

        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Chatwoot").classes("text-sm text-slate-500")
                ui.label("Ready" if state["chatwoot_ready"] else "Needs configuration").classes(
                    "text-xl font-semibold " + ("text-green-700" if state["chatwoot_ready"] else "text-amber-700")
                )
                status_badge("API token stored", state["token_saved"])
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Meta runtime route").classes("text-sm text-slate-500")
                if state["proxy_enabled"]:
                    ui.label("PROXY").classes("text-xl font-semibold text-green-700")
                    ui.label(legacy.redact_proxy(state["proxy_value"])).classes("text-sm text-slate-600")
                    status_badge("Resolver returns proxy", state["proxy_ready"])
                else:
                    ui.label("DIRECT").classes("text-xl font-semibold text-blue-700")
                    ui.label("Stored proxy credentials are retained but inactive.").classes("text-sm text-slate-500")
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Linked conversations").classes("text-sm text-slate-500")
                ui.label(str(state["link_count"])).classes("text-2xl font-semibold")

        with ui.card().classes("w-full p-6"):
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.avatar("1", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Connect Chatwoot").classes("text-xl font-semibold")
                    ui.label("Everything for the Chatwoot API connection is entered and stored here.").classes("text-slate-500")

            base = ui.input("Chatwoot URL", value=state["base"], placeholder="https://chatwoot.example.com").props("outlined").classes("w-full")
            ui.label("Use only the base URL, for example https://chatwoot.example.com — no /app path.").classes("text-xs text-slate-500 -mt-2")

            token = ui.input(
                "Personal API token",
                value=get_saved_secret("chatwoot_api_token"),
                password=True,
                password_toggle_button=True,
                placeholder="Paste Personal Access Token",
            ).props("outlined autocomplete=off").classes("w-full")
            ui.label("Chatwoot: avatar → Profile Settings → Personal Access Token. The eye icon reveals the value already stored in this panel.").classes("text-xs text-slate-500 -mt-2")

            with ui.row().classes("w-full gap-4 flex-wrap"):
                account = ui.input("Account ID (numeric)", value=state["account"], placeholder="1").props("outlined").classes("grow min-w-48")
                inbox = ui.input("Inbox ID (numeric)", value=state["inbox"], placeholder="2").props("outlined").classes("grow min-w-48")
            ui.label("The Inbox Identifier token in Chatwoot is NOT the Inbox ID. The Inbox ID is a number.").classes("text-sm text-amber-700")
            detected_inboxes = ui.select(options={}, label="Detected inboxes", with_input=True).props("outlined clearable").classes("w-full")

            def choose_inbox(event):
                if event.value is not None:
                    inbox.value = str(event.value)
            detected_inboxes.on_value_change(choose_inbox)

            async def detect_ids():
                try:
                    result = await asyncio.to_thread(discover_chatwoot, base.value or "", token.value or "")
                    account.value = str(result["account_id"])
                    options = {item["id"]: f"{item['name']} · ID {item['id']}" for item in result["inboxes"]}
                    detected_inboxes.options = options
                    detected_inboxes.update()
                    if len(options) == 1:
                        only_id = next(iter(options))
                        detected_inboxes.value = only_id
                        inbox.value = str(only_id)
                        ui.notify("Account and inbox detected", type="positive")
                    else:
                        ui.notify(f"Account detected. Choose one of {len(options)} inboxes.", type="positive")
                except Exception as exc:
                    ui.notify(f"Could not detect Chatwoot IDs: {exc}", type="negative", close_button=True)

            ui.button("Detect account and inboxes", on_click=detect_ids, icon="travel_explore").classes("mt-2")

            ui.separator().classes("my-5")
            with ui.row().classes("items-center gap-3 mb-2"):
                ui.avatar("2", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Meta route · direct or proxy").classes("text-xl font-semibold")
                    ui.label("This switch controls the resolver that mautrix-meta reads when it connects. Matrix and Chatwoot always stay direct.").classes("text-slate-500")

            proxy_switch = ui.switch("Route Meta through the saved proxy", value=state["proxy_enabled"])
            proxy_input = ui.input(
                "Proxy URL",
                value=get_saved_secret("proxy_url"),
                password=True,
                password_toggle_button=True,
                placeholder="http://user:password@host:8888",
            ).props("outlined autocomplete=off").classes("w-full")
            ui.label("The proxy URL may remain stored while DIRECT is selected. Stored does not mean active. Save after changing the switch or URL.").classes("text-xs text-slate-500")

            with ui.card().classes("w-full p-4 mt-3 bg-slate-50 border border-slate-200"):
                route_mode_label = ui.label(f"SAVED META ROUTE: {'PROXY' if state['proxy_enabled'] else 'DIRECT'}").classes("text-sm font-semibold text-slate-700")
                with ui.row().classes("w-full gap-4 flex-wrap"):
                    with ui.column().classes("grow min-w-56 gap-1"):
                        ui.label("Direct VPS public IP").classes("text-xs uppercase tracking-wide text-slate-500")
                        direct_ip_label = ui.label("Not tested yet").classes("text-lg font-mono font-semibold")
                    with ui.column().classes("grow min-w-56 gap-1"):
                        ui.label("Observed Meta route IP").classes("text-xs uppercase tracking-wide text-slate-500")
                        initial_route_ip = state["proxy_verified_ip"] if state["proxy_enabled"] else "Not tested yet"
                        route_ip_label = ui.label(initial_route_ip or "Not tested yet").classes("text-lg font-mono font-semibold")
                proxy_status = ui.label(
                    f"Last runtime-route check: {state['proxy_verified_at']} ({state['proxy_verified_mode'] or 'unknown mode'})" if state["proxy_verified_at"] else "Save the route, then test exactly what mautrix-meta will receive from the resolver."
                ).classes("text-sm text-slate-600 mt-2")
                checked_at_label = ui.label("").classes("text-xs text-slate-400")

            async def test_meta_route_now():
                try:
                    saved_enabled = effective_proxy()[1]
                    saved_proxy = effective_proxy()[2]
                    candidate_enabled = bool(proxy_switch.value)
                    candidate_proxy = (proxy_input.value or "").strip()
                    if candidate_enabled != saved_enabled or (candidate_enabled and candidate_proxy != saved_proxy):
                        proxy_status.text = "SAVE REQUIRED — the controls contain unsaved route changes. Save first; this test never tests an unsaved proxy value."
                        proxy_status.classes(replace="text-sm text-amber-700 mt-2 font-medium")
                        ui.notify("Save the Meta route before testing it", type="warning", close_button=True)
                        return

                    result = await asyncio.to_thread(verify_meta_route)
                    direct_ip_label.text = result["direct_ip"]
                    route_ip_label.text = result["route_ip"]
                    route_mode_label.text = f"SAVED META ROUTE: {result['mode']}"
                    checked_at_label.text = f"Checked: {result['checked_at']}"
                    if result["mode"] == "DIRECT":
                        proxy_status.text = "PASS — resolver returns DIRECT. Meta will connect without a proxy on its next connection/reconnection."
                        proxy_status.classes(replace="text-sm text-green-700 mt-2 font-medium")
                        ui.notify(f"Current Meta route is DIRECT: {result['route_ip']}", type="positive")
                    elif result["different"]:
                        proxy_status.text = "PASS — resolver returns PROXY and the observed Meta route IP differs from the VPS IP."
                        proxy_status.classes(replace="text-sm text-green-700 mt-2 font-medium")
                        ui.notify(f"Current Meta route is PROXY: {result['route_ip']}", type="positive")
                    else:
                        proxy_status.text = "FAIL — resolver returns PROXY, but the observed route IP equals the VPS IP. Do not rely on this proxy."
                        proxy_status.classes(replace="text-sm text-red-700 mt-2 font-medium")
                        ui.notify("Proxy route did not change the public IP", type="warning", close_button=True)
                except Exception as exc:
                    proxy_status.text = f"FAIL — {exc}"
                    proxy_status.classes(replace="text-sm text-red-700 mt-2 font-medium")
                    ui.notify(f"Meta route test failed: {exc}", type="negative", close_button=True)

            ui.button("Test current Meta route", on_click=test_meta_route_now, icon="route").props("outline").classes("mt-2")
            ui.label("This checks the same resolver state mautrix-meta uses. It does not test a typed-but-unsaved URL. Existing Meta sockets may need to reconnect before a changed route affects that already-open connection.").classes("text-xs text-slate-500 mt-1")

            ui.separator().classes("my-5")
            with ui.row().classes("items-center gap-3 mb-2"):
                ui.avatar("3", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Webhook signing secret · normally automatic").classes("text-xl font-semibold")
                    ui.label("The webhook verification step below imports this secret automatically when your Chatwoot version exposes it through the API.").classes("text-slate-500")
            webhook_secret_input = ui.input(
                "Webhook signing secret",
                value=get_saved_secret("chatwoot_webhook_signing_secret"),
                password=True,
                password_toggle_button=True,
                placeholder="Usually detected automatically; paste only if your Chatwoot does not expose it",
            ).props("outlined autocomplete=off").classes("w-full")

            async def save():
                try:
                    await asyncio.to_thread(
                        save_configuration,
                        base.value or "", account.value or "", inbox.value or "", token.value or "",
                        bool(proxy_switch.value), proxy_input.value or "", webhook_secret_input.value or "",
                    )
                    route_mode_label.text = f"SAVED META ROUTE: {'PROXY' if bool(proxy_switch.value) else 'DIRECT'}"
                    proxy_status.text = "Saved. Run Test current Meta route to verify the effective resolver path."
                    proxy_status.classes(replace="text-sm text-slate-600 mt-2")
                    ui.notify("Saved. Connection secrets and Meta route are now stored in this admin's private volume.", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative", close_button=True)

            ui.button("Save all connection settings", on_click=save, icon="save").classes("mt-5")

        with ui.card().classes("w-full p-6"):
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.avatar("4", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Test Chatwoot API").classes("text-xl font-semibold")
                    ui.label("This proves the saved token can access the configured account and inbox.").classes("text-slate-500")
            chatwoot_status = ui.label(
                f"PASS — last verified {state['chatwoot_verified_at']}" if state["chatwoot_verified_at"] else "Not tested yet."
            ).classes("text-sm text-slate-600")

            async def test_chatwoot():
                try:
                    message = await asyncio.to_thread(verify_chatwoot)
                    chatwoot_status.text = message
                    chatwoot_status.classes(replace="text-sm text-green-700 font-medium")
                    ui.notify("Chatwoot connection verified", type="positive")
                except Exception as exc:
                    chatwoot_status.text = f"FAIL — {exc}"
                    chatwoot_status.classes(replace="text-sm text-red-700 font-medium")
                    ui.notify(f"Chatwoot test failed: {exc}", type="negative", close_button=True)

            ui.button("Test Chatwoot", on_click=test_chatwoot, icon="dns").classes("mt-2")

        with ui.card().classes("w-full p-6"):
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.avatar("5", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Configure and verify the Chatwoot webhook").classes("text-xl font-semibold")
                    ui.label("No secret belongs in the URL. This panel verifies the registration and imports the signing secret when Chatwoot exposes it.").classes("text-slate-500")

            ui.label("In Chatwoot: Settings → Integrations → Webhooks → Add new webhook. Paste this exact URL and select only message_created.").classes("text-slate-700")
            with ui.row().classes("w-full items-center gap-2"):
                webhook_url_input = ui.input("Webhook URL", value=webhook_url).props("outlined readonly").classes("grow")
                async def copy_webhook_url():
                    await ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(webhook_url)})")
                    ui.notify("Webhook URL copied", type="positive")
                ui.button(icon="content_copy", on_click=copy_webhook_url).props("flat round")
            ui.label("After creating the webhook in Chatwoot, click the button below. The panel checks the exact URL and message_created subscription and stores the webhook signing secret automatically when available.").classes("text-sm text-slate-600")

            registration_status = ui.label(
                f"PASS — registration verified {state['webhook_registration_verified_at']}" if state["webhook_registration_verified_at"] else "Registration has not been verified yet."
            ).classes("text-sm text-slate-600 mt-3")
            delivery_status = ui.label(
                f"PASS — signed delivery received {state['webhook_delivery_verified_at']}" if state["webhook_delivery_verified_at"] else "No signed Chatwoot delivery has been received yet."
            ).classes("text-sm text-slate-600")

            async def check_webhook_registration():
                try:
                    message = await asyncio.to_thread(verify_webhook_registration, webhook_url)
                    registration_status.text = message
                    registration_status.classes(replace="text-sm text-green-700 font-medium mt-3")
                    ui.notify("Webhook registration verified", type="positive")
                except Exception as exc:
                    registration_status.text = f"FAIL — {exc}"
                    registration_status.classes(replace="text-sm text-red-700 font-medium mt-3")
                    ui.notify(f"Webhook check failed: {exc}", type="negative", close_button=True)

            ui.button("Verify webhook & import secret", on_click=check_webhook_registration, icon="verified").classes("mt-3")
            ui.label("The final signed-delivery check turns green automatically after Chatwoot sends a real signed webhook to this server. Refresh this page after the first reply/message event.").classes("text-xs text-slate-500 mt-2")

        with ui.card().classes("w-full p-6 border border-emerald-100"):
            with ui.row().classes("items-center gap-3 mb-2"):
                ui.avatar("6", color="positive", text_color="white")
                ui.label("Run the live end-to-end check").classes("text-xl font-semibold")
            ui.label("1. Log into Meta through mautrix-meta. 2. Send one fresh customer message from Messenger. 3. Confirm it appears once in Chatwoot. 4. Reply once from Chatwoot and confirm it arrives once in Meta.").classes("text-slate-600")
            ui.label("Linked conversations should move above 0 after the first real bridged conversation. CI cannot prove the Meta login or real provider path for you.").classes("text-sm text-amber-700 mt-2")
            ui.button("Refresh setup status", on_click=lambda: ui.navigate.to("/admin"), icon="refresh").props("outline").classes("mt-3")


def run() -> None:
    legacy.init_db()
    migrate_proxy_env_once()
    ui.run(
        host="0.0.0.0", port=8080, title="Integration Admin", storage_secret=legacy.SESSION_SECRET,
        session_middleware_kwargs={"same_site": "strict", "https_only": COOKIE_SECURE, "max_age": 8 * 60 * 60},
        reload=False, show=False, uvicorn_logging_level="warning", access_log=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
