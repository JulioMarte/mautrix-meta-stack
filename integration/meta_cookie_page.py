"""Recovery fallback for cookie-based Meta onboarding.

This intentionally avoids reproducing Facebook authentication. The operator logs
in on facebook.com with the normal browser flow (including MFA/passkeys), then
pastes a copied authenticated request. Only supported session cookies explicitly
requested by the live mautrix-meta BridgeV2 step are forwarded to the private
provisioning API.
"""
from __future__ import annotations

import asyncio

from nicegui import ui

import admin_v2
import nicegui_app
from meta_cookie_auth import CookieInputError, login_with_browser_cookies
from meta_onboarding_diagnostics import new_trace_id
from meta_provisioning import ProvisioningError, operator_error_message, provisioning_debug


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
                ui.label("Fallback de recuperación: conecta usando una sesión de navegador existente.").classes("text-sm text-slate-500")
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

        with ui.card().classes("w-full p-6 border border-blue-100"):
            ui.label("Fallback web — 4 pasos").classes("text-xl font-semibold")
            ui.label(
                "Usa este método solo si el flujo recomendado Messenger Android no puede completarse. Inicia sesión directamente en Facebook y copia una petición ya autenticada."
            ).classes("text-slate-600 mb-2")

            with ui.column().classes("gap-4 mt-2"):
                with ui.row().classes("items-start gap-3"):
                    ui.badge("1").props("rounded color=primary")
                    with ui.column().classes("gap-0"):
                        ui.label("Abre Facebook e inicia sesión normalmente").classes("font-semibold")
                        ui.label(
                            "Usa una ventana privada/incógnito si quieres una sesión limpia. Completa en Facebook cualquier passkey, código, aprobación, CAPTCHA o checkpoint."
                        ).classes("text-sm text-slate-600")

                with ui.row().classes("items-start gap-3"):
                    ui.badge("2").props("rounded color=primary")
                    with ui.column().classes("gap-0"):
                        ui.label("Abre las herramientas del navegador").classes("font-semibold")
                        ui.label(
                            "Chrome/Edge: F12 o Ctrl+Shift+I en Windows/Linux; Cmd+Option+I en macOS. Luego entra en la pestaña Network / Red."
                        ).classes("text-sm text-slate-600")

                with ui.row().classes("items-start gap-3"):
                    ui.badge("3").props("rounded color=primary")
                    with ui.column().classes("gap-0"):
                        ui.label("Copia una petición autenticada de Facebook").classes("font-semibold")
                        ui.label(
                            "En Network selecciona Fetch/XHR, recarga Facebook o abre un chat, busca una petición a facebook.com (por ejemplo graphql), haz clic derecho y elige Copy → Copy as cURL (POSIX)."
                        ).classes("text-sm text-slate-600")
                        ui.label(
                            "No hace falta buscar ni copiar cada cookie por separado: el cURL ya contiene el Cookie header que necesitamos."
                        ).classes("text-xs text-blue-700")

                with ui.row().classes("items-start gap-3"):
                    ui.badge("4").props("rounded color=primary")
                    with ui.column().classes("gap-0"):
                        ui.label("Pega aquí y conecta").classes("font-semibold")
                        ui.label(
                            "Pega el cURL completo en el cuadro inferior y pulsa Conectar Facebook. El panel conserva únicamente las cookies de sesión Meta compatibles y envía solo las que la versión desplegada de mautrix-meta solicite."
                        ).classes("text-sm text-slate-600")

            ui.link("Abrir Facebook Messenger en una pestaña nueva", "https://www.facebook.com/messages/", new_tab=True).classes("text-blue-700 font-medium mt-4")

            with ui.expansion("¿Y si ya tengo las cookies?", icon="cookie").classes("w-full mt-3"):
                ui.label(
                    "También puedes pegar directamente un Cookie header, un objeto JSON de cookies o un arreglo JSON exportado por una extensión del navegador. El método recomendado sigue siendo Copy as cURL porque suele ser más fácil y evita errores manuales."
                ).classes("text-sm text-slate-600 p-2")

        with ui.card().classes("w-full p-6"):
            ui.label("Pega aquí la sesión del navegador").classes("text-xl font-semibold")
            ui.label(
                "El panel no ejecuta el cURL. Solo lee el encabezado Cookie y entrega al provisioning API privado exactamente las cookies que el paso activo de mautrix-meta requiere."
            ).classes("text-slate-600")

            cookie_text = ui.textarea(
                "Copy as cURL (POSIX), JSON de cookies o Cookie header",
                placeholder="curl 'https://www.facebook.com/...' -H 'cookie: datr=...; c_user=...; sb=...; xs=...'",
            ).props("outlined autogrow autocomplete=off spellcheck=false").classes("w-full mt-4 font-mono text-sm")
            ui.label(
                "Importante: esto contiene una sesión sensible. Se usa únicamente para este intento, no se guarda en la configuración ni se escribe en logs, y el campo se limpia al terminar."
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
                trace_id = new_trace_id()
                try:
                    step = await asyncio.to_thread(login_with_browser_cookies, nicegui_app._prov_client(trace_id), raw)
                    safe = nicegui_app._store_meta_step(step, trace_id=trace_id)
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
                    nicegui_app._record_meta_failure(exc, operation="cookie_fallback", trace_id=trace_id)
                    provisioning_debug(
                        "cookie_ui_login_failed",
                        trace_id=exc.trace_id,
                        error_type=type(exc).__name__,
                        status_code=exc.status_code,
                        errcode=exc.errcode,
                        failure_code=exc.failure_code,
                        retryable=exc.retryable,
                    )
                    result_label.text = operator_error_message(exc)
                    result_label.classes(replace="text-sm mt-3 text-red-700 font-medium")
                except Exception as exc:
                    nicegui_app._record_meta_failure(exc, operation="cookie_fallback", trace_id=trace_id)
                    provisioning_debug(
                        "cookie_ui_login_failed",
                        error_type=type(exc).__name__,
                        failure_code="META_LOGIN_UNEXPECTED",
                    )
                    result_label.text = (
                        "No se pudo completar el acceso por un error inesperado del panel. "
                        "Revisa los logs META_LOGIN_DEBUG del mismo momento."
                    )
                    result_label.classes(replace="text-sm mt-3 text-red-700 font-medium")
                finally:
                    cookie_text.value = ""
                    cookie_text.update()
                    raw = ""

            ui.button("Conectar Facebook", icon="login", on_click=connect_from_browser).classes("mt-3")

        with ui.card().classes("w-full p-5 bg-slate-50"):
            ui.label("Qué datos usamos").classes("font-semibold")
            ui.label(
                "El parser reconoce las cookies de sesión Meta datr, c_user, sb y xs, descarta el resto y después envía únicamente el subconjunto solicitado por el paso de login de mautrix-meta."
            ).classes("text-sm text-slate-600")
            ui.label(
                "En la versión v26.08.1 validada por CI, el flujo Facebook solicita actualmente xs, c_user y datr; el fallback conserva compatibilidad con el contrato histórico de cuatro cookies cuando el bridge no publica su esquema."
            ).classes("text-xs text-slate-500 mt-1")
