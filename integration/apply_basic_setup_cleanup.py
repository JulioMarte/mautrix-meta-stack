from __future__ import annotations

from pathlib import Path


PATH = Path(__file__).with_name("admin_v2.py")


OLD_CALLBACK_UI = '''            ui.label("This is now the canonical outbound path: Chatwoot agent reply → this integration → Matrix → Meta. It replaces the old account-level webhook setup for normal operation.").classes("text-slate-600")
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
'''

NEW_CALLBACK_UI = '''            ui.label("This is the canonical outbound path: Chatwoot agent reply → this integration → Matrix → Meta.").classes("text-slate-600")
            with ui.row().classes("w-full items-end gap-2"):
                ui.input("Required callback URL", value=callback_url).props("outlined readonly").classes("grow")

                async def copy_callback_url():
                    await ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(callback_url)})")
                    ui.notify("Callback URL copied", type="positive")

                ui.button(icon="content_copy", on_click=copy_callback_url).props("flat round").tooltip("Copy callback URL")

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
'''


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} block, found {count}")
    return source.replace(old, new, 1)


def main() -> None:
    source = PATH.read_text(encoding="utf-8")
    source = replace_once(
        source,
        '    old_webhook_url = _origin(request) + "/webhooks/chatwoot"\n',
        "",
        "legacy webhook URL assignment",
    )
    source = replace_once(source, OLD_CALLBACK_UI, NEW_CALLBACK_UI, "basic callback UI")
    PATH.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
