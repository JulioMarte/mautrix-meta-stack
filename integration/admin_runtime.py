"""Production entrypoint that registers runtime enhancements before NiceGUI starts."""
import nicegui_app
import runtime_enhancements  # noqa: F401  - import registers routes/pages and runtime patches


if __name__ == "__main__":
    nicegui_app.run()
