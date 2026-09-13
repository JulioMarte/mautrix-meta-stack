"""NiceGUI production surface for the single-client Matrix <-> Chatwoot integration.

The integration logic remains in the existing backend modules; this file owns the public
HTTP/UI surface. One process and one port are used behind Coolify/Traefik.
"""
from __future__ import annotations

import asyncio
import hmac
import os
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


async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.middleware("http")(_security_headers)


def authenticated() -> bool:
    return bool(app.storage.user.get("authenticated", False))


def effective_proxy():
    return prod.effective_proxy()


def save_configuration(base: str, account: str, inbox: str, token: str,
                       proxy_enabled: bool, proxy_url: str) -> None:
    base = (base or "").strip().rstrip("/")
    parsed = urlsplit(base)
    scheme_ok = parsed.scheme == "https" or (ALLOW_INSECURE_CHATWOOT and parsed.scheme == "http")
    if not scheme_ok or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Chatwoot URL must be a clean HTTPS base URL")
    if not str(account).isdigit() or not str(inbox).isdigit():
        raise ValueError("Account ID and Inbox ID must be numeric")

    legacy.set_setting("chatwoot_base_url", base)
    legacy.set_setting("chatwoot_account_id", str(account))
    legacy.set_setting("chatwoot_inbox_id", str(inbox))
    if token and token.strip():
        legacy.set_setting("chatwoot_api_token", token.strip())

    managed, _, _ = effective_proxy()
    if not managed:
        legacy.set_setting("proxy_enabled", "1" if proxy_enabled else "0")
        if proxy_url and proxy_url.strip():
            prod.validate_proxy_url(proxy_url.strip())
            legacy.set_setting("proxy_url", proxy_url.strip())

    runtime.ensure_activation_boundary()


def verify_chatwoot() -> str:
    if not legacy.configured():
        raise RuntimeError("Chatwoot is not configured")
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = str(legacy.get_setting("chatwoot_inbox_id"))
    data = prod.cw_get(f"/api/v1/accounts/{account_id}/inboxes")
    inboxes = data.get("payload") if isinstance(data, dict) else data
    if not isinstance(inboxes, list) or not any(str(item.get("id")) == inbox_id for item in inboxes):
        raise RuntimeError("Configured Chatwoot inbox was not found")
    return "Chatwoot connection verified"


def verify_proxy() -> str:
    _, enabled, proxy = effective_proxy()
    if not enabled or not proxy:
        raise RuntimeError("Proxy is not configured")
    response = requests.get(
        "https://api.ipify.org?format=json",
        proxies={"http": proxy, "https": proxy},
        timeout=20,
    )
    response.raise_for_status()
    observed = response.json().get("ip", "unknown")
    return f"Proxy egress verified: {observed}"


@app.get("/")
async def root_redirect():
    return RedirectResponse("/admin", status_code=303)


@app.get("/health")
async def health():
    return {"ok": True, "configured": legacy.configured(), "admin_ui": "nicegui"}


@app.get("/internal/proxy")
async def internal_proxy(request: Request):
    auth = request.headers.get("authorization", "")
    expected_user = "mautrix"
    expected_password = prod.PROXY_RESOLVER_SECRET
    if not auth.lower().startswith("basic "):
        return JSONResponse({"detail": "not found"}, status_code=404)
    import base64
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not (hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)):
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
    payload = await request.json()
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
    event_key = "chatwoot:" + message_id if message_id else ""
    if event_key and legacy.event_seen(event_key):
        return {"ok": True, "duplicate": True}
    with legacy.db() as conn:
        link = conn.execute(
            "SELECT * FROM room_links WHERE conversation_id = ?", (int(conversation_id),)
        ).fetchone()
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped conversation"}
    await asyncio.to_thread(
        legacy.send_matrix_message,
        link["room_id"],
        content,
        "cw-" + (message_id or legacy.uuid.uuid4().hex),
    )
    if event_key:
        legacy.mark_event(event_key, "chatwoot_to_matrix")
    return {"ok": True}


def page_shell(title: str):
    ui.page_title(title)
    ui.colors(primary="#2563eb", secondary="#475569", accent="#0f766e", positive="#15803d", negative="#b91c1c")
    ui.query("body").classes("bg-slate-50")


@ui.page("/admin/login")
def login_page():
    page_shell("Integration Admin · Login")
    if authenticated():
        ui.navigate.to("/admin")
        return

    with ui.column().classes("w-full min-h-screen items-center justify-center p-6"):
        with ui.card().classes("w-full max-w-md p-6 shadow-lg"):
            ui.label("Integration Admin").classes("text-2xl font-semibold")
            ui.label("Matrix ↔ Chatwoot · single client").classes("text-slate-500 mb-4")
            password = ui.input("Admin password", password=True, password_toggle_button=True).props("outlined autocomplete=current-password").classes("w-full")
            status = ui.label("").classes("text-red-700")

            async def do_login():
                if hmac.compare_digest(password.value or "", legacy.ADMIN_PASSWORD):
                    app.storage.user["authenticated"] = True
                    app.storage.user["login_at"] = int(asyncio.get_running_loop().time())
                    ui.navigate.to("/admin")
                    return
                status.text = "Invalid password"
                await asyncio.sleep(0.35)

            ui.button("Sign in", on_click=do_login, icon="login").classes("w-full mt-2")


@ui.page("/admin")
def admin_page():
    page_shell("Integration Admin")
    if not authenticated():
        ui.navigate.to("/admin/login")
        return

    managed, proxy_enabled, proxy_value = effective_proxy()
    with legacy.db() as conn:
        link_count = conn.execute("SELECT COUNT(*) AS n FROM room_links").fetchone()["n"]

    with ui.header().classes("items-center justify-between bg-white text-slate-900 border-b border-slate-200"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("hub", size="md").classes("text-blue-600")
            ui.label("Matrix ↔ Chatwoot").classes("text-xl font-semibold")
        async def logout():
            app.storage.user.clear()
            ui.navigate.to("/admin/login")
        ui.button("Logout", on_click=logout, icon="logout").props("flat")

    with ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-5"):
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Chatwoot").classes("text-sm text-slate-500")
                if legacy.configured():
                    ui.label("Configured").classes("text-xl font-semibold text-green-700")
                else:
                    ui.label("Needs configuration").classes("text-xl font-semibold text-amber-700")
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Linked conversations").classes("text-sm text-slate-500")
                ui.label(str(link_count)).classes("text-xl font-semibold")
            with ui.card().classes("p-5 grow min-w-64"):
                ui.label("Meta proxy").classes("text-sm text-slate-500")
                if proxy_enabled:
                    ui.label(legacy.redact_proxy(proxy_value)).classes("text-base font-medium")
                    if managed:
                        ui.badge("Managed by Coolify").props("color=blue")
                else:
                    ui.label("Direct connection").classes("text-base font-medium")

        with ui.card().classes("w-full p-6"):
            ui.label("Chatwoot configuration").classes("text-xl font-semibold")
            ui.label("Secrets are stored privately and are never rendered back.").classes("text-slate-500 mb-2")
            with ui.row().classes("w-full gap-4 flex-wrap"):
                base = ui.input("Base URL", value=legacy.get_setting("chatwoot_base_url"), placeholder="https://chatwoot.example.com").props("outlined").classes("grow min-w-72")
                account = ui.input("Account ID", value=legacy.get_setting("chatwoot_account_id")).props("outlined").classes("grow min-w-48")
                inbox = ui.input("Inbox ID", value=legacy.get_setting("chatwoot_inbox_id")).props("outlined").classes("grow min-w-48")
            token = ui.input("API token", password=True, password_toggle_button=True, placeholder="Leave blank to keep current token").props("outlined autocomplete=off").classes("w-full")

            proxy_switch = None
            proxy_input = None
            if managed:
                ui.separator().classes("my-3")
                ui.label("Meta proxy").classes("text-lg font-semibold")
                ui.label("Managed by META_PROXY_URL / META_PROXY_ENABLED in Coolify. The credential cannot be edited from this panel.").classes("text-slate-500")
            else:
                ui.separator().classes("my-3")
                ui.label("Meta proxy").classes("text-lg font-semibold")
                proxy_switch = ui.switch("Use proxy", value=proxy_enabled)
                proxy_input = ui.input("Proxy URL", password=True, password_toggle_button=True, placeholder="http://user:password@host:port").props("outlined autocomplete=off").classes("w-full")

            async def save():
                try:
                    await asyncio.to_thread(
                        save_configuration,
                        base.value or "",
                        account.value or "",
                        inbox.value or "",
                        token.value or "",
                        bool(proxy_switch.value) if proxy_switch else proxy_enabled,
                        proxy_input.value or "" if proxy_input else "",
                    )
                    token.value = ""
                    if proxy_input:
                        proxy_input.value = ""
                    ui.notify("Configuration saved", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            ui.button("Save configuration", on_click=save, icon="save").classes("mt-3")

        with ui.card().classes("w-full p-6"):
            ui.label("Connectivity tests").classes("text-xl font-semibold")
            ui.label("Run these after every Coolify deployment or credential change.").classes("text-slate-500 mb-3")
            with ui.row().classes("gap-3 flex-wrap"):
                async def test_chatwoot():
                    try:
                        message = await asyncio.to_thread(verify_chatwoot)
                        ui.notify(message, type="positive")
                    except Exception as exc:
                        ui.notify(f"Chatwoot test failed: {exc}", type="negative")

                async def test_proxy():
                    try:
                        message = await asyncio.to_thread(verify_proxy)
                        ui.notify(message, type="positive")
                    except Exception as exc:
                        ui.notify(f"Proxy test failed: {exc}", type="negative")

                ui.button("Test Chatwoot", on_click=test_chatwoot, icon="dns")
                if proxy_enabled:
                    ui.button("Test proxy egress", on_click=test_proxy, icon="public").props("outline")

        with ui.card().classes("w-full p-6"):
            ui.label("Chatwoot webhook").classes("text-xl font-semibold")
            ui.label("Configure Chatwoot message_created to POST to:").classes("text-slate-500")
            ui.code("https://<integration-domain>/webhooks/chatwoot/<CHATWOOT_WEBHOOK_SECRET>")
            ui.label("The deployment secret is intentionally not displayed here.").classes("text-sm text-slate-500")


def run() -> None:
    legacy.init_db()
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="Integration Admin",
        storage_secret=legacy.SESSION_SECRET,
        session_middleware_kwargs={
            "same_site": "strict",
            "https_only": COOKIE_SECURE,
            "max_age": 8 * 60 * 60,
        },
        reload=False,
        show=False,
        uvicorn_logging_level="warning",
        access_log=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
