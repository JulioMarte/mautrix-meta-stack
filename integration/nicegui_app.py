"""NiceGUI entrypoint with managed Meta onboarding.

The previously proven admin implementation is kept in ``nicegui_legacy`` and
re-exported here. Managed Meta onboarding is registered as an authenticated page
and uses the same admin-v2 chrome operators see on /admin/basic.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import nicegui_legacy as _legacy_ui
from nicegui_legacy import *  # noqa: F401,F403 - compatibility surface
from nicegui import ui

import admin_v2 as _admin_v2
import meta_admin_patch as _meta_admin_patch
from meta_helper_routes import create_pairing, register_helper_routes
from meta_login_recovery import is_missing_login_process
from meta_provisioning import (
    MautrixProvisioningClient,
    ProvisioningError,
    connection_summary,
    operator_error_message,
    provisioning_debug,
    safe_step,
)


# Compose starts this module directly, so install the native admin-v2 sidebar
# here rather than relying on the Docker image CMD wrapper. admin_v2 is already
# fully imported at this point, avoiding the circular import seen when the patch
# was installed from a helper module.
_meta_admin_patch.install()


META_STEP_KEY = "meta_onboarding_step"
META_STEP_STARTED_KEY = "meta_onboarding_step_started_at"
META_LAST_COMPLETE_KEY = "meta_onboarding_last_complete_at"
META_PROCESS_TTL = 30 * 60

# Prefer the mobile Messenger login APIs because the exact pinned v26.08.1
# runtime exposes them as user_input flows. They can therefore be completed in
# the authenticated admin UI without cross-origin cookie extraction or a local
# helper. Web-cookie flows remain available as fallback choices.
FLOW_PREFERENCE = (
    "messenger-lite-android",
    "messenger-lite",
    "facebook",
    "messenger",
)
FLOW_PRODUCT_LABELS = {
    "messenger-lite-android": "Messenger Android — recomendado; acceso desde este panel",
    "messenger-lite": "Messenger iOS — acceso desde este panel",
    "facebook": "Facebook web — requiere helper local para cookies",
    "messenger": "Messenger web — requiere helper local para cookies",
}


def __getattr__(name: str):
    """Keep underscore-prefixed helpers from the old module available to tests."""
    return getattr(_legacy_ui, name)


def _prov_client() -> MautrixProvisioningClient:
    return MautrixProvisioningClient()


def _clear_meta_step() -> None:
    _legacy_ui.legacy.set_setting(META_STEP_KEY, "")
    _legacy_ui.legacy.set_setting(META_STEP_STARTED_KEY, "")


def _store_meta_step(step: dict[str, Any]) -> dict[str, Any]:
    safe = safe_step(step)
    if safe.get("type") == "complete":
        _legacy_ui.legacy.set_setting(META_LAST_COMPLETE_KEY, _legacy_ui._now_utc())
        _clear_meta_step()
        return safe
    _legacy_ui.legacy.set_setting(META_STEP_KEY, json.dumps(safe, separators=(",", ":")))
    _legacy_ui.legacy.set_setting(META_STEP_STARTED_KEY, str(int(time.time())))
    return safe


def _load_meta_step() -> tuple[dict[str, Any], bool]:
    raw = _legacy_ui.legacy.get_setting(META_STEP_KEY)
    if not raw:
        return {}, False
    try:
        step = json.loads(raw)
        if not isinstance(step, dict):
            return {}, False
    except json.JSONDecodeError:
        return {}, False
    try:
        started = int(_legacy_ui.legacy.get_setting(META_STEP_STARTED_KEY) or "0")
    except ValueError:
        started = 0
    expired = bool(started and time.time() - started > META_PROCESS_TTL)
    return step, expired


def _recover_missing_login_process(exc: Exception) -> bool:
    """Drop a persisted step when mautrix has lost its temporary login process."""
    if not is_missing_login_process(exc):
        return False
    _clear_meta_step()
    return True


def meta_runtime_state() -> dict[str, Any]:
    """Fetch a product-facing Meta connection state without exposing secrets."""
    try:
        whoami = _prov_client().whoami()
        summary = connection_summary(whoami)
        summary["available"] = True
        summary["error"] = ""
        summary["network"] = (whoami.get("network") or {}).get("display_name") if isinstance(whoami.get("network"), dict) else "Meta"
        return summary
    except Exception as exc:
        return {
            "available": False,
            "status": "error",
            "connected": False,
            "logins": [],
            "error": str(exc),
            "network": "Meta",
        }


def _friendly_step_title(step_type: str) -> str:
    return {
        "cookies": "Autenticación web segura requerida",
        "user_input": "Completa el acceso a Facebook",
        "display_and_wait": "Confirma el paso mostrado",
        "webauthn": "Verificación del dispositivo requerida",
        "client_http": "Autenticación local requerida",
        "complete": "Cuenta conectada",
    }.get(step_type, "Acción requerida")


def _field_label(field: dict[str, Any]) -> str:
    return str(field.get("name") or field.get("id") or "Dato")


def _field_is_secret(field: dict[str, Any], step: dict[str, Any]) -> bool:
    field_type = str(field.get("type") or "").lower()
    lowered_id = str(field.get("id") or "").lower()
    step_id = str(step.get("step_id") or "").lower()
    if "captcha" in lowered_id or "captcha" in step_id:
        return False
    return (
        field_type in {"password", "secret", "token", "2fa_code", "otp", "code"}
        or any(marker in lowered_id for marker in ("password", "passcode", "token", "2fa", "otp", "code"))
    )


def _ordered_flow_options(flow_options: dict[str, str]) -> dict[str, str]:
    """Keep recommended login methods first without preselecting an action."""
    ordered: dict[str, str] = {}
    for flow_id in FLOW_PREFERENCE:
        if flow_id in flow_options:
            ordered[flow_id] = flow_options[flow_id]
    for flow_id, label in flow_options.items():
        if flow_id not in ordered:
            ordered[flow_id] = label
    return ordered


register_helper_routes(_legacy_ui.app, _prov_client, _store_meta_step)


@ui.page("/admin/meta")
def meta_onboarding_page():
    if not _admin_v2._require_auth():
        return
    _admin_v2._admin_chrome("meta")

    runtime = meta_runtime_state()
    saved_step, expired = _load_meta_step()

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 md:p-6 gap-5"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-0"):
                ui.label("Facebook Messenger").classes("text-2xl font-semibold")
                ui.label("Conecta la cuenta Meta que alimentará Messenger y Marketplace sin usar Element.").classes("text-sm text-slate-500")
            ui.button("Actualizar", icon="refresh", on_click=lambda: ui.navigate.to("/admin/meta")).props("flat no-caps")

        with ui.card().classes("w-full p-6"):
            ui.label("Estado de la cuenta").classes("text-sm text-slate-500")
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
                ui.label(runtime["error"]).classes("text-sm text-red-700 mt-2")

            for login in runtime.get("logins") or []:
                with ui.row().classes("items-center gap-3 mt-3"):
                    ui.icon("account_circle").classes("text-slate-500")
                    with ui.column().classes("gap-0"):
                        ui.label(login.get("name") or "Cuenta Meta").classes("font-semibold")
                        detail = login.get("reason") or login.get("event") or "Sesión registrada"
                        ui.label(str(detail)).classes("text-xs text-slate-500")

        with ui.card().classes("w-full p-6"):
            ui.label("Conectar o reconectar Facebook").classes("text-xl font-semibold")
            ui.label(
                "El panel consulta directamente los métodos que ofrece la versión de mautrix-meta desplegada. "
                "Selecciona un método y la conexión comenzará automáticamente."
            ).classes("text-slate-600")

            flow_options: dict[str, str] = {}
            try:
                for flow in _prov_client().flows():
                    flow_id = str(flow.get("id") or "")
                    if not flow_id:
                        continue
                    product_label = FLOW_PRODUCT_LABELS.get(flow_id)
                    name = str(flow.get("name") or flow_id)
                    description = str(flow.get("description") or "")
                    flow_options[flow_id] = product_label or (f"{name} — {description}" if description else name)
            except Exception as exc:
                ui.label(f"No se pudieron cargar los métodos de acceso: {exc}").classes("text-red-700 mt-2")

            async def start_login(event):
                selected = str(event.value or "").strip()
                if not selected:
                    return
                try:
                    step = await asyncio.to_thread(_prov_client().start, selected)
                    _store_meta_step(step)
                    ui.navigate.to("/admin/meta")
                except Exception as exc:
                    provisioning_debug(
                        "ui_start_failed",
                        flow_id=selected,
                        error_type=type(exc).__name__,
                        status_code=getattr(exc, "status_code", 0),
                        errcode=getattr(exc, "errcode", ""),
                    )
                    ui.notify(f"No se pudo iniciar la conexión: {operator_error_message(exc)}", type="negative", close_button=True)

            async def disconnect_all():
                try:
                    await asyncio.to_thread(_prov_client().logout, "all")
                    _clear_meta_step()
                    ui.notify("Cuenta Meta desconectada", type="positive")
                    ui.navigate.to("/admin/meta")
                except Exception as exc:
                    ui.notify(f"No se pudo desconectar: {exc}", type="negative", close_button=True)

            if saved_step:
                ui.label(
                    "Ya hay un intento de conexión en curso. Complétalo o cancélalo abajo antes de elegir otro método."
                ).classes("text-sm text-amber-700 mt-3")
            elif flow_options:
                ui.select(
                    _ordered_flow_options(flow_options),
                    value=None,
                    label="Método de conexión",
                    on_change=start_login,
                ).props("outlined").classes("w-full mt-3")
                if any(flow_id.startswith("messenger-lite") for flow_id in flow_options):
                    ui.label(
                        "Recomendado: Messenger Android. Al seleccionarlo, el acceso comienza de inmediato; "
                        "tus credenciales se envían por HTTPS al backend y de ahí, por la red privada, al provisioning API de mautrix."
                    ).classes("text-sm text-blue-700 mt-2")
            else:
                ui.label("No hay métodos de conexión disponibles en este momento.").classes("text-sm text-slate-500 mt-3")

            if runtime.get("logins"):
                ui.button("Desconectar", icon="link_off", on_click=disconnect_all).props("outline color=negative").classes("mt-3")

        if saved_step:
            step_type = str(saved_step.get("type") or "")
            with ui.card().classes("w-full p-6 border border-amber-100"):
                ui.label(_friendly_step_title(step_type)).classes("text-xl font-semibold")
                if saved_step.get("instructions"):
                    ui.label(str(saved_step["instructions"])).classes("text-slate-600")
                if expired:
                    ui.label("Este intento lleva más de 30 minutos abierto. Cancélalo y comienza uno nuevo antes de continuar.").classes("text-amber-700 font-medium mt-2")

                async def cancel_login():
                    login_id = str(saved_step.get("login_id") or "")
                    try:
                        if login_id:
                            await asyncio.to_thread(_prov_client().cancel, login_id)
                    except ProvisioningError:
                        pass
                    _clear_meta_step()
                    ui.navigate.to("/admin/meta")

                if step_type == "cookies":
                    url = str((saved_step.get("cookies") or {}).get("url") or "https://www.facebook.com/")
                    ui.label(
                        "Elegiste un método web. La versión actual de mautrix necesita las cookies de una sesión de Facebook y un "
                        "navegador normal no puede entregarlas de forma segura al panel por las restricciones de origen y cookies HttpOnly."
                    ).classes("text-slate-700 mt-3")
                    ui.label(f"Sitio de autenticación: {url}").classes("text-sm text-slate-500")
                    ui.label(
                        "Puedes cancelar este intento y usar Messenger Android/iOS para completar el acceso directamente en el panel, "
                        "o usar el helper local para este método web."
                    ).classes("text-blue-700 font-medium mt-2")

                    async def launch_helper():
                        try:
                            pairing = create_pairing(saved_step)
                            handoff_id = json.dumps(pairing["id"])
                            token = json.dumps(pairing["token"])
                            # A custom desktop protocol has no standard NiceGUI Python
                            # abstraction; this is deliberate, user-initiated JS interop.
                            await ui.run_javascript(
                                "window.location.href = 'mautrix-meta-helper://connect?origin=' + "
                                "encodeURIComponent(window.location.origin) + '&id=' + "
                                f"encodeURIComponent({handoff_id}) + '&token=' + encodeURIComponent({token});"
                            )
                            ui.notify("Se abrió el helper. Si el navegador pregunta, autoriza abrir la aplicación.", type="info")
                        except Exception as exc:
                            ui.notify(f"No se pudo crear el emparejamiento: {exc}", type="negative", close_button=True)

                    ui.button("Abrir helper de Facebook", icon="open_in_new", on_click=launch_helper).classes("mt-3")
                    ui.label("El helper es necesario únicamente para los métodos web basados en cookies.").classes("text-xs text-slate-500 mt-2")

                elif step_type == "user_input":
                    user_input = saved_step.get("user_input") or {}
                    attachments = user_input.get("attachments") or []
                    if attachments:
                        with ui.column().classes("w-full gap-2 mt-3"):
                            ui.label("Imagen de verificación").classes("text-sm font-medium text-slate-700")
                            for attachment in attachments:
                                content = str(attachment.get("content") or "")
                                mimetype = str(attachment.get("mimetype") or "")
                                if content and mimetype.startswith("image/"):
                                    ui.image(
                                        f"data:{mimetype};base64,{content}"
                                    ).classes("max-w-md w-auto border rounded bg-white p-2")
                            ui.label(
                                "Escribe en el campo de abajo los caracteres que ves en la imagen."
                            ).classes("text-sm text-slate-500")

                    inputs: dict[str, Any] = {}
                    for field in user_input.get("fields") or []:
                        field_id = str(field.get("id") or "")
                        if not field_id:
                            continue
                        options = field.get("options") or []
                        if options:
                            opts = {str(o.get("id")): str(o.get("name") or o.get("id")) for o in options if o.get("id") is not None}
                            inputs[field_id] = ui.select(opts, label=_field_label(field)).props("outlined").classes("w-full")
                        else:
                            secret = _field_is_secret(field, saved_step)
                            inputs[field_id] = ui.input(
                                _field_label(field), password=secret, password_toggle_button=secret
                            ).props("outlined autocomplete=off").classes("w-full")

                    async def submit_input():
                        values = {key: str(widget.value or "") for key, widget in inputs.items()}
                        try:
                            step = await asyncio.to_thread(
                                _prov_client().submit_user_input,
                                str(saved_step.get("login_id") or ""),
                                str(saved_step.get("step_id") or ""),
                                values,
                                txn_id=str(saved_step.get("txn_id") or ""),
                            )
                            for key in values:
                                values[key] = ""
                            _store_meta_step(step)
                            ui.navigate.to("/admin/meta")
                        except Exception as exc:
                            for key in values:
                                values[key] = ""
                            if _recover_missing_login_process(exc):
                                provisioning_debug(
                                    "ui_login_process_missing",
                                    operation="submit_user_input",
                                    login_id=str(saved_step.get("login_id") or ""),
                                    step_id=str(saved_step.get("step_id") or ""),
                                    status_code=getattr(exc, "status_code", 0),
                                    errcode=getattr(exc, "errcode", ""),
                                )
                                ui.notify(
                                    "Este intento de conexión ya no existe en mautrix. El bridge pudo haberse reiniciado; inicia una conexión nueva.",
                                    type="warning",
                                    close_button=True,
                                )
                                ui.navigate.to("/admin/meta")
                                return
                            provisioning_debug(
                                "ui_submit_failed",
                                operation="submit_user_input",
                                login_id=str(saved_step.get("login_id") or ""),
                                step_id=str(saved_step.get("step_id") or ""),
                                error_type=type(exc).__name__,
                                status_code=getattr(exc, "status_code", 0),
                                errcode=getattr(exc, "errcode", ""),
                            )
                            ui.notify(f"No se pudo continuar: {operator_error_message(exc)}", type="negative", close_button=True)

                    ui.button("Continuar", icon="arrow_forward", on_click=submit_input).classes("mt-3")

                elif step_type == "display_and_wait":
                    display = saved_step.get("display_and_wait") or {}
                    if display.get("data"):
                        ui.label(str(display["data"])).classes("text-lg font-semibold mt-3")
                    if display.get("image_url"):
                        ui.image(str(display["image_url"])).classes("max-w-sm mt-3")

                    async def continue_wait():
                        try:
                            step = await asyncio.to_thread(
                                _prov_client().wait,
                                str(saved_step.get("login_id") or ""),
                                str(saved_step.get("step_id") or ""),
                                txn_id=str(saved_step.get("txn_id") or ""),
                            )
                            _store_meta_step(step)
                            ui.navigate.to("/admin/meta")
                        except Exception as exc:
                            if _recover_missing_login_process(exc):
                                provisioning_debug(
                                    "ui_login_process_missing",
                                    operation="display_and_wait",
                                    login_id=str(saved_step.get("login_id") or ""),
                                    step_id=str(saved_step.get("step_id") or ""),
                                    status_code=getattr(exc, "status_code", 0),
                                    errcode=getattr(exc, "errcode", ""),
                                )
                                ui.notify(
                                    "Este intento de conexión ya no existe en mautrix. El bridge pudo haberse reiniciado; inicia una conexión nueva.",
                                    type="warning",
                                    close_button=True,
                                )
                                ui.navigate.to("/admin/meta")
                                return
                            provisioning_debug(
                                "ui_submit_failed",
                                operation="display_and_wait",
                                login_id=str(saved_step.get("login_id") or ""),
                                step_id=str(saved_step.get("step_id") or ""),
                                error_type=type(exc).__name__,
                                status_code=getattr(exc, "status_code", 0),
                                errcode=getattr(exc, "errcode", ""),
                            )
                            ui.notify(f"No se pudo continuar: {operator_error_message(exc)}", type="negative", close_button=True)

                    ui.button("Ya completé este paso", on_click=continue_wait, icon="check").classes("mt-3")

                elif step_type in {"webauthn", "client_http"}:
                    ui.label(
                        "El bridge solicitó una capacidad local que este panel todavía no implementa para este método. "
                        "No se intentará simularla ni pedir secretos manualmente. Cancela este intento y prueba Messenger Android/iOS."
                    ).classes("text-amber-700 mt-3")

                with ui.row().classes("mt-4 gap-3"):
                    ui.button("Cancelar intento", icon="close", on_click=cancel_login).props("outline color=negative")

        with ui.card().classes("w-full p-5 bg-slate-50"):
            ui.label("Seguridad").classes("font-semibold")
            ui.label(
                "El secreto de provisioning nunca llega al navegador. El estado persistido del onboarding contiene metadatos "
                "sanitizados y, cuando Facebook exige un CAPTCHA, únicamente la imagen temporal del desafío. "
                "Las contraseñas y cookies no se guardan en la configuración genérica del panel."
            ).classes("text-sm text-slate-600")
            if _legacy_ui.legacy.get_setting(META_LAST_COMPLETE_KEY):
                ui.label(f"Última conexión completada: {_legacy_ui.legacy.get_setting(META_LAST_COMPLETE_KEY)}").classes("text-xs text-slate-500 mt-2")


if __name__ in {"__main__", "__mp_main__"}:
    _legacy_ui.run()
