"""Simple Facebook cookie onboarding for the admin UI.

This intentionally mirrors mautrix's proven cookie-login path: the operator logs
into Facebook in a normal browser, pastes a copied cURL/Cookie header/JSON here,
and the integration immediately forwards only the required cookies to BridgeV2.
Raw cookie material is never persisted or logged.
"""
from __future__ import annotations

import asyncio
from typing import Any

from nicegui import ui

import admin_v2
import meta_admin_patch
import nicegui_app
from meta_cookie_input import CookieInputError, parse_cookie_input, select_required_cookies
from meta_provisioning import ProvisioningError, safe_step


COOKIE_PAGE = "/admin/meta-cookies"
LAST_ATTEMPT_KEY = "meta_cookie_last_attempt_at"
LAST_RESULT_KEY = "meta_cookie_last_result"
LAST_ERROR_KEY = "meta_cookie_last_error"

# Make the normal Facebook Messenger sidebar entry point at the stable cookie
# workflow. Keep /admin/meta available for diagnostics/backward compatibility.
meta_admin_patch.NAV_ITEMS[:] = [
    (key, label, icon, COOKIE_PAGE if key == "meta" else target)
    for key, label, icon, target in meta_admin_patch.NAV_ITEMS
]


def _required_cookie_ids(step: dict[str, Any]) -> list[str]:
    fields = (safe_step(step).get("cookies") or {}).get("fields") or []
    return [
        str(field.get("id"))
        for field in fields
        if isinstance(field, dict) and field.get("id") and field.get("required", True)
    ]


def _save_result(result: str, error: str = "") -> None:
    legacy = nicegui_app._legacy_ui.legacy
    legacy.set_setting(LAST_ATTEMPT_KEY, nicegui_app._legacy_ui._now_utc())
    legacy.set_setting(LAST_RESULT_KEY, result[:200])
    legacy.set_setting(LAST_ERROR_KEY, error[:1000])


def _diagnostic_error(exc: Exception) -> str:
    if isinstance(exc, ProvisioningError):
        parts = []
        if exc.errcode:
            parts.append(exc.errcode)
        if exc.status_code:
            parts.append(f"HTTP {exc.status_code}")
        parts.append(str(exc))
        return " · ".join(parts)
    return str(exc)


@ui.page(COOKIE_PAGE)
def meta_cookie_page():
    if not admin_v2._require_auth():
        return
    admin_v2._admin_chrome("meta")

    runtime = nicegui_app.meta_runtime_state()
    legacy = nicegui_app._legacy_ui.legacy

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 md:p-6 gap-5"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-0"):
                ui.label("Facebook Messenger").classes("text-2xl font-semibold")
                ui.label("Conecta una sesión de Facebook ya autenticada usando sus cookies.").classes("text-sm text-slate-500")
            ui.button("Actualizar", icon="refresh", on_click=lambda: ui.navigate.to(COOKIE_PAGE)).props("flat no-caps")

        with ui.card().classes("w-full p-6"):
            ui.label("Estado").classes("text-sm text-slate-500")
            if runtime["status"] == "connected":
                ui.label("Conectado").classes("text-2xl font-semibold text-green-700")
            elif runtime["status"] == "connecting":
                ui.label("Conectando").classes("text-2xl font-semibold text-blue-700")
            elif runtime["status"] == "action_required":
                ui.label("Acción requerida").classes("text-2xl font-semibold text-amber-700")
            elif runtime["status"] == "disconnected":
                ui.label("Desconectado").classes("text-2xl font-semibold text-slate-700")
            else:
                ui.label("No disponible").classes("text-2xl font-semibold text-red-700")
            if runtime.get("error"):
                ui.label(str(runtime["error"])).classes("text-sm text-red-700 mt-2")
            for login in runtime.get("logins") or []:
                ui.label(str(login.get("name") or "Cuenta Meta")).classes("font-medium mt-2")
                detail = login.get("reason") or login.get("event")
                if detail:
                    ui.label(str(detail)).classes("text-xs text-slate-500")

        with ui.card().classes("w-full p-6"):
            ui.label("Conectar con cookies del navegador").classes("text-xl font-semibold")
            ui.label(
                "Este es el método web directo de mautrix-meta. Inicia sesión normalmente en Facebook en una ventana privada; "
                "Facebook se encarga de passkeys, códigos, aprobaciones y cualquier challenge. Después copia una petición autenticada "
                "del navegador y pégala aquí."
            ).classes("text-slate-600")

            with ui.column().classes("gap-1 mt-3 text-sm text-slate-700"):
                ui.label("1. Abre facebook.com en una ventana privada y completa el inicio de sesión.")
                ui.label("2. Abre DevTools → Network, filtra por XHR/fetch y selecciona una petición autenticada (por ejemplo graphql).")
                ui.label("3. Copy → Copy as cURL (POSIX). También aceptamos el header Cookie o un JSON de cookies.")
                ui.label("4. Pega el contenido abajo y pulsa Conectar.")

            cookie_input = ui.textarea(
                "cURL / Cookie header / JSON",
                placeholder="curl 'https://www.facebook.com/...' ...  o  datr=...; c_user=...; sb=...; xs=...",
            ).props("outlined autogrow autocomplete=off spellcheck=false").classes("w-full mt-4 font-mono")
            ui.label(
                "Las cookies requeridas por Facebook son normalmente datr, c_user, sb y xs. El valor pegado se usa solo para esta petición y no se guarda."
            ).classes("text-xs text-slate-500")

            status = ui.label("").classes("text-sm mt-3")

            async def connect_cookie_session():
                raw = str(cookie_input.value or "")
                # Remove the secret from the live UI before any network call.
                cookie_input.value = ""
                cookie_input.update()
                client = nicegui_app._prov_client()
                login_id = ""
                parsed: dict[str, str] = {}
                selected: dict[str, str] = {}
                try:
                    parsed = parse_cookie_input(raw)
                    step = await asyncio.to_thread(client.start, "facebook")
                    if str(step.get("type") or "") != "cookies":
                        raise RuntimeError(f"mautrix devolvió un paso inesperado: {step.get('type') or 'desconocido'}")
                    login_id = str(step.get("login_id") or "")
                    step_id = str(step.get("step_id") or "")
                    txn_id = str(step.get("txn_id") or "")
                    required = _required_cookie_ids(step)
                    if not required:
                        raise RuntimeError("mautrix no informó qué cookies requiere para este login")
                    selected = select_required_cookies(parsed, required)
                    next_step = await asyncio.to_thread(
                        client.submit_cookies_trusted,
                        login_id,
                        step_id,
                        selected,
                        txn_id=txn_id,
                    )
                    safe = safe_step(next_step)
                    if safe.get("type") != "complete":
                        raise RuntimeError(f"mautrix no completó el login; siguiente paso: {safe.get('type') or 'desconocido'}")
                    nicegui_app._legacy_ui.legacy.set_setting(
                        nicegui_app.META_LAST_COMPLETE_KEY,
                        nicegui_app._legacy_ui._now_utc(),
                    )
                    nicegui_app._clear_meta_step()
                    _save_result("connected")
                    status.text = "PASS — Facebook quedó conectado mediante cookies."
                    status.classes(replace="text-sm mt-3 text-green-700 font-medium")
                    ui.notify("Facebook conectado", type="positive")
                    ui.navigate.to(COOKIE_PAGE)
                except (CookieInputError, ProvisioningError, RuntimeError) as exc:
                    detail = _diagnostic_error(exc)
                    _save_result("failed", detail)
                    status.text = "FAIL — " + detail
                    status.classes(replace="text-sm mt-3 text-red-700 font-medium")
                    ui.notify(detail, type="negative", close_button=True)
                    # Failed starts should not leave a dangling provisioning login.
                    if login_id:
                        try:
                            await asyncio.to_thread(client.cancel, login_id)
                        except Exception:
                            pass
                    print(f"meta cookie login failed: {detail}", flush=True)
                except Exception as exc:
                    detail = _diagnostic_error(exc)
                    _save_result("failed", detail)
                    status.text = "FAIL — " + detail
                    status.classes(replace="text-sm mt-3 text-red-700 font-medium")
                    ui.notify(detail, type="negative", close_button=True)
                    if login_id:
                        try:
                            await asyncio.to_thread(client.cancel, login_id)
                        except Exception:
                            pass
                    print(f"meta cookie login unexpected failure: {detail}", flush=True)
                finally:
                    selected.clear()
                    parsed.clear()
                    raw = ""

            async def disconnect_all():
                try:
                    await asyncio.to_thread(nicegui_app._prov_client().logout, "all")
                    nicegui_app._clear_meta_step()
                    _save_result("disconnected")
                    ui.notify("Cuenta Meta desconectada", type="positive")
                    ui.navigate.to(COOKIE_PAGE)
                except Exception as exc:
                    detail = _diagnostic_error(exc)
                    _save_result("disconnect_failed", detail)
                    ui.notify(detail, type="negative", close_button=True)

            with ui.row().classes("gap-3 mt-3"):
                ui.button("Conectar", icon="login", on_click=connect_cookie_session)
                if runtime.get("logins"):
                    ui.button("Desconectar", icon="link_off", on_click=disconnect_all).props("outline color=negative")

        last_attempt = legacy.get_setting(LAST_ATTEMPT_KEY)
        if last_attempt:
            with ui.card().classes("w-full p-5 bg-slate-50"):
                ui.label("Último intento").classes("font-semibold")
                ui.label(f"{last_attempt} · {legacy.get_setting(LAST_RESULT_KEY) or 'unknown'}").classes("text-sm text-slate-600")
                if legacy.get_setting(LAST_ERROR_KEY):
                    ui.label(legacy.get_setting(LAST_ERROR_KEY)).classes("text-sm text-red-700")
                ui.label("Nunca se registran los valores de cookies, contraseñas ni códigos.").classes("text-xs text-slate-500 mt-2")
