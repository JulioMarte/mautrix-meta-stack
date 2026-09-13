"""NiceGUI production surface for the single-client Matrix <-> Chatwoot integration."""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from nicegui import app, ui

import final_app as runtime

legacy = runtime.legacy
prod = runtime.prod

COOKIE_SECURE = os.getenv("INTEGRATION_COOKIE_SECURE", "true").lower() == "true"
ALLOW_INSECURE_CHATWOOT = os.getenv("ALLOW_INSECURE_CHATWOOT", "false").lower() == "true"
IP_CHECK_URL = "https://api.ipify.org?format=json"


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


def setup_state() -> dict:
    _, proxy_enabled, proxy_value = effective_proxy()
    base = legacy.get_setting("chatwoot_base_url")
    account = legacy.get_setting("chatwoot_account_id")
    inbox = legacy.get_setting("chatwoot_inbox_id")
    token_saved = bool(legacy.get_setting("chatwoot_api_token"))
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
        "managed_proxy": False,
        "proxy_enabled": proxy_enabled,
        "proxy_value": proxy_value,
        "proxy_ready": proxy_ready,
        "link_count": int(link_count),
    }


def get_saved_secret(name: str) -> str:
    """Return a saved secret only to the already-authenticated admin UI callback."""
    if name == "chatwoot_api_token":
        return legacy.get_setting("chatwoot_api_token")
    if name == "proxy_url":
        return legacy.get_setting("proxy_url")
    raise ValueError("Unknown saved secret")


def _clean_chatwoot_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    parsed = urlsplit(base)
    scheme_ok = parsed.scheme == "https" or (ALLOW_INSECURE_CHATWOOT and parsed.scheme == "http")
    if not scheme_ok or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Chatwoot URL must be a clean HTTPS base URL")
    return base


def save_configuration(base: str, account: str, inbox: str, token: str,
                       proxy_enabled: bool, proxy_url: str) -> None:
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

    legacy.set_setting("chatwoot_base_url", base)
    legacy.set_setting("chatwoot_account_id", str(account))
    legacy.set_setting("chatwoot_inbox_id", str(inbox))
    if token and token.strip():
        legacy.set_setting("chatwoot_api_token", token.strip())
    legacy.set_setting("proxy_enabled", "1" if proxy_enabled else "0")
    if requested_proxy:
        prod.validate_proxy_url(requested_proxy)
        legacy.set_setting("proxy_url", requested_proxy)
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
            for item in inboxes if item.get("id") is not None
        ],
    }


def verify_chatwoot() -> str:
    if not legacy.configured():
        raise RuntimeError("Chatwoot is not configured")
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = str(legacy.get_setting("chatwoot_inbox_id"))
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/inboxes")
    inboxes = data.get("payload") if isinstance(data, dict) else data
    if not isinstance(inboxes, list) or not any(str(item.get("id")) == inbox_id for item in inboxes):
        raise RuntimeError(
            "Configured Chatwoot inbox was not found. Use the numeric Inbox ID returned by Chatwoot, "
            "not the Inbox Identifier token shown in the inbox Configuration tab."
        )
    return "Chatwoot connection verified"


def _public_ip(session: requests.Session, proxies=None) -> str:
    response = session.get(IP_CHECK_URL, proxies=proxies, timeout=20)
    response.raise_for_status()
    observed = str(response.json().get("ip", "")).strip()
    try:
        return str(ipaddress.ip_address(observed))
    except ValueError as exc:
        raise RuntimeError("IP check service returned an invalid public IP") from exc


def test_proxy_url(proxy: str) -> dict:
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
        "direct_ip": direct_ip,
        "proxy_ip": proxy_ip,
        "different": direct_ip != proxy_ip,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def verify_proxy() -> dict:
    _, enabled, proxy = effective_proxy()
    if not enabled or not proxy:
        raise RuntimeError("Proxy is not configured")
    return test_proxy_url(proxy)


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
    _, enabled, proxy = effective_proxy()
    if not enabled:
        return {"proxy_url": ""}
    if not proxy:
        return JSONResponse({"error": "proxy enabled but not configured"}, status_code=503)
    return {"proxy_url": proxy}


@app.post("/webhooks/chatwoot/{secret}")
async def chatwoot_webhook(secret: str, request: Request):
    if not hmac.compare_digest(secret, legacy.WEBHOOK_SECRET):
        return JSONResponse({"detail": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
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


def page_shell(title: str):
    ui.page_title(title)
    ui.colors(primary="#2563eb", secondary="#475569", accent="#0f766e", positive="#15803d", negative="#b91c1c")
    ui.query("body").classes("bg-slate-50 text-slate-900")


def status_badge(label: str, ok: bool):
    ui.badge(label, color="positive" if ok else "warning").props("outline" if not ok else "")


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
            ui.label("Configure Chatwoot and Meta connectivity for this client instance.").classes("text-slate-500 mb-5")
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
def admin_page():
    page_shell("Integration Admin")
    if not authenticated():
        ui.navigate.to("/admin/login")
        return

    state = setup_state()
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
        completed = sum([state["chatwoot_ready"], state["proxy_ready"], state["link_count"] > 0])
        with ui.card().classes("w-full p-5 bg-blue-50 border border-blue-100"):
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("Setup overview").classes("text-lg font-semibold")
                    ui.label("Configure Chatwoot and the optional Meta proxy here; Coolify only keeps infrastructure secrets.").classes("text-slate-600")
                ui.badge(f"{completed}/3 runtime checks ready", color="primary")
            ui.linear_progress(value=completed / 3, show_value=False).classes("mt-2")

        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Chatwoot").classes("text-sm text-slate-500")
                ui.label("Ready" if state["chatwoot_ready"] else "Needs configuration").classes(
                    "text-xl font-semibold " + ("text-green-700" if state["chatwoot_ready"] else "text-amber-700")
                )
                status_badge("Personal API token stored", state["token_saved"])
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Meta proxy · optional").classes("text-sm text-slate-500")
                if state["proxy_enabled"]:
                    ui.label(legacy.redact_proxy(state["proxy_value"])).classes("text-base font-medium")
                    status_badge("Proxy configured", state["proxy_ready"])
                else:
                    ui.label("Disabled · direct connection").classes("text-base font-medium")
                    ui.label("You can still test a proxy below without enabling or saving it.").classes("text-xs text-slate-500")
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Linked conversations").classes("text-sm text-slate-500")
                ui.label(str(state["link_count"])).classes("text-2xl font-semibold")

        with ui.card().classes("w-full p-6"):
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.avatar("1", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Chatwoot settings").classes("text-xl font-semibold")
                    ui.label("Use your Chatwoot Personal Access Token. The numeric Account ID and Inbox ID can be detected automatically.").classes("text-slate-500")

            base = ui.input("Chatwoot URL", value=state["base"], placeholder="https://chatwoot.example.com").props("outlined").classes("w-full")
            ui.label("Example: https://chatwoot.yourdomain.com — no /app path and no trailing account/inbox URL.").classes("text-xs text-slate-500 -mt-2")

            token_placeholder = "Stored — click Load saved token to inspect it" if state["token_saved"] else "Paste Personal Access Token"
            token = ui.input("Personal API token", password=True, password_toggle_button=True, placeholder=token_placeholder).props("outlined autocomplete=off").classes("w-full")
            ui.label("Find it in Chatwoot: avatar (bottom-left) → Profile Settings → Personal Access Token. Treat it like a password.").classes("text-xs text-slate-500 -mt-2")

            with ui.row().classes("gap-3 flex-wrap"):
                async def load_saved_token():
                    saved = get_saved_secret("chatwoot_api_token")
                    if not saved:
                        ui.notify("No Chatwoot token is saved yet", type="warning")
                        return
                    token.value = saved
                    ui.notify("Saved token loaded into the field. Use the eye icon to reveal it.", type="warning")

                ui.button("Load saved token", on_click=load_saved_token, icon="visibility").props("outline")

            with ui.row().classes("w-full gap-4 flex-wrap"):
                account = ui.input("Account ID (numeric)", value=state["account"], placeholder="1").props("outlined").classes("grow min-w-48")
                inbox = ui.input("Inbox ID (numeric)", value=state["inbox"], placeholder="2").props("outlined").classes("grow min-w-48")
            ui.label("Important: the Inbox Identifier token shown in Chatwoot's inbox Configuration tab is NOT the Inbox ID. Inbox ID is a number.").classes("text-sm text-amber-700")
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
                        ui.notify("Account and the only inbox were detected automatically", type="positive")
                    else:
                        ui.notify(f"Account detected. Choose one of {len(options)} inboxes below.", type="positive")
                except Exception as exc:
                    ui.notify(f"Could not detect Chatwoot IDs: {exc}", type="negative", close_button=True)

            ui.button("Detect Account ID and inboxes", on_click=detect_ids, icon="travel_explore").classes("mt-2")

            ui.separator().classes("my-5")
            with ui.row().classes("items-center gap-3 mb-2"):
                ui.avatar("2", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Meta proxy · optional").classes("text-xl font-semibold")
                    ui.label("Test a proxy first. Enable it for Meta only after you are satisfied with the result.").classes("text-slate-500")

            proxy_switch = ui.switch("Use this proxy for Meta", value=state["proxy_enabled"])
            existing_proxy = bool(state["proxy_value"])
            proxy_placeholder = "Stored — click Load saved proxy to inspect it" if existing_proxy else "http://user:password@host:8888"
            proxy_input = ui.input("Proxy URL", password=True, password_toggle_button=True, placeholder=proxy_placeholder).props("outlined autocomplete=off").classes("w-full")
            ui.label("Supported: http, https, socks5, socks5h. The test below works before saving and while the Meta proxy toggle is OFF.").classes("text-xs text-slate-500")

            with ui.row().classes("gap-3 flex-wrap"):
                async def load_saved_proxy():
                    saved = get_saved_secret("proxy_url")
                    if not saved:
                        ui.notify("No proxy URL is saved yet", type="warning")
                        return
                    proxy_input.value = saved
                    ui.notify("Saved proxy loaded into the field. Use the eye icon to reveal it.", type="warning")

                ui.button("Load saved proxy", on_click=load_saved_proxy, icon="visibility").props("outline")

            with ui.card().classes("w-full p-4 mt-3 bg-slate-50 border border-slate-200"):
                with ui.row().classes("w-full gap-4 flex-wrap"):
                    with ui.column().classes("grow min-w-56 gap-1"):
                        ui.label("Direct VPS public IP").classes("text-xs uppercase tracking-wide text-slate-500")
                        direct_ip_label = ui.label("Not tested yet").classes("text-lg font-mono font-semibold")
                    with ui.column().classes("grow min-w-56 gap-1"):
                        ui.label("Proxy public IP").classes("text-xs uppercase tracking-wide text-slate-500")
                        proxy_ip_label = ui.label("Not tested yet").classes("text-lg font-mono font-semibold")
                proxy_status = ui.label("Enter a proxy URL and test it before saving.").classes("text-sm text-slate-600 mt-2")
                checked_at_label = ui.label("").classes("text-xs text-slate-400")

            async def test_proxy_now():
                try:
                    candidate = (proxy_input.value or "").strip() or get_saved_secret("proxy_url")
                    result = await asyncio.to_thread(test_proxy_url, candidate)
                    direct_ip_label.text = result["direct_ip"]
                    proxy_ip_label.text = result["proxy_ip"]
                    checked_at_label.text = f"Last checked: {result['checked_at']}"
                    if result["different"]:
                        proxy_status.text = "Proxy is changing the public egress IP."
                        proxy_status.classes(replace="text-sm text-green-700 mt-2 font-medium")
                        ui.notify(f"Proxy works for this HTTP test: {result['proxy_ip']}", type="positive")
                    else:
                        proxy_status.text = "Direct and proxy IP are identical. Do not use this proxy for Meta until explained."
                        proxy_status.classes(replace="text-sm text-red-700 mt-2 font-medium")
                        ui.notify("Proxy IP matches the VPS IP", type="warning", close_button=True)
                except Exception as exc:
                    proxy_status.text = f"Proxy test failed: {exc}"
                    proxy_status.classes(replace="text-sm text-red-700 mt-2 font-medium")
                    ui.notify(f"Proxy test failed: {exc}", type="negative", close_button=True)

            ui.button("Test proxy now — no save required", on_click=test_proxy_now, icon="public").props("outline").classes("mt-2")
            ui.label("A different IP proves the HTTP request used different egress. It does not prove residential classification or Meta acceptance.").classes("text-xs text-amber-700")

            async def save():
                try:
                    await asyncio.to_thread(
                        save_configuration,
                        base.value or "", account.value or "", inbox.value or "", token.value or "",
                        bool(proxy_switch.value), proxy_input.value or "",
                    )
                    token.value = ""
                    proxy_input.value = ""
                    ui.notify("Configuration saved. Run the Chatwoot connection test next.", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative", close_button=True)

            ui.button("Save all settings", on_click=save, icon="save").classes("mt-5")

        with ui.card().classes("w-full p-6"):
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.avatar("3", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Validate Chatwoot").classes("text-xl font-semibold")
                    ui.label("This checks the saved token, numeric Account ID and numeric Inbox ID against Chatwoot.").classes("text-slate-500")

            async def test_chatwoot():
                try:
                    message = await asyncio.to_thread(verify_chatwoot)
                    ui.notify(message, type="positive")
                except Exception as exc:
                    ui.notify(f"Chatwoot test failed: {exc}", type="negative", close_button=True)

            ui.button("Test Chatwoot", on_click=test_chatwoot, icon="dns")

        with ui.card().classes("w-full p-6"):
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.avatar("4", color="primary", text_color="white")
                with ui.column().classes("gap-0"):
                    ui.label("Configure Chatwoot webhook").classes("text-xl font-semibold")
                    ui.label("Create a message_created webhook in Chatwoot pointing to this integration domain.").classes("text-slate-500")
            ui.code("https://<integration-domain>/webhooks/chatwoot/<CHATWOOT_WEBHOOK_SECRET>").classes("w-full")
            ui.label("CHATWOOT_WEBHOOK_SECRET remains a deployment secret and is intentionally not shown here.").classes("text-sm text-slate-500")

        with ui.card().classes("w-full p-6 border border-emerald-100"):
            with ui.row().classes("items-center gap-3 mb-2"):
                ui.avatar("5", color="positive", text_color="white")
                ui.label("Run the live end-to-end check").classes("text-xl font-semibold")
            ui.label("After the first Meta login, send one fresh customer message and confirm it appears once in Chatwoot. Reply from Chatwoot and confirm it arrives once in Meta.").classes("text-slate-600")
            ui.label("CI cannot prove this step without your VPS, Meta account and Chatwoot instance.").classes("text-sm text-amber-700 mt-2")


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
