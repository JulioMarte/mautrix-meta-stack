"""Simple cookie-first Meta onboarding page.

This intentionally avoids reproducing Facebook authentication. The operator logs
in on facebook.com with the normal browser flow (including MFA/passkeys), then
pastes a copied authenticated request. Only the four cookies required by
mautrix-meta are forwarded to the private provisioning API.
"""
from __future__ import annotations

import asyncio

from nicegui import ui

import admin_v2
import nicegui_app
from meta_cookie_auth import CookieInputError, login_with_browser_cookies
from meta_provisioning import ProvisioningError


@ui.page("/admin/meta-cookie")
def meta_cookie_page():
    if not admin_v2._require_auth():
        return
    admin_v2._admin_chrome("meta")

    runtime = nicegui_app.meta_runtime_state()

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 md:p-6 gap-5"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-0"):
                ui.label("Facebook Messenger").classes("text-2xl font-semibold")
                ui.label("Conecta Facebook usando una sesión ya autenticada en tu navegador.").classes("text-sm text-slate-500")
            ui.button("Actualizar", icon="refresh", on_click=lambda: ui.navigate.to("/admin/meta-cookie")).props("flat no-caps")

        with ui.card().classes("w-full p-6"):
            ui.label("Estado de la cuenta").classes("text-sm text-slate-500")
            if runtime.get("connected"):
                ui.label("Conectado").classes("text-2xl font-semibold text-green-700")
            elif runtime.get("status") == "action_required":
                ui.label("Acción requerida").classes("text-2xl font-semibold text-amber-700")
            elif runtime.get("status") == "connecting":
                ui.label("Conectando").classes("text-2xl font-semibold text-blue-700")
            elif runtime.get("status") == "disconnected":
                ui.label("Desconectado").classes("text-2xl font-semibold text-slate-700")
            else:
                ui.label("No disponible").classes("text-2xl font-semibold text-red-700")

            if runtime.get("error"):
                ui.label(str(runtime["error"])).classes("text-sm text-red-700 mt-2")

            for login in runtime.get("logins") or []:
                with ui.row().classes("items-center gap-3 mt-3"):
                    ui.icon("account_circle").classes("text-slate-500")
                    with ui.column().classes("gap-0"):
                        ui.label(login.get("name") or "Cuenta Meta").classes("font-semibold")
                        detail = login.get("reason") or login.get("event") or "Sesión registrada"
                        ui.label(str(detail)).classes("text-xs text-slate-500")

            async def disconnect_all():
                try:
                    await asyncio.to_thread(nicegui_app._prov_client().logout, "all")
                    nicegui_app._clear_meta_step()
                    ui.notify("Cuenta Meta desconectada", type="positive")
                    ui.navigate.to("/admin/meta-cookie")
                except Exception as exc:
                    ui.notify(f"No se pudo desconectar: {exc}", type="negative", close_button=True)

            if runtime.get("logins"):
                ui.button("Desconectar cuenta", icon="link_off", on_click=disconnect_all).props("outline color=negative").classes("mt-3")

        with ui.card().classes("w-full p-6"):
            ui.label("Conectar con cookies del navegador").classes("text-xl font-semibold")
            ui.label(
                "Esta es la ruta simple y estable: Facebook hace el login normal en tu navegador; nuestro panel solo recibe la sesión ya autenticada. "
                "Así, si Facebook pide passkey, código, aprobación en otro dispositivo o CAPTCHA, lo resuelves directamente en Facebook."
            ).classes("text-slate-600")

            with ui.column().classes("gap-2 mt-4"):
                ui.label("1. Abre Facebook en una ventana privada e inicia sesión normalmente.").classes("text-sm")
                ui.label("2. Abre DevTools → Network, filtra por XHR/fetch y abre una petición de Facebook (por ejemplo graphql).").classes("text-sm")
                ui.label("3. Haz Copy as cURL (POSIX) y pega el resultado abajo. También aceptamos un JSON de cookies.").classes("text-sm")

            ui.link("Abrir Facebook en una pestaña nueva", "https://www.facebook.com/messages/", new_tab=True).classes("text-blue-700 font-medium mt-3")

            cookie_text = ui.textarea(
                "Copy as cURL (POSIX), JSON de cookies o Cookie header",
                placeholder="curl 'https://www.facebook.com/...' -H 'cookie: datr=...; c_user=...; sb=...; xs=...'",
            ).props("outlined autogrow autocomplete=off spellcheck=false").classes("w-full mt-4 font-mono text-sm")
            ui.label(
                "El contenido pegado puede incluir cookies de sesión sensibles. Se usa únicamente para este intento, no se guarda en la configuración ni se escribe en logs."
            ).classes("text-xs text-amber-700 mt-2")

            result_label = ui.label("").classes("text-sm mt-3")

            async def connect_from_browser():
                raw = str(cookie_text.value or "")
                if not raw.strip():
                    result_label.text = "Pega primero el cURL o las cookies del navegador."
                    result_label.classes(replace="text-sm mt-3 text-red-700 font-medium")
                    return

                result_label.text = "Conectando con la sesión del navegador…"
                result_label.classes(replace="text-sm mt-3 text-blue-700 font-medium")
                try:
                    step = await asyncio.to_thread(login_with_browser_cookies, nicegui_app._prov_client(), raw)
                    safe = nicegui_app._store_meta_step(step)
                    if str(safe.get("type") or "") != "complete":
                        raise RuntimeError(f"mautrix-meta devolvió un paso inesperado: {safe.get('type') or 'desconocido'}")
                    result_label.text = str(safe.get("instructions") or "Cuenta conectada correctamente.")
                    result_label.classes(replace="text-sm mt-3 text-green-700 font-medium")
                    ui.notify("Facebook conectado correctamente", type="positive")
                    ui.navigate.to("/admin/meta-cookie")
                except CookieInputError as exc:
                    result_label.text = str(exc)
                    result_label.classes(replace="text-sm mt-3 text-red-700 font-medium")
                except ProvisioningError as exc:
                    suffix = f" [{exc.errcode}]" if exc.errcode else ""
                    result_label.text = f"Facebook/mautrix rechazó la sesión: {exc}{suffix}"
                    result_label.classes(replace="text-sm mt-3 text-red-700 font-medium")
                except Exception as exc:
                    result_label.text = f"No se pudo conectar: {exc}"
                    result_label.classes(replace="text-sm mt-3 text-red-700 font-medium")
                finally:
                    # Do not leave the session blob sitting in the UI after the request.
                    cookie_text.value = ""
                    cookie_text.update()
                    raw = ""

            ui.button("Conectar Facebook", icon="login", on_click=connect_from_browser).classes("mt-3")

        with ui.card().classes("w-full p-5 bg-slate-50"):
            ui.label("Qué datos usamos").classes("font-semibold")
            ui.label(
                "Para Facebook, mautrix-meta necesita únicamente las cookies datr, c_user, sb y xs. El parser descarta el resto antes de enviarlas al provisioning API privado."
            ).classes("text-sm text-slate-600")
            ui.label(
                "No ejecutamos el cURL que pegues. Solo extraemos el Cookie header y validamos esas cuatro cookies."
            ).classes("text-xs text-slate-500 mt-1")
