"""NiceGUI entrypoint with managed Meta onboarding.

The previously proven admin implementation is kept in ``nicegui_legacy`` and
re-exported here.  This isolates the new onboarding surface while preserving all
existing imports/tests and runtime behavior.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import nicegui_legacy as _legacy_ui
from nicegui_legacy import *  # noqa: F401,F403 - compatibility surface
from nicegui import ui

from meta_helper_routes import create_pairing, register_helper_routes
from meta_provisioning import (
    MautrixProvisioningClient,
    ProvisioningError,
    connection_summary,
    safe_step,
)


META_STEP_KEY = "meta_onboarding_step"
META_STEP_STARTED_KEY = "meta_onboarding_step_started_at"
META_LAST_COMPLETE_KEY = "meta_onboarding_last_complete_at"
META_PROCESS_TTL = 30 * 60


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
        "cookies": "Autenticación segura requerida",
        "user_input": "Completa los datos solicitados",
        "display_and_wait": "Confirma el paso mostrado",
        "webauthn": "Verificación del dispositivo requerida",
        "client_http": "Autenticación local requerida",
        "complete": "Cuenta conectada",
    }.get(step_type, "Acción requerida")


def _field_label(field: dict[str, Any]) -> str:
    return str(field.get("name") or field.get("id") or "Dato")


def _install_admin_meta_link() -> None:
    """Add a small product-navigation link without rewriting the proven /admin page."""
    ui.add_head_html(
        """
        <script>
        document.addEventListener('DOMContentLoaded', () => {
          if (window.location.pathname !== '/admin') return;
          if (document.getElementById('managed-meta-link')) return;
          const a = document.createElement('a');
          a.id = 'managed-meta-link';
          a.href = '/admin/meta';
          a.textContent = 'Conectar Facebook';
          a.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:9999;background:#2563eb;color:white;padding:10px 16px;border-radius:999px;text-decoration:none;font:600 14px system-ui;box-shadow:0 4px 16px rgba(0,0,0,.18)';
          document.body.appendChild(a);
        });
        </script>
        """,
        shared=True,
    )


_install_admin_meta_link()
register_helper_routes(_legacy_ui.app, _prov_client, _store_meta_step)


@ui.page("/admin/meta")
def meta_onboarding_page():
    _legacy_ui.page_shell("Integration Admin · Facebook")
    if not _legacy_ui.authenticated():
        ui.navigate.to("/admin/login")
        return

    runtime = meta_runtime_state()
    saved_step, expired = _load_meta_step()

    with ui.header().classes("items-center justify-between bg-white text-slate-900 border-b border-slate-200"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/admin")).props("flat round")
            with ui.column().classes("gap-0"):
                ui.label("Facebook Messenger").classes("text-xl font-semibold")
                ui.label("Conexión administrada sin Element").classes("text-xs text-slate-500")
        ui.button("Actualizar", icon="refresh", on_click=lambda: ui.navigate.to("/admin/meta")).props("flat")

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 md:p-6 gap-5"):
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
            ui.label("Este panel habla con mautrix-meta por la red privada. Nunca muestra el secreto de provisioning ni pide copiar cookies desde DevTools.").classes("text-slate-600")

            flow_options: dict[str, str] = {}
            try:
                for flow in _prov_client().flows():
                    flow_id = str(flow.get("id") or "")
                    if not flow_id:
                        continue
                    name = str(flow.get("name") or flow_id)
                    description = str(flow.get("description") or "")
                    flow_options[flow_id] = f"{name} — {description}" if description else name
            except Exception as exc:
                ui.label(f"No se pudieron cargar los métodos de acceso: {exc}").classes("text-red-700 mt-2")

            preferred = "facebook" if "facebook" in flow_options else (next(iter(flow_options), None))
            flow_select = ui.select(flow_options, value=preferred, label="Método de conexión").props("outlined").classes("w-full mt-3")

            async def start_login():
                try:
                    step = await asyncio.to_thread(_prov_client().start, str(flow_select.value or ""))
                    _store_meta_step(step)
                    ui.navigate.to("/admin/meta")
                except Exception as exc:
                    ui.notify(f"No se pudo iniciar la conexión: {exc}", type="negative", close_button=True)

            async def disconnect_all():
                try:
                    await asyncio.to_thread(_prov_client().logout, "all")
                    _clear_meta_step()
                    ui.notify("Cuenta Meta desconectada", type="positive")
                    ui.navigate.to("/admin/meta")
                except Exception as exc:
                    ui.notify(f"No se pudo desconectar: {exc}", type="negative", close_button=True)

            with ui.row().classes("gap-3 mt-3"):
                if flow_options:
                    ui.button("Conectar Facebook", icon="login", on_click=start_login)
                else:
                    ui.button("Conectar Facebook", icon="login", on_click=start_login).disable()
                if runtime.get("logins"):
                    ui.button("Desconectar", icon="link_off", on_click=disconnect_all).props("outline color=negative")

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
                    ui.label("La versión actual de mautrix requiere una sesión web de Facebook para este método. Un navegador normal no puede entregar esas cookies de forma segura al panel.").classes("text-slate-700 mt-3")
                    ui.label(f"Sitio de autenticación: {url}").classes("text-sm text-slate-500")
                    ui.label("Usa el helper local de autenticación. El emparejamiento dura 5 minutos, es de un solo uso y nunca contiene el secreto de provisioning.").classes("text-blue-700 font-medium mt-2")

                    async def launch_helper():
                        try:
                            pairing = create_pairing(saved_step)
                            handoff_id = json.dumps(pairing["id"])
                            token = json.dumps(pairing["token"])
                            await ui.run_javascript(
                                "window.location.href = 'mautrix-meta-helper://connect?origin=' + "
                                "encodeURIComponent(window.location.origin) + '&id=' + "
                                f"encodeURIComponent({handoff_id}) + '&token=' + encodeURIComponent({token});"
                            )
                            ui.notify("Se abrió el helper. Si el navegador pregunta, autoriza abrir la aplicación.", type="info")
                        except Exception as exc:
                            ui.notify(f"No se pudo crear el emparejamiento: {exc}", type="negative", close_button=True)

                    ui.button("Abrir helper de Facebook", icon="open_in_new", on_click=launch_helper).classes("mt-3")
                    ui.label("Si el helper aún no está instalado, instala la aplicación de escritorio de este repositorio y vuelve a pulsar el botón.").classes("text-xs text-slate-500 mt-2")

                elif step_type == "user_input":
                    inputs: dict[str, Any] = {}
                    for field in (saved_step.get("user_input") or {}).get("fields") or []:
                        field_id = str(field.get("id") or "")
                        if not field_id:
                            continue
                        options = field.get("options") or []
                        if options:
                            opts = {str(o.get("id")): str(o.get("name") or o.get("id")) for o in options if o.get("id") is not None}
                            inputs[field_id] = ui.select(opts, label=_field_label(field)).props("outlined").classes("w-full")
                        else:
                            secret = str(field.get("type") or "").lower() in {"password", "secret"} or "password" in field_id.lower()
                            inputs[field_id] = ui.input(_field_label(field), password=secret, password_toggle_button=secret).props("outlined autocomplete=off").classes("w-full")

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
                            _store_meta_step(step)
                            ui.navigate.to("/admin/meta")
                        except Exception as exc:
                            ui.notify(f"No se pudo continuar: {exc}", type="negative", close_button=True)
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
                            ui.notify(f"No se pudo continuar: {exc}", type="negative", close_button=True)
                    ui.button("Ya completé este paso", on_click=continue_wait, icon="check").classes("mt-3")

                elif step_type in {"webauthn", "client_http"}:
                    ui.label("Este paso requiere capacidades locales del navegador/dispositivo que el backend no debe simular. Se delegará al helper de autenticación.").classes("text-blue-700 mt-3")

                with ui.row().classes("mt-4 gap-3"):
                    ui.button("Cancelar intento", icon="close", on_click=cancel_login).props("outline color=negative")

        with ui.card().classes("w-full p-5 bg-slate-50"):
            ui.label("Seguridad").classes("font-semibold")
            ui.label("El secreto de provisioning se lee únicamente dentro del contenedor desde el volumen privado de mautrix. Las respuestas introducidas por el usuario no se guardan en el estado genérico del panel.").classes("text-sm text-slate-600")
            if _legacy_ui.legacy.get_setting(META_LAST_COMPLETE_KEY):
                ui.label(f"Última conexión completada: {_legacy_ui.legacy.get_setting(META_LAST_COMPLETE_KEY)}").classes("text-xs text-slate-500 mt-2")


if __name__ in {"__main__", "__mp_main__"}:
    _legacy_ui.run()
