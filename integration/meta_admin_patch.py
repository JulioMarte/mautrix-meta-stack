"""Native admin-v2 integration for managed Meta onboarding.

The production admin redirects /admin to /admin/basic, so onboarding must be part
of the actual NiceGUI navigation chrome instead of relying on injected JavaScript
that only runs on the legacy /admin path.
"""
from __future__ import annotations

from nicegui import app, ui

import admin_v2


NAV_ITEMS = [
    ("basic", "Basic setup", "tune", "/admin/basic"),
    ("meta", "Facebook Messenger", "forum", "/admin/meta-cookie"),
    ("advanced", "Advanced", "settings", "/admin/advanced"),
    ("status", "Status & tests", "monitor_heart", "/admin/status"),
]


def admin_chrome(active: str):
    """Render the real admin-v2 chrome with Facebook onboarding as first-class UI."""
    ui.page_title("Matrix ↔ Chatwoot Admin")
    ui.colors(primary="#2563eb", positive="#15803d", negative="#b91c1c", warning="#d97706")
    ui.query("body").classes("bg-slate-50 text-slate-900")
    drawer = ui.left_drawer(value=True).classes("bg-white border-r border-slate-200")
    with drawer:
        with ui.column().classes("w-full gap-1 p-3"):
            ui.label("Integration Admin").classes("text-lg font-semibold px-2 py-2")
            for key, label, icon, target in NAV_ITEMS:
                classes = "w-full justify-start " + ("bg-blue-50 text-blue-700" if active == key else "")
                ui.button(
                    label,
                    icon=icon,
                    on_click=lambda target=target: ui.navigate.to(target),
                ).props("flat no-caps").classes(classes)
            ui.separator().classes("my-3")
            ui.label(
                "Element is optional for diagnostics. Normal operation should happen entirely through Chatwoot."
            ).classes("text-xs text-slate-500 px-2")

    with ui.header().classes("items-center justify-between bg-white text-slate-900 border-b border-slate-200"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="menu", on_click=drawer.toggle).props("flat round")
            ui.icon("hub").classes("text-blue-600")
            ui.label("Matrix ↔ Chatwoot").classes("font-semibold text-lg")

        async def logout():
            app.storage.user.clear()
            ui.navigate.to("/admin/login")

        ui.button("Logout", icon="logout", on_click=logout).props("flat no-caps")


def install() -> None:
    """Replace only the shared admin chrome; page/business logic stays untouched."""
    admin_v2._admin_chrome = admin_chrome
